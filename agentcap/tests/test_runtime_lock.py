"""The runtime half of "reproducible elsewhere".

agentcap shipped gigabytes of repo tree and omitted the ~1.4 KB that makes the
tree runnable: 17 of 20 verified instances only earned `verified` through a venv
path that exists on one machine, while `portability: self_contained` — a field
about the git artifact — read like a claim about the whole instance.

Verified 2026-08-18: a fresh venv built from nothing but this lock re-earned
red_green on all 14 litellm sessions with identical pass_to_pass counts. So the
lock is worth capturing; these tests pin what may and may not be promised.

Run with:  python3 -m agentcap.tests.test_runtime_lock
"""
import os
import shutil
import subprocess
import sys
import tempfile

from agentcap import replay as R


def main():
    fails = []
    tmp = tempfile.mkdtemp(prefix="agentcap-lock-")
    try:
        # ---- what may be promised, by how the interpreter was provisioned ----
        venv = os.path.join(tmp, "v")
        os.makedirs(os.path.join(venv, "bin"))
        open(os.path.join(venv, "pyvenv.cfg"), "w").write("home = /usr\n")
        conda = os.path.join(tmp, "c")
        os.makedirs(os.path.join(conda, "bin"))
        os.makedirs(os.path.join(conda, "conda-meta"))
        for label, py, want in [
            ("venv (pyvenv.cfg)", os.path.join(venv, "bin", "python"), "venv"),
            ("conda (conda-meta)", os.path.join(conda, "bin", "python"), "conda"),
            ("distro python", "/usr/bin/python3", "system"),
        ]:
            got = R._runtime_kind(py)
            if got != want:
                fails.append("kind: %s -> %r (wanted %r)" % (label, got, want))
            else:
                print("[ok] kind: %s -> %s" % (label, want))

        # A distro python must NOT get a lock. A freeze of it describes the host;
        # it cannot rebuild one, and promising otherwise is the exact over-claim
        # this whole change exists to remove.
        text, info = R._runtime_lock("/usr/bin/python3")
        if text is not None or info["kind"] != "system":
            fails.append("a system interpreter was given a lock: %r" % info)
        elif not info.get("unreproducible_reason"):
            fails.append("no reason given for refusing a system interpreter")
        else:
            print("[ok] a distro python yields no lock, kind=system, reason stated")

        # conda: a pip freeze of it does not install -- proven by a consumer on
        # 2026-08-19, so it must not be shipped as a recipe either
        cpy = os.path.join(conda, "bin", "python")
        text, info = R._runtime_lock(cpy)
        if text is not None:
            fails.append("a conda env was given a lock: %r" % info)
        elif not info.get("unreproducible_reason"):
            fails.append("no reason given for refusing a conda env")
        else:
            print("[ok] a conda env yields no lock, reason stated")

        # ---- the unpinnable filter -------------------------------------------
        cases = [
            ("editable", "-e git+https://github.com/x/y@abc#egg=y", True),
            ("local file url", "y @ file:///home/u/y", True),
            ("direct git url", "y @ git+https://github.com/x/y", True),
            ("local path", "y @ /home/u/wheels/y.whl", True),
            ("ordinary pin", "pytest==9.1.1", False),
            ("extras pin", "uvicorn[standard]==0.34.0", False),
        ]
        for label, line, should_drop in cases:
            dropped = bool(R._UNPINNABLE.search(line))
            if dropped != should_drop:
                fails.append("unpinnable: %s -> dropped=%s (wanted %s)"
                             % (label, dropped, should_drop))
        if not any(f.startswith("unpinnable") for f in fails):
            print("[ok] editable / file:// / git+ / local-path lines are excluded, "
                  "ordinary and extras pins kept")

        # ---- end to end on a real venv ---------------------------------------
        real = os.path.join(tmp, "real")
        subprocess.run([sys.executable, "-m", "venv", real], check=True,
                       capture_output=True)
        py = os.path.join(real, "bin", "python")
        text, info = R._runtime_lock(py)
        if info["kind"] != "venv":
            fails.append("a real venv was not recognised: %r" % info)
        elif text is not None and (info["sha256"] is None or info["packages"] < 1):
            fails.append("lock produced without a digest or count: %r" % info)
        elif text is not None and any(R._UNPINNABLE.search(l) for l in text.splitlines()):
            fails.append("an unpinnable line survived into the lock")
        else:
            # a bare venv can legitimately be empty -> (None, kind=venv)
            print("[ok] real venv: kind=venv, packages=%d, digest=%s"
                  % (info["packages"], info["sha256"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fails += guards_and_bundles()

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")




def guards_and_bundles():
    """The two defects an independent consumer found on 2026-08-19, plus the conda
    one. Kept in this file because all three are about not promising more than the
    artifact can deliver."""
    import json as _json
    from agentcap.export import _apply_regression_spec, _assert_bundle_usable
    fails = []

    G = "tests/t.py::TestX::test_guard"
    F = "tests/t.py::TestX::test_fix"

    # A guard was in BOTH fail_to_pass and pass_to_pass. No consumer can honour
    # that: the standard rule is "every FAIL_TO_PASS fails before the patch".
    task = {"fail_to_pass": [F, G], "pass_to_pass": []}
    _apply_regression_spec(task, {"regression_scope": "tests", "regression_reason": None,
                                  "pass_to_pass": [G, "tests/t.py::other"],
                                  "regressions": [], "guards_in_ftp": [G]})
    if G in task["fail_to_pass"]:
        fails.append("guard still in fail_to_pass")
    elif task["fail_to_pass"] != [F]:
        fails.append("fail_to_pass wrong after the move: %r" % task["fail_to_pass"])
    elif G not in task["pass_to_pass"]:
        fails.append("guard did not land in pass_to_pass")
    elif task.get("guards_moved_from_ftp") != [G]:
        fails.append("the move was not recorded")
    else:
        print("[ok] a guard moves out of fail_to_pass into pass_to_pass, recorded")

    # the witness must survive a sweep that produced nothing
    task = {"fail_to_pass": [F, G], "pass_to_pass": []}
    _apply_regression_spec(task, {"regression_scope": "tests",
                                  "regression_reason": "no_tests_collected",
                                  "pass_to_pass": [G], "regressions": [],
                                  "guards_in_ftp": [G]})
    if G not in task.get("pass_to_pass", []):
        fails.append("guard lost when the sweep collected nothing")
    else:
        print("[ok] the guard witness survives a failed sweep")

    # never empty the spec, even if every candidate looks like a guard
    task = {"fail_to_pass": [G], "pass_to_pass": []}
    _apply_regression_spec(task, {"regression_scope": None, "regression_reason": None,
                                  "pass_to_pass": [], "regressions": [],
                                  "guards_in_ftp": [G]})
    if not task["fail_to_pass"]:
        fails.append("fail_to_pass was emptied")
    else:
        print("[ok] fail_to_pass is never emptied by the move")

    # A bundle the producer wrote successfully but no consumer can clone must be
    # rejected here rather than shipped (sglang-omni#1360 shipped one for 11 days).
    tmp = tempfile.mkdtemp(prefix="agentcap-bundletest-")
    try:
        junk = os.path.join(tmp, "broken.bundle")
        open(junk, "wb").write(b"# v2 git bundle\nnot really a bundle\n")
        try:
            _assert_bundle_usable(junk, ["0" * 40])
            fails.append("an unusable bundle passed the check")
        except RuntimeError:
            print("[ok] an unusable bundle is rejected instead of shipped")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return fails


if __name__ == "__main__":
    main()
