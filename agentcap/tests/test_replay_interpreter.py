"""Recorded-interpreter recovery in replay.

`replay` defaults to a clean pytest-only interpreter and falls back to the one
the session actually used only when the clean one collects nothing. That fallback
is the only way a repo with third-party deps ever earns `verified`, and it was a
silent no-op whenever the agent wrote the interpreter with a shell variable --
`_PY_RE` was anchored at `/`, so `"$HOME/.../bin/pytest"` matched nothing and the
report just showed every test `missing` with no fallback reason.

Run with:  python3 -m agentcap.tests.test_replay_interpreter
"""
import os
import sys

from agentcap import replay as R


def seed_with(*cmds):
    return {"evidence": [{"cmd": c} for c in cmds]}


def main():
    fails = []
    # The real interpreter on this box, used as the thing that must be found.
    venv = "/home/kps_spark/workspace/osmind-repos/.venv-litellm"
    have_venv = os.path.exists(os.path.join(venv, "bin", "python3"))

    # 1. the regex itself: all four spellings must yield a bin/ dir
    cases = {
        "/abs": '/opt/v/bin/pytest -q',
        "$VAR": '"$HOME/wt/.venv/bin/pytest" -q',
        "${VAR}": '"${HOME}/wt/.venv/bin/python3" -m pytest',
        "~": '~/wt/.venv/bin/python -m pytest',
    }
    for label, cmd in cases.items():
        if not R._PY_RE.findall(cmd):
            fails.append("_PY_RE misses the %s spelling: %r" % (label, cmd))
    if not fails:
        print("[ok] _PY_RE matches absolute, $VAR, ${VAR} and ~ spellings")

    # 2. expansion + the guards that make the loose regex safe
    if R._resolve_bindir("$HOME/x/bin/") != os.path.expanduser("~/x/bin/"):
        fails.append("$HOME not expanded")
    elif R._resolve_bindir("~/x/bin/") != os.path.expanduser("~/x/bin/"):
        fails.append("~ not expanded")
    elif R._resolve_bindir("venv/bin/") is not None:
        fails.append("relative bindir accepted; it would resolve against agentcap's cwd")
    elif R._resolve_bindir("$AGENTCAP_NO_SUCH_VAR_/bin/") is not None:
        fails.append("unset variable produced a usable path")
    else:
        print("[ok] expansion works and relative / unset-var paths are rejected")

    # 3. end to end: a $HOME-spelled command must recover a real interpreter.
    #    This is the case that silently produced no fallback at all.
    if have_venv:
        rel = os.path.relpath(venv, os.path.expanduser("~"))
        got = R._interpreter_from_seed(seed_with('"$HOME/%s/bin/pytest" -q tests/' % rel))
        if not got or not os.path.exists(got):
            fails.append("$HOME-spelled command recovered no interpreter: %r" % got)
        else:
            print("[ok] $HOME-spelled command recovers %s" % got)
    else:
        print("[skip] end-to-end: %s not present on this box" % venv)

    # 4. a command with no interpreter at all still returns None (no guessing)
    if R._interpreter_from_seed(seed_with("git check-ignore tests/a.py")) is not None:
        fails.append("invented an interpreter from a command that names none")
    else:
        print("[ok] a command naming no interpreter yields None")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
