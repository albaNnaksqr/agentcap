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


def build_session(tmp, name, source_fix, log_runs, extra_end_files=None,
                  extra_base_files=None, commit_at_end=False):
    """A real env session (start/end snapshot) + a fabricated joined trajectory."""
    repo = os.path.join(tmp, name)
    store = os.path.join(tmp, name + "_store")
    os.makedirs(repo)
    git(repo, "init", "-q"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    write(repo, "src/mod.py", "def f():\n    return 0\n")
    write(repo, "tests/test_mod.py", "from src.mod import f\ndef test_f():\n    assert f() == 1\n")
    for rel, c in (extra_base_files or {}).items():        # pre-existing, NOT authored
        write(repo, rel, c)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base (test red)")

    sid, _ = sess.start_session(repo, agent="claude", root=store, session_id="claude-S",
                                extra={"agent_session_id": "S", "log_path": "/x"})
    # agent work
    if source_fix:
        write(repo, "src/mod.py", "def f():\n    return 1\n")     # real fix
    else:
        write(repo, "tests/test_mod.py",                          # gamed: edit the test
              "from src.mod import f\ndef test_f():\n    assert f() == 0\n")
    for rel, c in (extra_end_files or {}).items():                # e.g. an authored test
        write(repo, rel, c)
    if commit_at_end:                       # some agents commit their own work
        git(repo, "add", "-A"); git(repo, "commit", "-qm", "agent commit")
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
    # $REPO_ROOT is a repo-root alias: left literal, replay points PYTHONPATH at
    # nothing and an installed copy shadows the reconstructed tree
    pp4 = T._pythonpath_components('PYTHONPATH="$REPO_ROOT/python" pytest a::b', cwd)
    if pp4 != ["python"]:
        fails.append("pythonpath $REPO_ROOT/python -> ['python'], got %s" % pp4)
    pp5 = T._pythonpath_components('PYTHONPATH=${REPO_ROOT} pytest a::b', cwd)
    if pp5 != ["."]:
        fails.append("pythonpath ${REPO_ROOT} -> ['.'], got %s" % pp5)
    # ...and when the agent inlines the substitution instead of binding a variable.
    # An alias table cannot catch this: it is a command, not a variable. The bare
    # and backtick forms matter too -- an \S+ match truncates them at the first
    # space into a plausible but nonexistent component, which is worse than no
    # match because it silently restores the shadowing. Observed on sglang#33867.
    for cmd, want, label in (
        ('PYTHONPATH="$(git rev-parse --show-toplevel)/python" pytest a::b',
         ["python"], 'quoted $(git rev-parse ...)'),
        ('PYTHONPATH=$(git rev-parse --show-toplevel)/python pytest a::b',
         ["python"], 'bare $(git rev-parse ...)'),
        ('PYTHONPATH=`git rev-parse --show-toplevel`/python pytest a::b',
         ["python"], 'backtick git rev-parse'),
        ('PYTHONPATH="$( git rev-parse  --show-toplevel )/python" pytest a::b',
         ["python"], 'whitespace-tolerant'),
    ):
        got = T._pythonpath_components(cmd, cwd)
        if got != want:
            fails.append("pythonpath %s -> %s, got %s" % (label, want, got))
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

    # --- authored tests win over merely-observed flips ---
    # the session writes test_f; a broad run also reds two pre-existing tests that
    # are never run again (litellm#35793) — those must not become the task
    RED_BROAD = ("tests/test_mod.py::test_f_extra FAILED\n"
                 "tests/test_other.py::test_legacy_a FAILED\n"
                 "tests/test_other.py::test_legacy_b FAILED\n"
                 "=== 3 failed in 0.3s ===")
    GREEN_ONE = "tests/test_mod.py::test_f_extra PASSED\n=== 1 passed in 0.1s ==="
    sdir_a = build_session(tmp, "authored", source_fix=True,
                           log_runs=[("python -m pytest -q", RED_BROAD),
                                     ("python -m pytest -q tests/test_mod.py", GREEN_ONE)],
                           extra_base_files={"tests/test_other.py":
                                             "def test_legacy_a():\n    assert 1\n"
                                             "def test_legacy_b():\n    assert 1\n"},
                           extra_end_files={"tests/test_mod.py":
                                            "from src.mod import f\n"
                                            "def test_f():\n    assert f() == 1\n"
                                            "def test_f_extra():\n    assert f() == 1\n"})
    seed_a = T.extract_seed(sdir_a)
    if not seed_a:
        fails.append("authored-filter fixture produced no seed")
    else:
        got = seed_a["candidate_fail_to_pass"]
        if seed_a["authored_tests"]:
            # test_f is authored -> the two legacy ids must be gone
            if got != ["tests/test_mod.py::test_f_extra"]:
                fails.append("authored test should be the only ftp: %s" % got)
            elif "tests/test_other.py::test_legacy_a" not in seed_a["dropped_observed_ftp"]:
                fails.append("dropped observed flips not recorded: %s"
                             % seed_a["dropped_observed_ftp"])
            else:
                print("[ok] authored test wins; merely-observed flips recorded as dropped")
        else:
            # nothing authored in this fixture -> behaviour must be unchanged
            if "tests/test_mod.py::test_f_extra" not in got:
                fails.append("no authored tests -> observed ftp must be kept: %s" % got)
            else:
                print("[ok] no authored test -> observed flips kept (fallback intact)")

    # --- an agent that commits its work must still be readable ---
    sdir_c = build_session(tmp, "committed", source_fix=True,
                           log_runs=[("python -m pytest -q", RED_BROAD),
                                     ("python -m pytest -q tests/test_mod.py", GREEN_ONE)],
                           extra_base_files={"tests/test_other.py":
                                             "def test_legacy_a():\n    assert 1\n"
                                             "def test_legacy_b():\n    assert 1\n"},
                           extra_end_files={"tests/test_mod.py":
                                            "from src.mod import f\n"
                                            "def test_f():\n    assert f() == 1\n"
                                            "def test_f_extra():\n    assert f() == 1\n"},
                           commit_at_end=True)
    import os as _os
    diffs = [_os.path.getsize(_os.path.join(sdir_c, "env_end", d))
             for d in ("staged.diff", "unstaged.diff")]
    seed_c = T.extract_seed(sdir_c)
    if any(diffs):
        fails.append("fixture should have empty diffs after committing: %s" % diffs)
    elif not seed_c or seed_c["authored_tests"] != ["test_f_extra"]:
        fails.append("committed work must still yield authored tests: %s"
                     % (seed_c and seed_c["authored_tests"]))
    elif seed_c["candidate_fail_to_pass"] != ["tests/test_mod.py::test_f_extra"]:
        fails.append("committed session ftp not narrowed: %s"
                     % seed_c["candidate_fail_to_pass"])
    else:
        print("[ok] agent committed its work -> authored tests read from the commit range")

    # the diff parser itself: only ADDED test defs count
    import tempfile as _tf
    d = _tf.mkdtemp(dir=tmp)
    os.makedirs(os.path.join(d, "env_end"))
    with open(os.path.join(d, "env_end", "unstaged.diff"), "w") as f:
        f.write("--- a/tests/t.py\n+++ b/tests/t.py\n"
                "+def test_added():\n+    assert 1\n"
                "-def test_removed():\n"
                " def test_untouched():\n"
                "+    async def test_async_added():\n"
                # a lone `+` blank line followed by a CONTEXT def: the shape an
                # agent leaves whenever it appends a test above an existing one.
                # \s in the pattern would cross the newline and claim
                # test_preexisting as authored, defeating the narrowing entirely.
                # Seen on litellm#36197.
                "+    def test_appended(self):\n+        assert 1\n+\n"
                "     def test_preexisting(self):\n")
    names = T._added_test_names(d)
    if names != {"test_added", "test_async_added", "test_appended"}:
        fails.append("added-test extraction wrong: %s" % sorted(names))
    elif T._node_func("tests/t.py::Case::test_x[param-1]") != "test_x":
        fails.append("node func extraction wrong: %s"
                     % T._node_func("tests/t.py::Case::test_x[param-1]"))
    elif T._node_func("pkg.mod.Case.test_y") != "test_y":
        fails.append("dotted node func extraction wrong")
    else:
        print("[ok] added-test extraction: additions only, params and dotted ids handled")

    # --- authored tests living in a NEW file -------------------------------
    # git diff covers tracked files only, so a brand-new test file produces no
    # hunk and the narrowing silently did not apply (sglang#35564: authored 0).
    import tempfile as _tf, json as _j, os as _os, hashlib as _h, shutil as _sh
    from agentcap.taskseed import _added_test_names
    d = _tf.mkdtemp(prefix="agentcap-newfile-")
    cas = _os.path.join(d, "cas"); _os.makedirs(cas)
    body = "import pytest\n\n\ndef test_brand_new():\n    assert True\n\n\nasync def test_async_new():\n    assert True\n"
    # store it the way snapshot does: CAS at oid[:2]/oid[2:]
    oid = _h.sha1(("blob %d\0" % len(body)).encode() + body.encode()).hexdigest()
    _os.makedirs(_os.path.join(cas, oid[:2]), exist_ok=True)
    open(_os.path.join(cas, oid[:2], oid[2:]), "w").write(body)
    _os.makedirs(_os.path.join(d, "env_end"))
    _j.dump({"added": ["tests/test_new.py"], "modified": [], "deleted": []},
            open(_os.path.join(d, "delta.json"), "w"))
    _j.dump({"meta": {}, "entries": [
        {"path": "tests/test_new.py", "type": "file", "status": "present",
         "untracked": True, "content_hash": oid}]},
        open(_os.path.join(d, "env_end", "manifest.json"), "w"))
    got = _added_test_names(d, {"cas_root": cas})
    if got != {"test_brand_new", "test_async_new"}:
        fails.append("new-file authored tests not found: %r" % sorted(got))
    else:
        print("[ok] authored tests are read from a newly CREATED file, not just diffs")

    # a new file that is NOT a test file must not contribute names
    _j.dump({"added": ["src/helper.py"], "modified": [], "deleted": []},
            open(_os.path.join(d, "delta.json"), "w"))
    _j.dump({"meta": {}, "entries": [
        {"path": "src/helper.py", "type": "file", "status": "present",
         "untracked": True, "content_hash": oid}]},
        open(_os.path.join(d, "env_end", "manifest.json"), "w"))
    if _added_test_names(d, {"cas_root": cas}):
        fails.append("a non-test new file contributed authored test names")
    else:
        print("[ok] a new non-test file contributes nothing")
    _sh.rmtree(d, ignore_errors=True)

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
