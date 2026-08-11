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

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
