"""Join test (step 5): confidence tiers (session_id->high, cwd+time->medium,
cwd-only->low, nothing->unjoined) and that benchmark eligibility now requires
join=high. Run with:  python3 -m agentcap.tests.test_join
"""
import os
import subprocess
import sys
import tempfile
import time

from agentcap import session as sess
from agentcap import join as J
from agentcap.trajectory import TrajectorySource


def sh(*a):
    subprocess.run(a, check=True, capture_output=True)


def make_repo(path):
    os.makedirs(path)
    sh("git", "-C", path, "init", "-q")
    sh("git", "-C", path, "config", "user.email", "t@t")
    sh("git", "-C", path, "config", "user.name", "t")
    with open(os.path.join(path, "f.py"), "w") as f:
        f.write("x=1\n")
    sh("git", "-C", path, "add", "-A")
    sh("git", "-C", path, "commit", "-qm", "base")


class FakeSource(TrajectorySource):
    def __init__(self, trajs):
        self._t = trajs

    def trajectories(self):
        return self._t


def main():
    tmp = tempfile.mkdtemp(prefix="agentcap-join-")
    repo = os.path.join(tmp, "repo")
    store = os.path.join(tmp, "store")
    make_repo(repo)
    fails = []

    # a watcher-style session (has agent_session_id) so session_id join is possible
    sid, s = sess.start_session(
        repo, agent="claude", confidence="high", root=store,
        session_id="claude-SESS", extra={"agent_session_id": "SESS", "log_path": "/l"})
    sess.end_session(session_id=sid, confidence="high", root=store)
    s = [x for x in sess.list_sessions(store) if x["session_id"] == sid][0]

    # --- scoring tiers (pure) ---
    t_hi = {"agent": "claude", "session_id": "SESS", "cwd": repo,
            "log_path": "/x", "first_ts": 0, "last_ts": 0, "n_steps": 5}
    if J.score(s, t_hi)[0] != "high":
        fails.append("session_id match should be high: %s" % (J.score(s, t_hi),))

    t_med = {"agent": "codex", "session_id": "OTHER", "cwd": repo, "log_path": "/y",
             "first_ts": sess_epoch(s, "created_at"), "last_ts": sess_epoch(s, "closed_at"),
             "n_steps": 3}
    if J.score(s, t_med)[0] != "medium":
        fails.append("cwd+time should be medium: %s" % (J.score(s, t_med),))

    t_low = {"agent": "codex", "session_id": "OTHER", "cwd": repo, "log_path": "/z",
             "first_ts": 1, "last_ts": 2, "n_steps": 1}   # cwd only, no time overlap
    if J.score(s, t_low)[0] != "low":
        fails.append("cwd-only should be low: %s" % (J.score(s, t_low),))

    t_none = {"agent": "codex", "session_id": "OTHER", "cwd": "/elsewhere",
              "log_path": "/w", "first_ts": 1, "last_ts": 2, "n_steps": 1}
    if J.score(s, t_none)[0] is not None:
        fails.append("no signal should be unjoined: %s" % (J.score(s, t_none),))
    if not fails:
        print("[ok] confidence tiers: high / medium / low / unjoined")

    # --- join_session picks the strongest (session_id beats cwd+time) ---
    best = J.join_session(s, [t_med, t_low, t_hi])
    if best["join_confidence"] != "high" or "session_id" not in best["signals"]:
        fails.append("join_session did not prefer session_id: %s" % best)
    else:
        print("[ok] join_session prefers the strongest signal")

    # --- eligibility: high/high env is NOT benchmark-eligible until a high join exists ---
    rep0 = sess.verify_session(sid, root=store)
    if rep0["benchmark_eligible"]:
        fails.append("eligible before any join")
    J.join_all(FakeSource([t_hi]), root=store)
    rep1 = sess.verify_session(sid, root=store)
    if not rep1["benchmark_eligible"] or rep1["join_confidence"] != "high":
        fails.append("not eligible after high join: %s" % rep1)
    else:
        print("[ok] benchmark eligibility requires join=high (gated correctly)")

    # a low join must NOT make it eligible
    J.join_all(FakeSource([t_low]), root=store)
    rep2 = sess.verify_session(sid, root=store)
    if rep2["benchmark_eligible"]:
        fails.append("low join wrongly made it eligible")
    else:
        print("[ok] low join stays fuel-only (not benchmark-eligible)")

    print("-" * 50)
    if fails:
        print("FAIL (%d):" % len(fails))
        for f in fails:
            print("  - " + f)
        sys.exit(1)
    print("ALL PASS")


def sess_epoch(s, key):
    from agentcap.trajectory import epoch
    return epoch(s.get(key))


if __name__ == "__main__":
    main()
