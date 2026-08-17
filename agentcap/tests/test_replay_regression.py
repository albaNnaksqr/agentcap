"""The regression half of the verifier spec.

Until now pass_to_pass was mined from the session log and was empty for all 50
seeds in the store -- agents run `pytest -q`, which prints dots and no node ids,
so the names were never written down anywhere. `verified` therefore meant only
"the new tests went red->green" and said nothing about whether the change broke
anything. Replay now produces the set itself from the reconstructed trees.

Run with:  python3 -m agentcap.tests.test_replay_regression
"""
import os
import shutil
import subprocess
import sys
import tempfile

from agentcap import replay as R
from agentcap.replay import _grade
from agentcap.export import _apply_regression_spec


def write(tree, rel, body):
    p = os.path.join(tree, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(body)


def main():
    fails = []

    # ---- scope selection -------------------------------------------------
    cases = [
        ("sibling dirs -> their common parent",
         ["tests/test_litellm/llms/azure/image_edit/test_a.py",
          "tests/test_litellm/llms/azure/image_generation/test_b.py"],
         "tests/test_litellm/llms/azure"),
        ("one file -> its own directory",
         ["tests/test_litellm/llms/azure/test_a.py"], "tests/test_litellm/llms/azure"),
        # `tests/` alone means "the whole suite"; on a repo whose full suite does
        # not even collect that is a guaranteed timeout, so refuse instead
        ("too shallow -> None", ["tests/test_a.py", "tests/test_b.py"], None),
        ("unrelated trees -> None", ["a/x/test_a.py", "b/y/test_b.py"], None),
        ("no dir component -> None", ["test_a.py"], None),
        ("no test files -> None", [], None),
        ("None input -> None", None, None),
    ]
    for label, files, want in cases:
        got = R._regression_scope(files)
        if got != want:
            fails.append("scope: %s -> %r (wanted %r)" % (label, got, want))
        else:
            print("[ok] scope: %s" % label)

    # ---- the sweep actually produces names -------------------------------
    py = sys.executable
    if subprocess.run([py, "-c", "import pytest"], capture_output=True).returncode != 0:
        print("[skip] sweep: no pytest for %s" % py)
    else:
        tmp = tempfile.mkdtemp(prefix="agentcap-regr-")
        try:
            # END tree: test_keep passes, test_new passes (the fix landed)
            end = os.path.join(tmp, "end")
            write(end, "pkg/tests/test_keep.py", "def test_keep():\n    assert True\n")
            write(end, "pkg/tests/test_new.py", "def test_new():\n    assert True\n")
            got, ok = R._sweep(py, end, "pkg/tests", 120)
            names = {n.split("::")[-1] for n in got}
            if not ok or names != {"test_keep", "test_new"}:
                fails.append("sweep did not name both passing tests: %r" % sorted(got))
            else:
                print("[ok] sweep names the passing node ids (what -q could never give)")

            # START tree: test_keep passes, test_new fails (pre-fix), and
            # test_broken passes here but will be gone at END
            start = os.path.join(tmp, "start")
            write(start, "pkg/tests/test_keep.py", "def test_keep():\n    assert True\n")
            write(start, "pkg/tests/test_new.py", "def test_new():\n    assert False\n")
            write(start, "pkg/tests/test_broken.py", "def test_broken():\n    assert True\n")
            spass, ok_s = R._sweep(py, start, "pkg/tests", 120)
            epass, ok_e = R._sweep(py, end, "pkg/tests", 120)
            ftp = {n for n in epass if n.endswith("::test_new")}
            ptp = sorted((epass & spass) - ftp)
            regr = sorted((spass - epass) - ftp)
            if [n.split("::")[-1] for n in ptp] != ["test_keep"]:
                fails.append("pass_to_pass wrong: %r" % ptp)
            elif [n.split("::")[-1] for n in regr] != ["test_broken"]:
                fails.append("regression not caught: %r" % regr)
            else:
                print("[ok] pass-at-both -> pass_to_pass; passed-at-START-only -> regression")
                print("     (a test the session DELETED shows up as a regression, "
                      "which the task contract forbids)")
            # a failing test at START must not become a regression just by failing
            if any(n.endswith("::test_new") for n in regr):
                fails.append("the fail_to_pass test leaked into regressions")
            else:
                print("[ok] the fail_to_pass test is excluded from both sets")

            # timeout is reported, never silently treated as "nothing passes"
            got, ok = R._sweep(py, end, "pkg/tests", 0)
            if ok is not False:
                fails.append("a sweep timeout was not reported as incomplete")
            else:
                print("[ok] a sweep timeout reports completed=False, not an empty set")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- fallback scope when the directory collects nothing ---------------
    # A single test file sitting directly under a huge directory makes the
    # common ancestor that huge directory; on litellm that directory does not
    # collect and #36999 lost its whole guard to it.
    fb_cases = [
        ("two files -> both, space joined",
         {"test_files": ["tests/a/test_x.py", "tests/b/test_y.py"]}, "tests/a",
         "tests/a/test_x.py tests/b/test_y.py"),
        ("one file", {"test_files": ["tests/test_litellm/test_cost_calculator.py"]},
         "tests/test_litellm", "tests/test_litellm/test_cost_calculator.py"),
        ("no test files -> None", {"test_files": []}, "tests/x", None),
        # never retry the identical target -- that is a guaranteed second failure
        ("same as what already failed -> None",
         {"test_files": ["tests/a/test_x.py"]}, "tests/a/test_x.py", None),
    ]
    for label, seed, tried, want in fb_cases:
        got = R._regression_scope_fallback(seed, tried)
        if got != want:
            fails.append("fallback: %s -> %r (wanted %r)" % (label, got, want))
        else:
            print("[ok] fallback scope: %s" % label)

    # a too-shallow ancestor is not the end of it: the files themselves are a
    # valid target, and this is how the two scope_undeterminable sessions
    # (test_files = ['tests/test_swa_verifier.py'], dirname 'tests') get a guard
    shallow = {"test_files": ["tests/test_swa_verifier.py"]}
    if R._regression_scope(shallow["test_files"]) is not None:
        fails.append("a 1-segment ancestor should still be refused as a directory scope")
    elif R._regression_scope_fallback(shallow, None) != "tests/test_swa_verifier.py":
        fails.append("no file-level target for a too-shallow ancestor")
    else:
        print("[ok] too-shallow ancestor still yields a file-level target")

    # ---- grading: a guard is not counter-evidence -------------------------
    # The task contract asks for a guard covering neighbouring behaviour, and a
    # guard passes before the fix BY DEFINITION. Demanding every candidate be red
    # at START punished exactly that (litellm#37105 was graded green_only for
    # writing the guard its pack asked for).
    def t(end, start, name="x"):
        return {"node_id": "tests/t.py::%s" % name, "end_status": end, "start_status": start}
    grade_cases = [
        ("one real red->green + one guard -> red_green",
         [t("passed", "failed", "fix"), t("passed", "passed", "guard")], ("red_green", True)),
        ("all candidates green at START -> still green_only (the vacuous case)",
         [t("passed", "passed", "a"), t("passed", "passed", "b")], ("green_only", False)),
        ("classic all-red -> red_green", [t("passed", "failed")], ("red_green", True)),
        ("any END not green -> not_green", [t("failed", "failed")], ("not_green", False)),
        # missing/error at START keep counting as "not passed", unchanged
        ("missing at START still counts as red",
         [t("passed", "missing")], ("red_green", True)),
        ("nothing runnable at END -> setup_failed",
         [t("missing", None)], ("setup_failed", False)),
    ]
    for label, tests, want in grade_cases:
        outcome, verified, _ = _grade(tests)
        if (outcome, verified) != want:
            fails.append("grade: %s -> %r (wanted %r)" % (label, (outcome, verified), want))
        else:
            print("[ok] grade: %s" % label)

    # every empty guard must carry a reason -- this was the last unlabelled one
    for lbl, rep, want in [
        ("swept fine but the scope held only the ftp test",
         {"regression_scope": "tests/t.py", "regression_reason": "no_witnesses_outside_ftp",
          "pass_to_pass": [], "regressions": []}, "session_log"),
    ]:
        task = {"pass_to_pass": []}
        _apply_regression_spec(task, rep)
        if task.get("pass_to_pass_source") != want or not task.get("regression_reason"):
            fails.append("empty guard without a reason survived: %s -> %r" % (lbl, task))
        else:
            print("[ok] export spec: %s carries its reason" % lbl)

    # ---- export takes the set from replay, and labels an empty one -------
    cases = [
        ("swept and found witnesses -> replay's set wins",
         {"regression_scope": "tests/x", "regression_reason": None,
          "pass_to_pass": ["tests/x::a", "tests/x::b"], "regressions": []},
         {"pass_to_pass": ["tests/x::a", "tests/x::b"],
          "pass_to_pass_source": "replay_sweep", "regressions": []}),
        # an empty guard must say WHY, or it reads as a clean bill of health
        ("collected nothing -> seed's set kept, reason carried",
         {"regression_scope": "tests/x", "regression_reason": "no_tests_collected",
          "pass_to_pass": [], "regressions": []},
         {"pass_to_pass": ["seed_ptp"], "pass_to_pass_source": "session_log",
          "regression_reason": "no_tests_collected"}),
        ("no scope -> seed's set kept, reason carried",
         {"regression_scope": None, "regression_reason": "scope_undeterminable",
          "pass_to_pass": [], "regressions": []},
         {"pass_to_pass": ["seed_ptp"], "pass_to_pass_source": "session_log",
          "regression_scope": None}),
        ("regressions are shipped so a consumer sees WHY verified is false",
         {"regression_scope": "tests/x", "regression_reason": None,
          "pass_to_pass": ["tests/x::a"], "regressions": ["tests/x::broke"]},
         {"regressions": ["tests/x::broke"], "pass_to_pass_source": "replay_sweep"}),
    ]
    for label, rep, want in cases:
        task = {"pass_to_pass": ["seed_ptp"]}
        _apply_regression_spec(task, rep)
        bad = {k: (task.get(k), v) for k, v in want.items() if task.get(k) != v}
        if bad:
            fails.append("export spec: %s -> %r" % (label, bad))
        else:
            print("[ok] export spec: %s" % label)

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
