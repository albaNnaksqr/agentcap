"""Runtime evidence (L1): record the facts a later rebuild would need — platform,
dependency-declaration files (hashed like everything else, git-normalized blob oids),
and the toolchain versions those declarations imply. Evidence sidecar only: nothing
here gates verify/value, and agentcap still does not build or resolve a runtime
(that stays out of scope — flight recorder, not flight simulator).
"""
import os
import platform
import subprocess

from . import gitutil as g

RUNTIME_VERSION = 1

# dep-declaration basename -> toolchain to probe. Manifests (pyproject/package.json)
# count as declarations too: they're what a rebuild starts from when no lock exists.
DEP_FILES = {
    "requirements.txt": ["python3"],
    "pyproject.toml": ["python3"],
    "uv.lock": ["python3", "uv"],
    "poetry.lock": ["python3", "poetry"],
    "Pipfile.lock": ["python3"],
    "package.json": ["node", "npm"],
    "package-lock.json": ["node", "npm"],
    "yarn.lock": ["node", "yarn"],
    "pnpm-lock.yaml": ["node", "pnpm"],
    "Cargo.toml": ["cargo", "rustc"],
    "Cargo.lock": ["cargo", "rustc"],
    "go.mod": ["go"],
    "go.sum": ["go"],
    "Gemfile.lock": ["ruby"],
    "composer.lock": ["php"],
}

_VERSION_ARGS = {"go": ["version"]}  # everything else answers --version


def _probe(tool):
    """First line of `tool --version` (or None if the tool is absent/broken).
    Absence is evidence too: the declaration implied a tool this host lacks."""
    try:
        p = subprocess.run([tool] + _VERSION_ARGS.get(tool, ["--version"]),
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = (p.stdout or p.stderr).strip().splitlines()
    return line[0] if p.returncode == 0 and line else None


def collect(repo):
    dep_files, tools_wanted = [], {"git"}
    for path in g.tracked_present(repo) + g.untracked(repo):
        base = path.rsplit("/", 1)[-1]
        if base not in DEP_FILES:
            continue
        dep_files.append({
            "path": path,
            "content_hash": g.hash_file(repo, path),
            "size": os.path.getsize(os.path.join(repo, path)),
        })
        tools_wanted.update(DEP_FILES[base])
    dep_files.sort(key=lambda d: d["path"])
    return {
        "runtime_version": RUNTIME_VERSION,
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "dep_files": dep_files,
        "tools": {t: _probe(t) for t in sorted(tools_wanted)},
    }
