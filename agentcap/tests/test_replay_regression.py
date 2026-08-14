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

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
