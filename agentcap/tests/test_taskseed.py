"""Task-seed test (small increment): mine candidate FAIL_TO_PASS from a trajectory's
pytest outputs, WITHOUT running anything, and grade it. Covers: strong (source fix +
red->green), the gaming trap (test-only change -> weak), counts-only, and pytest
output parsing. Run with:  python3 -m agentcap.tests.test_taskseed
"""
import json
import os
import subprocess
import sys
import tempfile

from agentcap import session as sess
from agentcap import join as J
from agentcap import taskseed as T


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def git(repo, *a):
    sh("git", "-C", repo, *a)


def write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def claude_log(path, cwd, runs):
    """Fabricate a Claude-shape jsonl with pytest tool_use/tool_result pairs.
    runs = list of (command, output_text)."""
    lines = [{"type": "system", "cwd": cwd, "timestamp": "2026-07-01T00:00:00.000Z"}]
    for i, (cmd, out) in enumerate(runs):
        cid = "call%d" % i
        lines.append({"type": "assistant", "timestamp": "2026-07-01T00:0%d:00.000Z" % i,
                      "message": {"content": [
                          {"type": "tool_use", "id": cid, "name": "Bash",
                           "input": {"command": cmd}}]}})
        lines.append({"type": "user", "timestamp": "2026-07-01T00:0%d:30.000Z" % i,
                      "message": {"content": [
                          {"type": "tool_result", "tool_use_id": cid,
                           "content": [{"type": "text", "text": out}]}]}})
    with open(path, "w") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


def build_session(tmp, name, source_fix, log_runs):
    """A real env session (start/end snapshot) + a fabricated joined trajectory."""
    repo = os.path.join(tmp, name)
    store = os.path.join(tmp, name + "_store")
    os.makedirs(repo)
    git(repo, "init", "-q"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    write(repo, "src/mod.py", "def f():\n    return 0\n")
    write(repo, "tests/test_mod.py", "from src.mod import f\ndef test_f():\n    assert f() == 1\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base (test red)")

    sid, _ = sess.start_session(repo, agent="claude", root=store, session_id="claude-S",
                                extra={"agent_session_id": "S", "log_path": "/x"})
    # agent work
    if source_fix:
        write(repo, "src/mod.py", "def f():\n    return 1\n")     # real fix
    else:
        write(repo, "tests/test_mod.py",                          # gamed: edit the test
              "from src.mod import f\ndef test_f():\n    assert f() == 0\n")
    sess.end_session(session_id=sid, root=store)

    log = os.path.join(tmp, name + ".jsonl")
    claude_log(log, repo, log_runs)
    J.set_join(sid, {"agent": "claude", "session_id": "S", "cwd": repo,
                     "log_path": log, "first_ts": 0, "last_ts": 0, "n_steps": 4},
               confidence="high", root=store)
    _, sdir = sess._paths(store)
    return os.path.join(sdir, sid)


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-seed-")
    fails = []

    # --- pytest output parsing ---
    p = T.parse_pytest("tests/test_mod.py::test_f FAILED\n=== 1 failed in 0.1s ===")
    if "tests/test_mod.py::test_f" not in p["failed"] or p["counts"].get("failed") != 1:
        fails.append("parse verbose FAILED wrong: %s" % p)
    p2 = T.parse_pytest("FAILED tests/test_mod.py::test_f - AssertionError\n1 failed, 2 passed")
    if "tests/test_mod.py::test_f" not in p2["failed"] or p2["counts"].get("passed") != 2:
        fails.append("parse -q summary FAILED wrong: %s" % p2)
    if not fails:
        print("[ok] pytest output parsing (verbose + -q summary)")

    RED = "tests/test_mod.py::test_f FAILED\n=== 1 failed in 0.1s ==="
    GREEN = "tests/test_mod.py::test_f PASSED\n=== 1 passed in 0.1s ==="

    # --- node-level red->green: FTP mined, source_delta captured, verified=false ---
    sdir = build_session(tmp, "strong", source_fix=True,
                         log_runs=[("python -m pytest -q", RED),
                                   ("python -m pytest -q", GREEN)])
    seed = T.extract_seed(sdir)
    if not seed or seed["candidate_fail_to_pass"] != ["tests/test_mod.py::test_f"]:
        fails.append("FTP not mined: %s" % seed)
    elif seed["verified"] is not False:
        fails.append("seed must be verified=false")
    elif seed["test_only_delta"]:
        fails.append("source fix should not be flagged test_only")
    else:
        print("[ok] node red->green: FTP mined, source fix, verified=false")

    # --- test-only change flagged (a neutral flag now, not a 'weak' verdict) ---
    sdir2 = build_session(tmp, "gamed", source_fix=False,
                          log_runs=[("python -m pytest -q", RED),
                                    ("python -m pytest -q", GREEN)])
    seed2 = T.extract_seed(sdir2)
    if not seed2 or not seed2["test_only_delta"]:
        fails.append("test-only change should set test_only_delta: %s" % seed2)
    else:
        print("[ok] test-only change flagged (test_only_delta=true)")

    # --- COUNTS-ONLY: failures then zero, no node ids fabricated ---
    sdir3 = build_session(tmp, "counts", source_fix=True,
                          log_runs=[("python -m pytest -q", "=== 1 failed in 0.1s ==="),
                                    ("python -m pytest -q", "=== 2 passed in 0.1s ===")])
    seed3 = T.extract_seed(sdir3)
    if not seed3:
        fails.append("counts-only should still yield a seed (evidence)")
    elif seed3["candidate_fail_to_pass"]:
        fails.append("counts-only should have no node FTP (nothing fabricated)")
    else:
        print("[ok] counts-only: evidence kept, no fabricated node ids")

    # --- NO SEED: no test runs at all ---
    sdir4 = build_session(tmp, "none", source_fix=True,
                          log_runs=[("ls -la", "src tests")])
    if T.extract_seed(sdir4) is not None:
        fails.append("no test runs should yield no seed")
    else:
        print("[ok] no test runs -> no seed (silent-noise avoided)")

    # --- PYTHONPATH extraction: $PWD/subdir, abs-under-cwd, and drop-outside ---
    cwd = "/home/u/wt/repo"
    pp = T._pythonpath_components('PYTHONPATH=$PWD/python python -m pytest a::b', cwd)
    if pp != ["python"]:
        fails.append("pythonpath $PWD/python -> ['python'], got %s" % pp)
    pp2 = T._pythonpath_components('PYTHONPATH="$PWD:%s/src" pytest x::y' % cwd, cwd)
    if pp2 != [".", "src"]:
        fails.append("pythonpath quoted $PWD:abs-under-cwd -> ['.','src'], got %s" % pp2)
    pp3 = T._pythonpath_components('PYTHONPATH=/opt/site-packages pytest x::y', cwd)
    if pp3 != []:
        fails.append("pythonpath outside worktree should be dropped, got %s" % pp3)
    if any(f.startswith("pythonpath") for f in fails):
        pass
    else:
        print("[ok] pythonpath: $PWD/subdir relativized, machine-abs dropped")

    # --- unittest dotted ids: resolved to a repo file, or refused ---
    end = {"tests/test_mod.py", "src/mod.py", "pkg/sub/test_deep.py", "other/test_deep.py"}
    r_disc = T._node_path("test_mod.Case.test_f", end)          # discover -s tests
    r_full = T._node_path("pkg.sub.test_deep.Case.test_f", end)  # full dotted path
    r_amb = T._node_path("test_deep.Case.test_f", end)           # two files, same basename
    r_none = T._node_path("nope.Case.test_f", end)
    r_pytest = T._node_path("tests/test_mod.py::test_f", end)    # unchanged behaviour
    r_gone = T._node_path("tests/deleted.py::test_f", end)
    if r_disc != "tests/test_mod.py":
        fails.append("discover-relative dotted id unresolved: %s" % r_disc)
    elif r_full != "pkg/sub/test_deep.py":
        fails.append("full dotted path unresolved: %s" % r_full)
    elif r_amb is not None:
        fails.append("ambiguous basename must refuse, got %s" % r_amb)
    elif r_none is not None or r_gone is not None:
        fails.append("unknown ids must resolve to None: %s / %s" % (r_none, r_gone))
    elif r_pytest != "tests/test_mod.py":
        fails.append("pytest id resolution changed: %s" % r_pytest)
    else:
        print("[ok] unittest dotted ids grounded to files; ambiguity refused")

    # --- end-to-end: a unittest session yields a dotted FTP, grounded ---
    U_RED = ("test_f (tests.test_mod.Case.test_f) ... FAIL\n"
             "\nFAIL: test_f (tests.test_mod.Case.test_f)\n"
             "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
    U_GREEN = "test_f (tests.test_mod.Case.test_f) ... ok\n\nRan 1 test in 0.001s\n\nOK\n"
    sdir_u = build_session(tmp, "unittest_rg", source_fix=True,
                           log_runs=[("python -m unittest discover -s tests -v", U_RED),
                                     ("python -m unittest discover -s tests -v", U_GREEN)])
    seed_u = T.extract_seed(sdir_u)
    if not seed_u or seed_u["candidate_fail_to_pass"] != ["tests.test_mod.Case.test_f"]:
        fails.append("unittest session should seed a dotted FTP: %s"
                     % (seed_u and seed_u["candidate_fail_to_pass"]))
    elif seed_u["test_files"] != ["tests/test_mod.py"]:
        fails.append("test_files should hold the resolved path: %s" % seed_u["test_files"])
    elif seed_u["source_delta"] != ["src/mod.py"]:
        fails.append("source_delta wrong: %s" % seed_u["source_delta"])
    else:
        print("[ok] unittest red->green session seeds a grounded dotted FTP")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
