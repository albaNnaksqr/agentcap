"""Multi-framework test recognition: detect_framework on commands, per-framework
output parsing (pytest / unittest / go / cargo / js), tooltrace surfacing non-pytest
runs, and value.assess grounding a jest session. Run with
    python3 -m agentcap.tests.test_testparse
"""
import json
import os
import subprocess
import sys
import tempfile

from agentcap import testparse as tp
from agentcap import tooltrace
from agentcap import session as sess
from agentcap import join as J
from agentcap import value as V


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def git(repo, *a):
    sh("git", "-C", repo, *a)


def write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def claude_log(path, cwd, events):
    lines = [{"type": "system", "cwd": cwd, "timestamp": "2026-07-01T00:00:00.000Z"}]
    for i, (kind, a, b) in enumerate(events):
        ts = "2026-07-01T00:%02d:00.000Z" % i
        if kind == "bash":
            cid = "c%d" % i
            lines.append({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "id": cid, "name": "Bash", "input": {"command": a}}]}})
            lines.append({"type": "user", "timestamp": ts, "message": {"content": [
                {"type": "tool_result", "tool_use_id": cid,
                 "content": [{"type": "text", "text": b}]}]}})
        else:
            lines.append({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "id": "e%d" % i, "name": "Edit",
                 "input": {"file_path": a}}]}})
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


def main():
    fails = []

    # --- detect_framework on commands ---
    cases = [
        ("python -m pytest -q", "pytest"),
        ("uv run pytest tests/", "pytest"),
        ("python -m unittest discover", "unittest"),
        ("go test ./...", "go"),
        ("cargo test --workspace", "cargo"),
        ("npm test", "js"),
        ("yarn test", "js"),
        ("npx vitest run", "js"),
        ("npx jest src/", "js"),
        ("ls tests/", None),
        ("echo pytest_cache", None),        # word boundary: not a pytest invocation
        ("git log", None),
    ]
    bad = [(c, want, tp.detect_framework(c)) for c, want in cases
           if tp.detect_framework(c) != want]
    if bad:
        fails.append("detect_framework wrong: %s" % bad)
    else:
        print("[ok] detect_framework: %d commands classified" % len(cases))

    # --- pytest parsing preserved (node level) ---
    p = tp.parse("tests/test_a.py::test_x FAILED\n=== 1 failed in 0.1s ===", "pytest")
    if p["failed"] != {"tests/test_a.py::test_x"} or p["counts"].get("failed") != 1:
        fails.append("pytest parse regressed: %s" % p)
    else:
        print("[ok] pytest parsing preserved")

    # --- unittest: Ran N + OK / FAILED(failures=, errors=) ---
    u_ok = tp.parse("Ran 3 tests in 0.001s\n\nOK\n", "unittest")
    u_bad = tp.parse("Ran 4 tests in 0.003s\n\nFAILED (failures=1, errors=1)\n", "unittest")
    if u_ok["counts"].get("passed") != 3 or u_ok["counts"].get("failed", 0) != 0:
        fails.append("unittest OK parse wrong: %s" % u_ok)
    elif u_bad["counts"].get("failed") != 1 or u_bad["counts"].get("error") != 1 \
            or u_bad["counts"].get("passed") != 2:
        fails.append("unittest FAILED parse wrong: %s" % u_bad)
    else:
        print("[ok] unittest counts (OK and FAILED forms)")

    # --- go: verbose nodes + quiet ok ---
    g_v = tp.parse("--- FAIL: TestFoo\n--- PASS: TestBar\nFAIL\nexit status 1\n", "go")
    g_q = tp.parse("ok  \texample.com/pkg\t0.012s\n", "go")
    if g_v["failed"] != {"TestFoo"} or g_v["passed"] != {"TestBar"}:
        fails.append("go verbose nodes wrong: %s" % g_v)
    elif g_q["counts"].get("passed", 0) < 1 or g_q["counts"].get("failed", 0) != 0:
        fails.append("go quiet ok wrong: %s" % g_q)
    else:
        print("[ok] go: verbose node status + quiet package ok")

    # --- cargo: node lines + result summary ---
    c = tp.parse("test tests::foo ... FAILED\ntest tests::bar ... ok\n\n"
                 "test result: FAILED. 1 passed; 1 failed; 0 ignored\n", "cargo")
    if c["failed"] != {"tests::foo"} or c["passed"] != {"tests::bar"} \
            or c["counts"].get("passed") != 1 or c["counts"].get("failed") != 1:
        fails.append("cargo parse wrong: %s" % c)
    else:
        print("[ok] cargo: node lines + result summary counts")

    # --- js: jest / vitest / mocha count styles ---
    j = tp.parse("Tests:       2 failed, 10 passed, 12 total\n", "js")
    vt = tp.parse("  Tests  1 failed | 3 passed (4)\n", "js")
    m = tp.parse("  5 passing (20ms)\n  2 failing\n", "js")
    if j["counts"].get("failed") != 2 or j["counts"].get("passed") != 10:
        fails.append("jest counts wrong: %s" % j)
    elif vt["counts"].get("failed") != 1 or vt["counts"].get("passed") != 3:
        fails.append("vitest counts wrong: %s" % vt)
    elif m["counts"].get("passed") != 5 or m["counts"].get("failed") != 2:
        fails.append("mocha counts wrong: %s" % m)
    else:
        print("[ok] js: jest / vitest / mocha count lines")

    # --- tooltrace surfaces non-pytest runs, tagged with framework ---
    tmp = tempfile.mkdtemp(prefix="agentcap-tp-")
    log = os.path.join(tmp, "log.jsonl")
    claude_log(log, tmp, [
        ("bash", "npm test", "Tests:       1 failed, 2 passed, 3 total"),
        ("bash", "ls -la", "total 0"),
        ("bash", "go test ./...", "ok  \tpkg\t0.01s"),
    ])
    runs = tooltrace.test_runs(log, "claude")
    fw = [r.get("framework") for r in runs]
    if len(runs) != 2 or fw != ["js", "go"]:
        fails.append("tooltrace multi-framework runs wrong: %s" % fw)
    else:
        print("[ok] tooltrace: npm/go runs surfaced with framework tags, ls skipped")

    # --- end to end: a jest red->green session grounds (was systematically
    #     ungrounded when only pytest counted) ---
    repo = os.path.join(tmp, "repo")
    store = os.path.join(tmp, "store")
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    write(repo, "src/a.js", "let v = 0\n")
    write(repo, "src/b.js", "let w = 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    sid, _ = sess.start_session(repo, agent="claude", root=store, session_id="claude-S",
                                extra={"agent_session_id": "S", "log_path": "/x"})
    write(repo, "src/a.js", "let v = 1\n")
    write(repo, "src/b.js", "let w = 1\n")
    sess.end_session(session_id=sid, root=store)
    jlog = os.path.join(tmp, "jest.jsonl")
    claude_log(jlog, repo, [
        ("bash", "npm test", "Tests:       1 failed, 2 passed, 3 total"),
        ("edit", "src/a.js", None), ("edit", "src/b.js", None),
        ("bash", "npm test", "Tests:       3 passed, 3 total"),
    ])
    J.set_join(sid, {"agent": "claude", "session_id": "S", "cwd": repo, "log_path": jlog,
                     "first_ts": 0, "last_ts": 0, "n_steps": 4},
               confidence="high", root=store)
    sess.verify_session(sid, root=store)
    _, sdir = sess._paths(store)
    v = V.assess(os.path.join(sdir, sid))
    if not v or v["groundedness"] != "grounded":
        fails.append("jest red->green session should be grounded: %s"
                     % (v and v["groundedness"]))
    elif "js" not in v["signals"].get("frameworks", []):
        fails.append("frameworks signal missing js: %s" % v["signals"].get("frameworks"))
    else:
        print("[ok] jest red->green session -> grounded (tier=%s)" % v["value_tier"])

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
