"""agentcap CLI:
    python3 -m agentcap snapshot   <repo> <capture_dir> [--cas DIR]
    python3 -m agentcap verify     <capture_dir> <source_repo> [--cas DIR]
    python3 -m agentcap mark-start <repo> [--agent A] [--root DIR]
    python3 -m agentcap mark-end   [--repo R | --session-id ID] [--root DIR]
    python3 -m agentcap verify-session <session_id> [--root DIR]
"""
import argparse
import json
import sys

from .snapshot import snapshot
from .verify import verify
from . import session as sess


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agentcap")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="snapshot a git worktree into a capture dir")
    s.add_argument("repo")
    s.add_argument("capture_dir")
    s.add_argument("--cas", default=None)

    v = sub.add_parser("verify", help="reconstruct + hash-verify a capture")
    v.add_argument("capture_dir")
    v.add_argument("source_repo")
    v.add_argument("--cas", default=None)

    ms = sub.add_parser("mark-start", help="pin a session start (high confidence)")
    ms.add_argument("repo")
    ms.add_argument("--agent", default="manual")
    ms.add_argument("--root", default=sess.DEFAULT_ROOT)

    me = sub.add_parser("mark-end", help="pin a session end + compute delta")
    me.add_argument("--repo", default=None)
    me.add_argument("--session-id", default=None)
    me.add_argument("--root", default=sess.DEFAULT_ROOT)

    vs = sub.add_parser("verify-session", help="verify both env snapshots of a session")
    vs.add_argument("session_id")
    vs.add_argument("--root", default=sess.DEFAULT_ROOT)

    tk = sub.add_parser("tick", help="one watcher reconciliation pass")
    tk.add_argument("--root", default=sess.DEFAULT_ROOT)

    w = sub.add_parser("watch", help="run the watcher daemon loop")
    w.add_argument("--root", default=sess.DEFAULT_ROOT)
    w.add_argument("--interval", type=int, default=60)

    ob = sub.add_parser("observe", help="dump agent sessions the adapters see (debug)")

    jn = sub.add_parser("join", help="join trajectories to env sessions (with confidence)")
    jn.add_argument("--root", default=sess.DEFAULT_ROOT)
    jn.add_argument("--dataclaw", default=None, help="a dataclaw export dir (else raw logs)")

    sd = sub.add_parser("seed", help="mine candidate task signals from joined sessions")
    sd.add_argument("--root", default=sess.DEFAULT_ROOT)

    vl = sub.add_parser("value", help="score trajectory value (groundedness x process richness)")
    vl.add_argument("--root", default=sess.DEFAULT_ROOT)

    a = ap.parse_args(argv)
    if a.cmd == "snapshot":
        meta, manifest = snapshot(a.repo, a.capture_dir, cas_root=a.cas)
        print(json.dumps({"meta": meta, "files": len(manifest)}, indent=2))
    elif a.cmd == "verify":
        ok, report = verify(a.capture_dir, a.source_repo, cas_root=a.cas)
        print(json.dumps(report, indent=2))
        sys.exit(0 if ok else 1)
    elif a.cmd == "mark-start":
        sid, s = sess.start_session(a.repo, agent=a.agent, confidence="high", root=a.root)
        print(json.dumps({"session_id": sid, "base_sha_start": s["base_sha_start"]}, indent=2))
    elif a.cmd == "mark-end":
        s, delta = sess.end_session(session_id=a.session_id, repo=a.repo,
                                    confidence="high", root=a.root)
        print(json.dumps({"session_id": s["session_id"], "delta": delta}, indent=2))
    elif a.cmd == "verify-session":
        rep = sess.verify_session(a.session_id, root=a.root)
        print(json.dumps(rep, indent=2))
        sys.exit(0 if rep["verified"] else 1)
    elif a.cmd == "tick":
        from . import watcher
        started, ended = watcher.tick(root=a.root)
        print(json.dumps({"started": started, "ended": ended}, indent=2))
    elif a.cmd == "watch":
        from . import watcher
        watcher.watch(root=a.root, interval=a.interval)
    elif a.cmd == "observe":
        from . import watcher
        obs = watcher.observe()
        print(json.dumps([{k: o[k] for k in ("agent", "session_id", "cwd", "is_git")}
                          for o in obs], indent=2))
    elif a.cmd == "join":
        from . import join as J
        from .trajectory import RawLogSource, DataclawSource
        source = DataclawSource(a.dataclaw) if a.dataclaw else RawLogSource()
        results = J.join_all(source, root=a.root)
        summary = {"joined": 0, "high": 0, "medium": 0, "low": 0, "unjoined": 0}
        for j in results.values():
            if j is None:
                summary["unjoined"] += 1
            else:
                summary["joined"] += 1
                summary[j["join_confidence"]] += 1
        print(json.dumps(summary, indent=2))
    elif a.cmd == "seed":
        from . import taskseed as T
        results = T.seed_all(root=a.root)
        summary = {"with_candidates": 0, "no_seed": 0}
        for s in results.values():
            summary["with_candidates" if (s and not s.get("error")) else "no_seed"] += 1
        print(json.dumps(summary, indent=2))
    elif a.cmd == "value":
        from . import value as V
        results = V.assess_all(root=a.root)
        summary = {"high": 0, "medium": 0, "low": 0, "no_value": 0}
        for v in results.values():
            if not v or v.get("error"):
                summary["no_value"] += 1
            else:
                summary[v["value_tier"]] += 1
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
