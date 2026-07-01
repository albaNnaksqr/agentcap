"""Trajectory sources. A trajectory is the agent's conversation+tool-call stream for a
session; the env captures are the git snapshots around it. Join (join.py) pairs them.

The raw agent session logs ARE the stream dataclaw itself reads, so RawLogSource is
real, not a stand-in. DataclawSource is left as a drop-in for a dataclaw export.
"""
import datetime
import glob
import json
import os
import re

from . import adapters as A

_TS = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')


def epoch(iso):
    """ISO-8601 -> float seconds. Tolerates trailing 'Z'. Returns None on failure."""
    if not iso:
        return None
    s = iso.replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _scan_span(path):
    """Fast pass over a jsonl log: (first_ts, last_ts, n_lines) using a regex so we
    don't json-parse every line."""
    first = last = None
    n = 0
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                n += 1
                m = _TS.search(line)
                if m:
                    if first is None:
                        first = m.group(1)
                    last = m.group(1)
    except OSError:
        pass
    return epoch(first), epoch(last), n


class TrajectorySource:
    def trajectories(self):
        """-> list of {agent, session_id, cwd, log_path, first_ts, last_ts, n_steps}."""
        raise NotImplementedError


class RawLogSource(TrajectorySource):
    def __init__(self, adapter_list=None):
        self.adapters = adapter_list or A.default_adapters()

    def trajectories(self):
        out = []
        for ad in self.adapters:
            for s in ad.sessions():
                first, last, n = _scan_span(s["log_path"])
                out.append({
                    "agent": s["agent"],
                    "session_id": s["session_id"],
                    "cwd": s["cwd"],
                    "log_path": s["log_path"],
                    "first_ts": first,
                    "last_ts": last if last is not None else s["mtime"],
                    "n_steps": n,
                })
        return out


class DataclawSource(TrajectorySource):
    """Drop-in for a dataclaw export dir of per-session json (session_id/cwd/messages).
    Not exercised yet (no export on disk); shape kept parallel to RawLogSource."""
    def __init__(self, export_dir):
        self.export_dir = export_dir

    def trajectories(self):
        out = []
        for path in glob.glob(os.path.join(self.export_dir, "*.json")):
            try:
                d = json.load(open(path))
            except (OSError, ValueError):
                continue
            out.append({
                "agent": d.get("agent", "unknown"),
                "session_id": d.get("session_id") or d.get("sessionId"),
                "cwd": d.get("cwd"),
                "log_path": path,
                "first_ts": epoch(d.get("start_time")),
                "last_ts": epoch(d.get("end_time")),
                "n_steps": len(d.get("messages", []) or d.get("steps", [])),
            })
        return out
