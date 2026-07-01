"""Watcher: the default, zero-friction collection mechanism — NOT the correctness
boundary (v0.2). It observes agent session logs via adapters and reconciles them
against the agentcap session store, opening/closing sessions best-effort.

Sessions opened by the watcher are confidence="best-effort" (idle != done; the start
snapshot may lag the first agent edit), so they are NOT benchmark-eligible unless a
hook later upgrades them. Reconciliation is keyed by the agent's own session_id, so
two concurrent sessions in the same repo don't collide.

env_start FIDELITY depends on poll cadence: the watcher can only snapshot env_start
once it first *sees* the session's log (which appears after the agent starts), so the
start snapshot is faithful only when `interval + grace < the agent's first-edit
latency`. For interactive Claude Code / Codex sessions (minutes long) the default
60s/5s is comfortably safe. But a sub-minute headless run (e.g. `codex exec` solving a
toy task in ~30s) can finish editing before the first poll — env_start then captures
the ALREADY-CHANGED tree and the delta collapses to empty. If you need to capture such
fast sessions, tighten `interval`/`GRACE_SECS` below the first-edit latency, or pin the
boundary explicitly with mark-start/mark-end (the high-confidence path).

`reconcile()` is a pure decision function (observed + open state + now -> to_start /
to_end), testable without a daemon. `tick()` executes those decisions; `watch()`
loops via periodic reconciliation (survives sleep/crash/missed FS events).
"""
import os
import time

from . import adapters as A
from . import session as sess

IDLE_SECS = 15 * 60      # no writes for this long -> treat the session as ended
GRACE_SECS = 5           # ignore logs younger than this (still being created)


def _is_git(path):
    return bool(path) and os.path.isdir(os.path.join(path, ".git"))


def observe(adapter_list=None):
    adapter_list = adapter_list or A.default_adapters()
    obs = []
    for ad in adapter_list:
        for s in ad.sessions():
            s = dict(s)
            s["is_git"] = _is_git(s["cwd"])
            obs.append(s)
    return obs


def _safe_id(agent, session_id):
    return "%s-%s" % (agent, session_id.replace("/", "_"))


def reconcile(observed, sessions, now, idle_secs=IDLE_SECS, grace_secs=GRACE_SECS):
    """Pure decision. sessions: list of agentcap session dicts (any status).
    Returns (to_start, to_end, to_reopen).
      to_start:  observed git repos, fresh, with no agentcap session yet.
      to_reopen: observed git repos, fresh, whose agentcap session is CLOSED — the
                 agent resumed after an idle close, so continue the same session.
      to_end:    OPEN agentcap sessions whose agent log is idle or gone."""
    by_agent, open_by_agent = {}, {}
    for s in sessions:
        aid = s.get("extra", {}).get("agent_session_id")
        if not aid:
            continue
        key = (s["agent"], aid)
        by_agent[key] = s
        if s["status"] == "open":
            open_by_agent[key] = s

    to_start, to_reopen = [], []
    for o in observed:
        if not o["is_git"]:
            continue
        key = (o["agent"], o["session_id"])
        if key in open_by_agent:
            continue
        if now - o["mtime"] < grace_secs:      # still being created
            continue
        if now - o["mtime"] >= idle_secs:      # stale -> neither open nor resume
            continue
        existing = by_agent.get(key)
        if existing is None:
            to_start.append(o)                 # brand new session
        elif existing["status"] == "closed":
            to_reopen.append(existing)         # resumed after an idle close

    to_end = []
    for (agent, aid), s in open_by_agent.items():
        o = next((x for x in observed if x["agent"] == agent
                  and x["session_id"] == aid), None)
        if o is None:                           # log vanished
            to_end.append(s)
        elif now - o["mtime"] >= idle_secs:     # gone idle
            to_end.append(s)
    return to_start, to_end, to_reopen


def tick(adapter_list=None, root=sess.DEFAULT_ROOT, idle_secs=IDLE_SECS, now=None):
    now = now if now is not None else time.time()
    observed = observe(adapter_list)
    all_sessions = sess.list_sessions(root)
    to_start, to_end, to_reopen = reconcile(observed, all_sessions, now, idle_secs)

    started, ended, reopened = [], [], []
    for o in to_start:
        sid = _safe_id(o["agent"], o["session_id"])
        try:
            sess.start_session(
                o["cwd"], agent=o["agent"], confidence="best-effort", root=root,
                session_id=sid,
                extra={"agent_session_id": o["session_id"], "log_path": o["log_path"]})
            started.append(sid)
        except RuntimeError:
            pass  # already exists (raced) -> skip
    for s in to_reopen:
        sess.reopen_session(session_id=s["session_id"], root=root)
        reopened.append(s["session_id"])
    for s in to_end:
        sess.end_session(session_id=s["session_id"], confidence="best-effort", root=root)
        ended.append(s["session_id"])
    return started, ended, reopened


def watch(adapter_list=None, root=sess.DEFAULT_ROOT, interval=60, idle_secs=IDLE_SECS):
    while True:
        try:
            started, ended, reopened = tick(adapter_list, root, idle_secs)
            if started or ended or reopened:
                print("[agentcap] +%d started, -%d ended, ~%d reopened"
                      % (len(started), len(ended), len(reopened)), flush=True)
        except Exception as e:  # a watcher must never die on one bad tick
            print("[agentcap] tick error: %s" % e, flush=True)
        time.sleep(interval)
