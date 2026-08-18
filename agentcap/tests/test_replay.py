"""Replay gate: reconstruct the exportable artifact and re-run fail_to_pass.
verified is EARNED: end all-green AND start(+test patch) all-red. Run with
    python3 -m agentcap.tests.test_replay
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from agentcap import replay as R
from agentcap import export as E
from agentcap import session as sess
from agentcap.tests.test_export import build, claude_log  # fixture factories

MOD_RED = "def f():\n    return 0\n"
MOD_GREEN = "def f():\n    return 1\n"
TEST_F = "import mod\n\ndef test_f():\n    assert mod.f() == 1\n"
NODE = "tests/test_mod.py::test_f"
PROMPT = "make test_f pass"
RED_OUT = "%s FAILED\nE   AssertionError\n=== 1 failed in 0.1s ===" % NODE
GREEN_OUT = "%s PASSED\n=== 1 passed in 0.1s ===" % NODE
EVENTS = [("user", PROMPT), ("bash", "python -m pytest -q", RED_OUT),
          ("edit", "mod.py"), ("bash", "python -m pytest -q", GREEN_OUT)]


def seeded(tmp, name, files, edits):
    """build() + taskseed so replay has a seed to chew on."""
    from agentcap import taskseed as T
    repo, store, sid = build(tmp, name, files, edits, EVENTS)
    sdir = os.path.join(store, "sessions", sid)
    seed = T.extract_seed(sdir)
    assert seed and seed["candidate_fail_to_pass"] == [NODE], "fixture seed broken: %s" % seed
    return repo, store, sid, sdir


def main():
    fails = []

    # --- grading: pure outcome logic ---
    rg = R._grade([{"node_id": "t.py::a", "end_status": "passed", "start_status": "failed"}])
    if rg != ("red_green", True, None):
        fails.append("red_green grade wrong: %s" % (rg,))
    go = R._grade([{"node_id": "t.py::a", "end_status": "passed", "start_status": "passed"}])
    if go[:2] != ("green_only", False):
        fails.append("green_only grade wrong: %s" % (go,))
    ng = R._grade([{"node_id": "t.py::a", "end_status": "failed", "start_status": None}])
    if ng[:2] != ("not_green", False):
        fails.append("not_green grade wrong: %s" % (ng,))
    # collection error at start still demonstrates "not passing" -> red ok
    ce = R._grade([{"node_id": "t.py::a", "end_status": "passed", "start_status": "error"}])
    if ce != ("red_green", True, None):
        fails.append("start error should count as red: %s" % (ce,))
    # id missing at end is excluded; remaining ids gate
    mx = R._grade([{"node_id": "t.py::gone", "end_status": "missing", "start_status": None},
                   {"node_id": "t.py::a", "end_status": "passed", "start_status": "failed"}])
    if mx != ("red_green", True, None):
        fails.append("missing-at-end should be excluded: %s" % (mx,))
    # nothing runnable -> setup_failed
    nr = R._grade([{"node_id": "t.py::gone", "end_status": "missing", "start_status": None}])
    if nr[0] != "setup_failed" or nr[1] or nr[2] != "no_runnable_tests":
        fails.append("no runnable ids should be setup_failed: %s" % (nr,))
    if not fails:
        print("[ok] grading: red_green/green_only/not_green/excluded/none-runnable")

    tmp = tempfile.mkdtemp(prefix="agentcap-replay-")
    py = R._ensure_python(tmp)

    # --- S1 red_green: real fix, start red -> end green, verified earned ---
    files = {"mod.py": MOD_RED, "tests/test_mod.py": TEST_F}
    repo1, store1, sid1, sdir1 = seeded(tmp, "rg", files, {"mod.py": MOD_GREEN})
    rep = R.replay_session(sdir1, python=py)
    if rep["outcome"] != "red_green" or not rep["verified"]:
        fails.append("S1 should be red_green: %s / %s" % (rep["outcome"], rep.get("error")))
    elif not os.path.exists(os.path.join(sdir1, "replay.json")):
        fails.append("S1 replay.json not persisted")
    else:
        row = next(t for t in rep["tests"] if t["node_id"] == NODE)
        if row["end_status"] != "passed" or row["start_status"] == "passed":
            fails.append("S1 per-test rows wrong: %s" % row)
        else:
            print("[ok] S1 red_green: verified earned, per-test rows recorded")

    # --- S2 green_only: no real change, test passed all along ---
    files_g = {"mod.py": MOD_GREEN, "tests/test_mod.py": TEST_F}
    _, _, _, sdir2 = seeded(tmp, "go", files_g, {"README.md": "x\n"})
    rep2 = R.replay_session(sdir2, python=py)
    if rep2["outcome"] != "green_only" or rep2["verified"]:
        fails.append("S2 should be green_only: %s" % rep2["outcome"])
    else:
        print("[ok] S2 green_only: task invalid, verified withheld")

    # --- S3 not_green: end state still fails (e.g. uncaptured fixture dep) ---
    _, _, _, sdir3 = seeded(tmp, "ng", files, {"README.md": "x\n"})
    rep3 = R.replay_session(sdir3, python=py)
    if rep3["outcome"] != "not_green" or rep3["verified"]:
        fails.append("S3 should be not_green: %s" % rep3["outcome"])
    else:
        print("[ok] S3 not_green: unreplayable end state caught")

    # --- S4 setup_failed: source repo gone, bundle cannot be built ---
    repo4, store4, sid4, sdir4 = seeded(tmp, "sf", files, {"mod.py": MOD_GREEN})
    shutil.rmtree(repo4)
    rep4 = R.replay_session(sdir4, python=py)
    if rep4["outcome"] != "setup_failed" or rep4["verified"] or not rep4.get("error"):
        fails.append("S4 should be setup_failed with error: %s" % rep4)
    else:
        print("[ok] S4 setup_failed: infrastructure failure graded, error recorded")

    # --- S5 TDD: fail_to_pass test authored mid-session, absent at base ---
    files_tdd = {"mod.py": MOD_RED}          # no test file at start
    edits_tdd = {"mod.py": MOD_GREEN, "tests/test_mod.py": TEST_F}
    _, _, _, sdir5 = seeded(tmp, "tdd", files_tdd, edits_tdd)
    rep5 = R.replay_session(sdir5, python=py)
    if rep5["outcome"] != "red_green" or not rep5["verified"]:
        fails.append("S5 TDD overlay should verify: %s / %s"
                     % (rep5["outcome"], rep5.get("error")))
    elif "tests/test_mod.py" not in rep5["overlaid_test_files"]:
        fails.append("S5 test file not overlaid: %s" % rep5["overlaid_test_files"])
    else:
        print("[ok] S5 TDD: end-authored test overlaid onto base, red verified")

    # --- S6 provenance: a bundle-able repo must stay self_contained ---
    if rep.get("artifact_source") != "bundle" or rep.get("artifact_portability") != "self_contained":
        fails.append("S6 healthy repo should replay from a bundle: %s/%s"
                     % (rep.get("artifact_source"), rep.get("artifact_portability")))
    elif rep.get("artifact_fallback_reason") is not None:
        fails.append("S6 no fallback reason expected: %s" % rep["artifact_fallback_reason"])
    else:
        print("[ok] S6 provenance: bundle path recorded as self_contained")

    # --- S7 opt-in gate: an unbundleable source must NOT silently fall back ---
    repo7, store7, sid7, sdir7 = seeded(tmp, "loc", files, {"mod.py": MOD_GREEN})
    real_bundle = R._bundle

    def boom(*_a, **_k):
        raise RuntimeError("HTTP 413 from promisor remote")

    real_tree0 = R._tree_snapshot
    R._bundle = boom
    R._tree_snapshot = boom          # no self-contained tier available either
    try:
        rep7 = R.replay_session(sdir7, python=py)          # no allow_local_repo
    finally:
        R._bundle, R._tree_snapshot = real_bundle, real_tree0
    if rep7["outcome"] != "setup_failed" or rep7["verified"]:
        fails.append("S7 must NOT fall back without opt-in: %s" % rep7["outcome"])
    elif "413" not in (rep7.get("error") or ""):
        fails.append("S7 bundle failure must surface as the error: %s" % rep7.get("error"))
    elif rep7.get("artifact_source") is not None:
        fails.append("S7 no artifact should be claimed: %s" % rep7["artifact_source"])
    else:
        print("[ok] S7 opt-in gate: unbundleable source fails loudly by default")

    # --- S8a unbundleable but archivable -> tree snapshot, still self_contained ---
    repo8a, store8a, sid8a, sdir8a = seeded(tmp, "tree", files, {"mod.py": MOD_GREEN})
    R._bundle = boom
    try:
        rep8a = R.replay_session(sdir8a, python=py)      # no opt-in needed
    finally:
        R._bundle = real_bundle
    if rep8a["outcome"] != "red_green" or not rep8a["verified"]:
        fails.append("S8a tree snapshot should still earn red_green: %s / %s"
                     % (rep8a["outcome"], rep8a.get("error")))
    elif rep8a["artifact_source"] != "tree_snapshot":
        fails.append("S8a should degrade to a tree snapshot: %s" % rep8a["artifact_source"])
    elif rep8a.get("runtime_portability") not in ("same_class", "machine_local"):
        fails.append("S8a runtime_portability must always be stated, got %r"
                     % rep8a.get("runtime_portability"))
    elif rep8a["artifact_portability"] != "self_contained":
        fails.append("S8a history-free is still artifact-self_contained: %s" % rep8a["artifact_portability"])
    elif "413" not in (rep8a.get("artifact_fallback_reason") or ""):
        fails.append("S8a bundle failure not explained: %s"
                     % rep8a.get("artifact_fallback_reason"))
    else:
        print("[ok] S8a tree_snapshot: no history, still self_contained, verified")

    # --- S8 forced fallback: neither bundle nor archive -> machine_local ---
    repo8, store8, sid8, sdir8 = seeded(tmp, "loc2", files, {"mod.py": MOD_GREEN})
    real_tree = R._tree_snapshot
    R._bundle = boom
    R._tree_snapshot = boom
    try:
        rep8 = R.replay_session(sdir8, python=py, allow_local_repo=True)
    finally:
        R._bundle, R._tree_snapshot = real_bundle, real_tree
    if rep8["outcome"] != "red_green" or not rep8["verified"]:
        fails.append("S8 red/green must still be earned via local repo: %s" % rep8["outcome"])
    elif rep8["artifact_source"] != "local_repo" or rep8["artifact_portability"] != "machine_local":
        fails.append("S8 downgrade not recorded: %s/%s"
                     % (rep8["artifact_source"], rep8["artifact_portability"]))
    elif "413" not in (rep8.get("artifact_fallback_reason") or ""):
        fails.append("S8 fallback reason not recorded: %s" % rep8.get("artifact_fallback_reason"))
    else:
        print("[ok] S8 local_repo: verified earned, portability downgraded and explained")

    # --- S9 interpreter: clean-room default, recorded venv only as last resort ---
    seed9 = json.load(open(os.path.join(sdir8, "task_seed.json")))
    venv_bin = os.path.dirname(py)
    seed9["evidence"] = [{"cmd": "%s/pytest -q tests/test_mod.py" % venv_bin,
                          "counts": {}, "idx": 1}]
    with open(os.path.join(sdir8, "task_seed.json"), "w") as f:
        json.dump(seed9, f)
    if rep8.get("interpreter_source") != "explicit":
        fails.append("S9 explicit python should be labelled explicit: %s"
                     % rep8.get("interpreter_source"))
    else:
        # a collectable session must NOT be pushed onto the recorded interpreter
        rep9 = R.replay_session(sdir8, allow_local_repo=True)
        if rep9.get("interpreter_source") != "ambient":
            fails.append("S9 clean interpreter must win when it works: %s"
                         % rep9.get("interpreter_source"))
        elif rep9["outcome"] != "red_green":
            fails.append("S9 ambient run regressed: %s" % rep9["outcome"])
        else:
            print("[ok] S9 interpreter: clean-room default kept for collectable ids")

    # --- S10 interpreter fallback: clean one collects nothing -> labelled reuse ---
    _, _, _, sdir10 = seeded(tmp, "interp", files, {"mod.py": MOD_GREEN})
    # Shaped like a real venv: bin/python3 is a SYMLINK to the base interpreter.
    # Comparing realpath here would collapse it onto the ambient python and skip
    # the fallback entirely — the exact bug this pins.
    fake_bin = os.path.join(tmp, "session-venv", "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_py = os.path.join(fake_bin, "python3")
    if not os.path.lexists(fake_py):
        os.symlink(py, fake_py)
    if os.path.realpath(fake_py) != os.path.realpath(py):
        fails.append("S10 fixture should share a realpath with the ambient python")
    seed10 = json.load(open(os.path.join(sdir10, "task_seed.json")))
    seed10["evidence"] = [{"cmd": "%s/pytest -q tests/test_mod.py" % fake_bin,
                           "counts": {}, "idx": 1}]
    with open(os.path.join(sdir10, "task_seed.json"), "w") as f:
        json.dump(seed10, f)
    real_run = R._run_test
    dead = {"n": 0}

    def only_clean_is_blind(python_, tree, node_id, timeout_, pythonpath=None):
        """Simulate 'clean interpreter cannot import the repo's deps'. Keyed on
        the invocation path, like a venv's site-packages actually is."""
        if os.path.abspath(python_) == os.path.abspath(py):
            dead["n"] += 1
            return "missing"
        return real_run(python_, tree, node_id, timeout_, pythonpath)

    R._run_test = only_clean_is_blind
    try:
        rep10 = R.replay_session(sdir10)
    finally:
        R._run_test = real_run
    if not dead["n"]:
        fails.append("S10 fixture never exercised the clean interpreter")
    elif rep10.get("interpreter_source") != "session_recorded":
        fails.append("S10 should have fallen back: %s / %s"
                     % (rep10.get("interpreter_source"), rep10["outcome"]))
    elif not rep10.get("interpreter_fallback_reason"):
        fails.append("S10 fallback not explained")
    elif rep10["outcome"] != "red_green" or not rep10["verified"]:
        fails.append("S10 red/green should still be earned: %s" % rep10["outcome"])
    else:
        print("[ok] S10 interpreter fallback: uncollectable -> recorded venv, labelled")

    # --- S11 unittest ids: dotted targets are run by unittest, not pytest ---
    if R.runner_for("tests/t.py::test_x") != "pytest" or \
            R.runner_for("pkg.mod.Case.test_x") != "unittest":
        fails.append("runner_for misroutes ids")
    else:
        # a unittest.TestCase suite, discovered from tests/ -> ids are relative to
        # that dir, so the module is NOT importable from the repo root
        UT_TEST = ("import unittest\nfrom mod import f\n\n"
                   "class Case(unittest.TestCase):\n"
                   "    def test_f(self):\n        self.assertEqual(f(), 1)\n")
        UT_RED = ("test_f (test_mod.Case.test_f) ... FAIL\n"
                  "\nFAIL: test_f (test_mod.Case.test_f)\n"
                  "\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n")
        UT_GREEN = ("test_f (test_mod.Case.test_f) ... ok\n"
                    "\nRan 1 test in 0.001s\n\nOK\n")
        UT_NODE = "test_mod.Case.test_f"
        from agentcap import taskseed as T2
        from agentcap.tests.test_export import build as build2
        repo11, store11, sid11 = build2(
            tmp, "ut", {"mod.py": MOD_RED, "tests/test_mod.py": UT_TEST},
            {"mod.py": MOD_GREEN},
            [("user", "fix it"),
             ("bash", "python -m unittest discover -s tests -v", UT_RED),
             ("edit", "mod.py"),
             ("bash", "python -m unittest discover -s tests -v", UT_GREEN)])
        sdir11 = os.path.join(store11, "sessions", sid11)
        seed11 = T2.extract_seed(sdir11)
        if not seed11 or seed11["candidate_fail_to_pass"] != [UT_NODE]:
            fails.append("S11 fixture seed wrong: %s"
                         % (seed11 and seed11["candidate_fail_to_pass"]))
        else:
            rep11 = R.replay_session(sdir11, python=py)
            row = rep11["tests"][0] if rep11.get("tests") else {}
            if rep11["outcome"] != "red_green" or not rep11["verified"]:
                fails.append("S11 unittest session should verify: %s / %s"
                             % (rep11["outcome"], rep11.get("error")))
            elif row.get("runner") != "unittest":
                fails.append("S11 runner not recorded: %s" % row)
            elif row.get("end_status") != "passed" or row.get("start_status") != "failed":
                fails.append("S11 per-id statuses wrong: %s" % row)
            else:
                print("[ok] S11 unittest: dotted id run via unittest, red_green earned")

        # a dotted id that exists nowhere in the tree stays missing, never invented
        if R._run_test(py, repo11, "no.such.Case.test_x", 60) != "missing":
            fails.append("S11 unknown dotted id should be missing")
        elif R._unittest_root(repo11, UT_NODE) != "tests":
            fails.append("S11 import root should be tests/: %s"
                         % R._unittest_root(repo11, UT_NODE))
        else:
            print("[ok] S11 import root derived from the tree; unknown id stays missing")

    # --- S12 disposable worktree: capture in one, delete it, still replayable ---
    parent = os.path.join(tmp, "wtparent")
    os.makedirs(parent)
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", parent] + a, check=True, capture_output=True)
    for rel, c in {"mod.py": MOD_RED, "tests/test_mod.py": TEST_F}.items():
        p = os.path.join(parent, rel)
        os.makedirs(os.path.dirname(p) or parent, exist_ok=True)
        open(p, "w").write(c)
    for a in (["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", parent] + a, check=True, capture_output=True)
    wt = os.path.join(tmp, "throwaway-wt")
    subprocess.run(["git", "-C", parent, "worktree", "add", "-q", "--detach", wt],
                   check=True, capture_output=True)

    store12 = os.path.join(tmp, "wt_store")
    from agentcap import taskseed as T3
    from agentcap import join as J3
    sid12, s12 = sess.start_session(wt, agent="claude", root=store12, session_id="claude-W",
                                    extra={"agent_session_id": "W", "log_path": "/x"})
    if s12.get("repo_object_source") != os.path.realpath(parent) and \
            s12.get("repo_object_source") != parent:
        fails.append("S12 parent repo not recorded: %s" % s12.get("repo_object_source"))
    open(os.path.join(wt, "mod.py"), "w").write(MOD_GREEN)
    sess.end_session(session_id=sid12, root=store12)
    log12 = os.path.join(tmp, "wt.jsonl")
    claude_log(log12, wt, EVENTS)
    J3.set_join(sid12, {"agent": "claude", "session_id": "W", "cwd": wt, "log_path": log12,
                        "first_ts": 0, "last_ts": 0, "n_steps": len(EVENTS)},
                confidence="high", root=store12)
    sdir12 = os.path.join(store12, "sessions", sid12)
    T3.extract_seed(sdir12)
    subprocess.run(["git", "-C", parent, "worktree", "remove", "--force", wt],
                   check=True, capture_output=True)      # the harness throws it away
    if os.path.exists(wt):
        fails.append("S12 fixture: worktree not actually gone")
    else:
        rep12 = R.replay_session(sdir12, python=py)
        if rep12["outcome"] != "red_green" or not rep12["verified"]:
            fails.append("S12 deleted worktree should still replay: %s / %s"
                         % (rep12["outcome"], rep12.get("error")))
        elif "worktree is gone" not in (rep12.get("artifact_fallback_reason") or ""):
            fails.append("S12 fallback to the parent repo not recorded: %s"
                         % rep12.get("artifact_fallback_reason"))
        else:
            print("[ok] S12 disposable worktree: replays from the recorded parent repo")

    # a session with neither the worktree nor a recorded parent must fail loudly
    s12_no = json.load(open(os.path.join(sdir12, "session.json")))
    s12_no["repo_object_source"] = None
    with open(os.path.join(sdir12, "session.json"), "w") as f:
        json.dump(s12_no, f)
    rep12b = R.replay_session(sdir12, python=py)
    if rep12b["outcome"] != "setup_failed" or "no usable object source" not in (
            rep12b.get("error") or ""):
        fails.append("S12 legacy session without a parent must fail loudly: %s" % rep12b)
    else:
        print("[ok] S12 no worktree and no recorded parent -> loud setup_failed")

    # --- replay_all: batch shape over the store ---
    results = R.replay_all(root=store1, python=py)
    if results.get(sid1, {}).get("outcome") != "red_green":
        fails.append("replay_all should reuse/recompute S1: %s" % results)
    else:
        print("[ok] replay_all: batch summary over the store")

    # --- export integration: verified flows from replay.json ---
    out1 = os.path.join(tmp, "out_rg")
    E.export_all(root=store1, out=out1)
    task1 = json.load(open(os.path.join(out1, sid1, "task.json")))
    rec1 = json.load(open(os.path.join(out1, sid1, "record.json")))
    if not task1["verified"] or task1.get("replay_outcome") != "red_green":
        fails.append("export should carry earned verified: %s / %s"
                     % (task1["verified"], task1.get("replay_outcome")))
    elif (rec1.get("replay") or {}).get("outcome") != "red_green":
        fails.append("record should embed replay outcome: %s" % rec1.get("replay"))
    else:
        print("[ok] export: verified earned via replay, embedded in task+record")

    if fails:
        print("\n".join("[FAIL] " + f for f in fails))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
