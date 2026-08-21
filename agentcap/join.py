"""Join a trajectory to an env-capture session, with a CONFIDENCE, not a boolean
(v0.2 / Codex). Signals: session_id (strongest), cwd, time_overlap, log_path.

Confidence:
  high   = session_id matches (direct, unambiguous)
  medium = cwd matches AND time windows overlap
  low    = cwd OR time overlap only
  (none) = no usable signal, OR the candidate is DISQUALIFIED -> not joined

A cwd unrelated to the repo disqualifies rather than lowering confidence. A
session that ran start to finish in /workspace/claimagent cannot be the record of
an edit made in /workspace/papyrus; that is certainty of the opposite, not weak
evidence. Before this rule, time_overlap alone was enough to staple whatever else
was running onto the capture and call it `low` — 16 sessions in the store were
paired across unrelated projects that way, three of them reached an export, and
one was a subagent capture that got this very conversation's transcript. An
unjoined session now records `unjoined_reason` rather than keeping a stale pair.

A low/medium join is fuel only; benchmark eligibility already requires join=high
(session.verify_session gates env; the benchmark gate ANDs join_confidence=high).
Manual repair is supported via set_join().
"""
import json
import os

from . import session as sess
from .trajectory import epoch

_RANK = {"high": 3, "medium": 2, "low": 1}


def _overlap(a0, a1, b0, b1):
    if None in (a0, a1, b0, b1):
        return False
    return a0 <= b1 and b0 <= a1


def _cwd_related(cwd, repo):
    """True when the log's working dir could plausibly be where this repo was
    edited: the same path, or one containing the other.

    Both directions count. A user starting the agent in ~/workspace while the
    capture is ~/workspace/trajmill is a plausible pairing (repo under cwd), and
    so is an agent that cd'd into a subdirectory of the repo (cwd under repo).
    Only genuinely unrelated trees are refused."""
    a, b = os.path.abspath(cwd), os.path.abspath(repo)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def score(session, traj):
    """Return (confidence, signals) for pairing this trajectory with this session,
    or (None, []) if there's no usable signal."""
    signals = []
    aid = session.get("extra", {}).get("agent_session_id")
    if aid and traj.get("session_id") == aid and traj.get("agent") == session.get("agent"):
        signals.append("session_id")
    if session.get("extra", {}).get("log_path") and \
            session["extra"]["log_path"] == traj.get("log_path"):
        signals.append("log_path")
    if traj.get("cwd") and os.path.abspath(traj["cwd"]) == os.path.abspath(session["repo"]):
        signals.append("cwd")
    elif traj.get("cwd") and not _cwd_related(traj["cwd"], session["repo"]):
        # DISQUALIFYING, not merely "one signal short". A session that ran start
        # to finish in /workspace/claimagent cannot be the record of an edit made
        # in /workspace/papyrus — that is not low confidence, it is certainty of
        # the opposite. Falling through to `time_overlap` alone let join staple
        # whatever else happened to be running onto the capture and label it
        # `low`: 16 sessions in the store were paired across unrelated projects
        # that way, and three of them reached an export.
        return None, ["cwd_disjoint"]
    if _overlap(epoch(session.get("created_at")), epoch(session.get("closed_at")),
                traj.get("first_ts"), traj.get("last_ts")):
        signals.append("time_overlap")

    if "session_id" in signals:
        conf = "high"
    elif "cwd" in signals and "time_overlap" in signals:
        conf = "medium"
    elif "cwd" in signals or "time_overlap" in signals:
        conf = "low"
    else:
        return None, []
    return conf, signals


def join_session(session, trajectories):
    """Best trajectory for one session. Ties broken by rank, then #signals, then time
    proximity of the trajectory's last activity to the session close."""
    best = None
    sclose = epoch(session.get("closed_at")) or epoch(session.get("created_at")) or 0
    for t in trajectories:
        conf, signals = score(session, t)
        if conf is None:
            continue
        prox = -abs((t.get("last_ts") or sclose) - sclose)
        key = (_RANK[conf], len(signals), prox)
        if best is None or key > best[0]:
            best = (key, conf, signals, t)
    if best is None:
        return None
    _, conf, signals, t = best
    return {
        "join_confidence": conf,
        "signals": signals,
        "trajectory": {k: t[k] for k in ("agent", "session_id", "cwd", "log_path",
                                         "first_ts", "last_ts", "n_steps")},
    }


def _refusal_reason(session, trajectories):
    """Why nothing was joined -- so `unjoined` is a statement, not a silence.

    `cwd_disjoint` is the interesting one: candidates existed and every one of
    them ran in a tree unrelated to this repo. That is the case a plain "no
    trajectory found" would hide."""
    saw_disjoint = False
    for t in trajectories:
        conf, sig = score(session, t)
        if conf is not None:
            return "candidate_scored_but_not_selected"   # should not happen
        if sig == ["cwd_disjoint"]:
            saw_disjoint = True
    return "cwd_disjoint" if saw_disjoint else "no_candidate_trajectory"


def join_all(source, root=sess.DEFAULT_ROOT):
    """Join every session in the store; write join.json into each session dir."""
    trajectories = source.trajectories()
    _, sessions_dir = sess._paths(root)
    results = {}
    for s in sess.list_sessions(root):
        j = join_session(s, trajectories)
        results[s["session_id"]] = j
        path = os.path.join(sessions_dir, s["session_id"], "join.json")
        if j is not None:
            _write(path, j)
        else:
            # Record the REFUSAL. Leaving an earlier join.json in place would keep
            # a pairing the current rules reject -- new code being right does not
            # make leftover state honest, and here the leftover is a trajectory
            # from a different project.
            _write(path, {"join_confidence": None, "signals": [],
                          "trajectory": None,
                          "unjoined_reason": _refusal_reason(s, trajectories)})
        sess.refresh_eligibility(s["session_id"], root=root)
    return results


def set_join(session_id, trajectory, confidence="high", signals=None,
             root=sess.DEFAULT_ROOT):
    """Manual repair: force a join for a session."""
    _, sessions_dir = sess._paths(root)
    j = {"join_confidence": confidence,
         "signals": signals or ["manual"],
         "trajectory": trajectory}
    _write(os.path.join(sessions_dir, session_id, "join.json"), j)
    sess.refresh_eligibility(session_id, root=root)
    return j


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, sort_keys=True, indent=2)
        f.write("\n")
