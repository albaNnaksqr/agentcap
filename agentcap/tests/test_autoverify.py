"""Verification timeliness: hashes are checked while base_sha is still alive.
end_session auto-verifies (verify.json persists at close), a later join refreshes
benchmark eligibility without re-reconstructing, and reopen invalidates the stale
report. Run with
    python3 -m agentcap.tests.test_autoverify
"""
import json
import os
import subprocess
import sys
import tempfile

from agentcap import join as J
from agentcap import session as sess


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def git(repo, *a):
    sh("git", "-C", repo, *a)


def write(root, rel, content):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-av-")
    repo = os.path.join(tmp, "repo")
    root = os.path.join(tmp, "store")
    os.makedirs(repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    write(repo, "a.py", "A = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")

    fails = []

    # --- close auto-verifies: verify.json exists without any manual verify-session ---
    sid, _ = sess.start_session(repo, agent="manual", root=root)
    write(repo, "a.py", "A = 2\n")
    sess.end_session(session_id=sid, root=root)
    sdir = os.path.join(root, "sessions", sid)
    vp = os.path.join(sdir, "verify.json")
    if not os.path.exists(vp):
        fails.append("end_session did not auto-verify")
    else:
        rep = json.load(open(vp))
        if not rep["verified"]:
            fails.append("auto-verify at close should verify a clean capture: %s" % rep)
        elif rep["benchmark_eligible"]:
            fails.append("no join yet -> must not be benchmark_eligible")
        else:
            print("[ok] end_session auto-verifies; eligible stays False pre-join")

    # --- a later join refreshes eligibility in the persisted report ---
    J.set_join(sid, {"agent": "manual", "session_id": sid}, confidence="high", root=root)
    rep = json.load(open(vp))
    if rep.get("join_confidence") != "high" or not rep["benchmark_eligible"]:
        fails.append("set_join did not refresh persisted eligibility: %s"
                     % {k: rep.get(k) for k in ("join_confidence", "benchmark_eligible")})
    else:
        print("[ok] later join refreshes verify.json eligibility (no re-reconstruction)")

    # --- reopen invalidates the stale report (env_end will be superseded) ---
    sess.reopen_session(sid, root=root)
    if os.path.exists(vp):
        fails.append("reopen left a stale verify.json behind")
    else:
        print("[ok] reopen removes the stale verify.json")

    # --- re-close: auto-verify recomputes against the new env_end ---
    write(repo, "a.py", "A = 3\n")
    sess.end_session(session_id=sid, root=root)
    if not os.path.exists(vp) or not json.load(open(vp))["verified"]:
        fails.append("re-close did not auto-verify the new env_end")
    else:
        print("[ok] re-close auto-verifies the recomputed env_end")

    # --- the base_sha-rot scenario auto-verify exists to beat: verifying AFTER the
    #     base commit is gone fails; the close-time report already banked the result ---
    banked = json.load(open(vp))
    git(repo, "checkout", "-q", "--detach")
    git(repo, "commit", "-q", "--amend", "-m", "rewritten")   # orphan the old sha
    git(repo, "checkout", "-q", "-B", "master")
    git(repo, "reflog", "expire", "--expire=now", "--all")
    git(repo, "gc", "-q", "--prune=now", "--aggressive")
    try:
        late = sess.verify_session(sid, root=root)
        late_ok = late["verified"]
    except Exception:
        late_ok = False
    if late_ok:
        # gc kept the sha reachable somehow; the scenario didn't arm — not a failure
        print("[--] base sha survived gc; rot scenario not exercised")
    elif not banked["verified"]:
        fails.append("banked close-time verification lost")
    else:
        print("[ok] late verify fails after history rewrite; close-time report banked")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
