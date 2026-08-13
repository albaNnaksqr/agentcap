"""Trajectory extraction test, focused on the codex yield/wait split.

A codex `exec` cell that outlives its `yield_time_ms` returns a placeholder
naming the still-running cell instead of the command's output; the agent then
collects the real output with `wait({cell_id})`. Unstitched, the run reads as
"produced nothing", which value.ended_green and taskseed._timeline both read as
"not green" -- a slow green suite scores worse than a fast one.

Covers: the plain (non-yielding) path is untouched; a yielded cell is stitched
back from its wait; matching is by cell id across interleaved calls, not by
adjacency; a wait that itself yields keeps the cell open for the next wait; and
a wait is never surfaced as a run of its own.
Run with:  python3 -m agentcap.tests.test_tooltrace
"""
import json
import os
import sys
import tempfile

from agentcap import tooltrace as T
from agentcap import testparse

STUB = "Script running with cell ID %s\nWall time 10.0 seconds\nOutput:\n\n{}"


def codex_log(path, events):
    """events: ('exec', cmd, out) | ('wait', cell_id, out) | ('plan', None, out)

    Mirrors the real shape: exec is a custom_tool_call whose input is a JS
    snippet, wait is a function_call whose arguments are JSON.
    """
    lines = []
    for i, (kind, a, out) in enumerate(events):
        ts = "2026-08-11T00:%02d:00.000Z" % i
        cid = "call_%d" % i
        if kind == "exec":
            inp = 'const r = await tools.exec_command({cmd:%s,workdir:"/w"});' % json.dumps(a)
            call = {"type": "custom_tool_call", "name": "exec", "input": inp, "call_id": cid}
        elif kind == "wait":
            call = {"type": "function_call", "name": "wait", "call_id": cid,
                    "arguments": json.dumps({"cell_id": a, "yield_time_ms": 30000})}
        else:
            call = {"type": "custom_tool_call", "name": "exec", "call_id": cid,
                    "input": 'const p = await tools.update_plan({plan:[]});'}
        lines.append({"timestamp": ts, "type": "response_item", "payload": call})
        lines.append({"timestamp": ts, "type": "response_item",
                      "payload": {"type": "custom_tool_call_output"
                                  if kind != "wait" else "function_call_output",
                                  "call_id": cid, "output": out or ""}})
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")
    return path


def counts(runs):
    return [testparse.parse(r["output"], r.get("framework"))["counts"] for r in runs]


def main():
    fails = []
    tmp = tempfile.mkdtemp(prefix="agentcap-tooltrace-")
    PYTEST = "python -m pytest -q"
    GREEN = "....\n4 passed in 1.00s\n"
    RED = "FFFF\n4 failed in 1.00s\n"

    # 1. no yielding at all -- the ordinary path must be untouched by the stitch
    log = codex_log(os.path.join(tmp, "plain.jsonl"),
                    [("exec", PYTEST, RED), ("exec", PYTEST, GREEN)])
    runs = T.test_runs(log, "codex")
    if counts(runs) != [{"failed": 4}, {"passed": 4}]:
        fails.append("plain path changed: %s" % counts(runs))
    else:
        print("[ok] non-yielding runs pass through unchanged")

    # 2. the bug: a slow run yields, the output arrives on the wait
    log = codex_log(os.path.join(tmp, "yield.jsonl"),
                    [("exec", PYTEST, STUB % "9"), ("wait", "9", GREEN)])
    runs = T.test_runs(log, "codex")
    if len(runs) != 1:
        fails.append("a wait must not surface as a run of its own: %d runs" % len(runs))
    elif counts(runs) != [{"passed": 4}]:
        fails.append("yielded cell not stitched from its wait: %s" % counts(runs))
    else:
        print("[ok] yielded cell stitched back from its wait")

    # 3. matching is by cell id, not adjacency: the agent routinely slips an
    #    update_plan (and even another exec) between the yield and the wait,
    #    and two cells can be in flight with their waits out of order.
    log = codex_log(os.path.join(tmp, "interleaved.jsonl"),
                    [("exec", PYTEST, STUB % "9"),
                     ("exec", "grep -rn foo .", "no matches"),
                     ("plan", None, "{}"),
                     ("exec", PYTEST, STUB % "11"),
                     ("wait", "11", RED),
                     ("wait", "9", GREEN)])
    runs = T.test_runs(log, "codex")
    if counts(runs) != [{"passed": 4}, {"failed": 4}]:
        fails.append("cell-id matching failed across interleaving: %s" % counts(runs))
    else:
        print("[ok] stitched by cell id across interleaved calls and out-of-order waits")

    # 4. a wait can time out too and hand back another placeholder; the cell has
    #    to stay open so the NEXT wait on it still lands.
    log = codex_log(os.path.join(tmp, "double.jsonl"),
                    [("exec", PYTEST, STUB % "3"),
                     ("wait", "3", STUB % "3"),
                     ("wait", "3", GREEN)])
    runs = T.test_runs(log, "codex")
    if counts(runs) != [{"passed": 4}]:
        fails.append("a re-yielding wait closed the cell early: %s" % counts(runs))
    else:
        print("[ok] repeated waits on one cell all land")

    # 5. partial stdout printed before the yield must survive -- the stitch
    #    appends, it does not replace. Losing the head would trade one lossy
    #    read for another.
    partial = "collected 4 items\n" + (STUB % "5")
    log = codex_log(os.path.join(tmp, "partial.jsonl"),
                    [("exec", PYTEST, partial), ("wait", "5", GREEN)])
    runs = T.test_runs(log, "codex")
    if "collected 4 items" not in runs[0]["output"]:
        fails.append("stitch replaced instead of appending; partial stdout lost")
    elif counts(runs) != [{"passed": 4}]:
        fails.append("append broke parsing: %s" % counts(runs))
    else:
        print("[ok] partial stdout before the yield is preserved")

    # 6. an orphan wait (no matching cell) must not crash or invent a run
    log = codex_log(os.path.join(tmp, "orphan.jsonl"),
                    [("wait", "7", GREEN), ("exec", PYTEST, RED)])
    runs = T.test_runs(log, "codex")
    if counts(runs) != [{"failed": 4}]:
        fails.append("orphan wait leaked into the runs: %s" % counts(runs))
    else:
        print("[ok] an unmatched wait is ignored")

    # ---- codex JSON result envelopes (litellm#35428) ----------------------
    # The counts survive an envelope because the summary regex needs no line
    # breaks; the test NAMES do not, and they are what seed/replay run on. So
    # every assertion below is on the extracted names, not on the counts.
    NODE = "tests/t.py::test_a"
    RED_TXT = "F\nFAILED %s - AssertionError\n1 failed in 0.10s\n" % NODE

    def env(**kw):
        d = {"chunk_id": "c1", "exit_code": 1}
        d.update(kw)
        return json.dumps(d)

    # 7. flat envelope: {"chunk_id":..,"output":"<escaped stdout>"}
    log = codex_log(os.path.join(tmp, "env_flat.jsonl"),
                    [("exec", PYTEST, "Output:\n" + env(output=RED_TXT))])
    runs = T.test_runs(log, "codex")
    got = testparse.parse(runs[0]["output"], runs[0].get("framework"))
    if set(got["failed"]) != {NODE}:
        fails.append("flat envelope: failing test name not recovered: %s" % got["failed"])
    else:
        print("[ok] flat JSON envelope unwrapped, failing test name recovered")

    # 8. keyed by cell, with an empty cell alongside -- the real shape codex
    #    emits for a multi-cell script.
    keyed = json.dumps({"patch": {}, "plan": {},
                        "red": {"chunk_id": "45e58f", "exit_code": 1, "output": RED_TXT}})
    log = codex_log(os.path.join(tmp, "env_keyed.jsonl"),
                    [("exec", PYTEST, "Script completed\nWall time 7.9 seconds\nOutput:\n" + keyed)])
    runs = T.test_runs(log, "codex")
    got = testparse.parse(runs[0]["output"], runs[0].get("framework"))
    if set(got["failed"]) != {NODE}:
        fails.append("keyed envelope: failing test name not recovered: %s" % got["failed"])
    else:
        print("[ok] cell-keyed envelope unwrapped past the empty cells")

    # 9. codex's own truncation warning is the ONLY signal that the payload is
    #    incomplete -- unwrapping must not eat it.
    warn = "Warning: truncated output (original token count: 20627)\nTotal output lines: 1\n\n"
    log = codex_log(os.path.join(tmp, "env_warn.jsonl"),
                    [("exec", PYTEST, warn + env(output=RED_TXT))])
    runs = T.test_runs(log, "codex")
    if "truncated output" not in runs[0]["output"]:
        fails.append("unwrap dropped codex's truncation warning")
    elif set(testparse.parse(runs[0]["output"], runs[0].get("framework"))["failed"]) != {NODE}:
        fails.append("unwrap kept the warning but lost the payload")
    else:
        print("[ok] truncation warning preserved alongside the unwrapped payload")

    # 10. a suite that legitimately prints JSON with an "output" key is NOT an
    #     envelope (no chunk_id) and must survive byte-for-byte.
    plain_json = '{"output": "hello"}\n.\n1 passed in 0.10s\n'
    log = codex_log(os.path.join(tmp, "env_notours.jsonl"),
                    [("exec", PYTEST, plain_json)])
    runs = T.test_runs(log, "codex")
    if runs[0]["output"] != plain_json:
        fails.append("rewrote non-envelope JSON: %r" % runs[0]["output"])
    else:
        print("[ok] JSON without chunk_id is left verbatim")

    # 11. two cells that both ran: outputs land in the order they ran, so a
    #     red cell followed by a green cell still reads as red-then-green.
    two = json.dumps({"a": {"chunk_id": "1", "output": RED_TXT},
                      "b": {"chunk_id": "2", "output": GREEN}})
    log = codex_log(os.path.join(tmp, "env_two.jsonl"), [("exec", PYTEST, two)])
    runs = T.test_runs(log, "codex")
    out = runs[0]["output"]
    if not (NODE in out and "4 passed" in out and out.index(NODE) < out.index("4 passed")):
        fails.append("multi-cell envelope lost order or content: %r" % out[:200])
    else:
        print("[ok] multi-cell envelope keeps cell order")

    # 12. text with no envelope at all is untouched (the common case).
    log = codex_log(os.path.join(tmp, "env_none.jsonl"), [("exec", PYTEST, GREEN)])
    runs = T.test_runs(log, "codex")
    if runs[0]["output"] != GREEN:
        fails.append("envelope-free output was rewritten: %r" % runs[0]["output"])
    else:
        print("[ok] envelope-free output passes through byte-for-byte")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
