"""Watcher: the default, zero-friction collection mechanism — NOT the correctness
boundary (v0.2). It observes agent session logs via adapters and reconciles them
against the agentcap session store, opening/closing sessions best-effort.

Sessions opened by the watcher are confidence="best-effort" (idle != done; the start
snapshot may lag the first agent edit), so they are NOT benchmark-eligible unless a
hook later upgrades them. Reconciliation is keyed by the agent's own session_id, so
two concurrent sessions in the same repo don't collide.

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


def reconcile(observed, open_sessions, now, idle_secs=IDLE_SECS, grace_secs=GRACE_SECS):
    """Pure decision. open_sessions: list of agentcap session dicts (status=open).
    Returns (to_start, to_end).
      to_start: observed entries that are git repos, fresh, and not already open.
      to_end:   open agentcap sessions whose agent log is idle or gone."""
    open_by_agent = {}
    for s in open_sessions:
        aid = s.get("extra", {}).get("agent_session_id")
        if aid:
            open_by_agent[(s["agent"], aid)] = s

    seen_keys = set()
    to_start = []
    for o in observed:
        if not o["is_git"]:
            continue
        key = (o["agent"], o["session_id"])
        seen_keys.add(key)
        if key in open_by_agent:
            continue
        if now - o["mtime"] < grace_secs:      # still being created
            continue
        if now - o["mtime"] >= idle_secs:      # already stale on first sight -> skip open
            continue
        to_start.append(o)

    to_end = []
    for (agent, aid), s in open_by_agent.items():
        o = next((x for x in observed if x["agent"] == agent
                  and x["session_id"] == aid), None)
        if o is None:                           # log vanished
            to_end.append(s)
        elif now - o["mtime"] >= idle_secs:     # gone idle
            to_end.append(s)
    return to_start, to_end


def tick(adapter_list=None, root=sess.DEFAULT_ROOT, idle_secs=IDLE_SECS, now=None):
    now = now if now is not None else time.time()
    observed = observe(adapter_list)
    open_sessions = sess.list_sessions(root, status="open")
    to_start, to_end = reconcile(observed, open_sessions, now, idle_secs)

    started, ended = [], []
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
    for s in to_end:
        sess.end_session(session_id=s["session_id"], confidence="best-effort", root=root)
        ended.append(s["session_id"])
    return started, ended


def watch(adapter_list=None, root=sess.DEFAULT_ROOT, interval=60, idle_secs=IDLE_SECS):
    while True:
        try:
            started, ended = tick(adapter_list, root, idle_secs)
            if started or ended:
                print("[agentcap] +%d started, -%d ended" % (len(started), len(ended)),
                      flush=True)
        except Exception as e:  # a watcher must never die on one bad tick
            print("[agentcap] tick error: %s" % e, flush=True)
        time.sleep(interval)
