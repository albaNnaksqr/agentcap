"""Per-agent session-log adapters (v0.2: versioned, tested against real logs — the
watcher treats these dirs as private implementation details of other tools, NOT a
stable API). Each adapter enumerates sessions and reads cwd from the log *content*
(the on-disk dir name encoding is lossy/ambiguous), plus session_id and last activity.
"""
import glob
import json
import os

CLAUDE_ROOT = os.path.expanduser("~/.claude/projects")
CODEX_ROOT = os.path.expanduser("~/.codex/sessions")
_SCAN_LINES = 400  # cwd usually appears early; bound the read


def _first_cwd(path):
    try:
        with open(path, "r", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > _SCAN_LINES:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                for cwd in _find_cwd(d):
                    if cwd:
                        return cwd
    except OSError:
        return None
    return None


def _find_cwd(obj):
    """cwd can be top-level (Claude) or nested (Codex session_meta/turn_context)."""
    if isinstance(obj, dict):
        if isinstance(obj.get("cwd"), str):
            yield obj["cwd"]
        for v in obj.values():
            yield from _find_cwd(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_cwd(v)


class Adapter:
    name = "base"

    def sessions(self):
        """-> list of {agent, session_id, cwd, log_path, mtime}. cwd may be None."""
        raise NotImplementedError


class ClaudeAdapter(Adapter):
    name = "claude"

    def __init__(self, root=CLAUDE_ROOT):
        self.root = root

    def sessions(self):
        out = []
        for path in glob.glob(os.path.join(self.root, "*", "*.jsonl")):
            out.append({
                "agent": self.name,
                "session_id": os.path.splitext(os.path.basename(path))[0],
                "cwd": _first_cwd(path),
                "log_path": path,
                "mtime": os.path.getmtime(path),
            })
        return out


class CodexAdapter(Adapter):
    name = "codex"

    def __init__(self, root=CODEX_ROOT):
        self.root = root

    def sessions(self):
        out = []
        for path in glob.glob(os.path.join(self.root, "*", "*", "*", "*.jsonl")):
            out.append({
                "agent": self.name,
                "session_id": os.path.splitext(os.path.basename(path))[0],
                "cwd": _first_cwd(path),
                "log_path": path,
                "mtime": os.path.getmtime(path),
            })
        return out


def default_adapters():
    return [ClaudeAdapter(), CodexAdapter()]
