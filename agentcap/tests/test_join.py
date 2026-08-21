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

    fails += gate_cases()

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




def gate_cases():
    """The cwd gate: an unrelated working directory disqualifies a candidate.

    Regression for 16 sessions paired across unrelated projects on time overlap
    alone (papyrus capture <- claimagent log, trajmill capture <- papyrus log),
    three of which reached an export.
    """
    from agentcap.join import score, _cwd_related
    fails = []
    T0, T1 = 1000, 2000

    def sess(repo):
        return {"repo": repo, "agent": "claude", "extra": {},
                "created_at": "2026-08-20T10:00:00+00:00",
                "closed_at": "2026-08-20T10:30:00+00:00"}

    def traj(cwd):
        # overlapping window, so time_overlap alone would have joined it before
        return {"agent": "claude", "session_id": "x", "cwd": cwd,
                "log_path": "/l.jsonl", "first_ts": 0, "last_ts": 10 ** 12}

    for label, cwd, repo, want in [
        ("same dir", "/w/papyrus", "/w/papyrus", "cwd"),
        ("agent cd'd into a subdir", "/w/papyrus/tests", "/w/papyrus", "related"),
        ("agent started one level up", "/w", "/w/papyrus", "related"),
        ("unrelated sibling", "/w/claimagent", "/w/papyrus", "reject"),
        ("unrelated absolute", "/tmp/other", "/w/papyrus", "reject"),
        # the prefix must be path-wise, not string-wise
        ("string prefix but different dir", "/w/papyrus-old", "/w/papyrus", "reject"),
    ]:
        conf, sig = score(sess(repo), traj(cwd))
        if want == "cwd":
            ok = conf == "medium" and "cwd" in sig
        elif want == "related":
            ok = conf == "low" and sig == ["time_overlap"]
        else:
            ok = conf is None and sig == ["cwd_disjoint"]
        if not ok:
            fails.append("cwd gate: %s -> conf=%r signals=%r" % (label, conf, sig))
        else:
            print("[ok] cwd gate: %s" % label)

    # a log that records no cwd at all is unknown, not disqualified
    conf, sig = score(sess("/w/papyrus"), {**traj("/w/claimagent"), "cwd": None})
    if conf != "low" or sig != ["time_overlap"]:
        fails.append("a cwd-less log should stay joinable on time alone: %r %r" % (conf, sig))
    else:
        print("[ok] cwd gate: a log with no recorded cwd is unknown, not refused")

    if not _cwd_related("/w/papyrus", "/w/papyrus-old"):
        print("[ok] cwd gate: path-wise prefix, not string-wise")
    else:
        fails.append("_cwd_related treated a string prefix as a path prefix")
    return fails


if __name__ == "__main__":
    main()
