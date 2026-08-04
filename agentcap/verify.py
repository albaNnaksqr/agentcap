"""Reconstruct a capture into a clean temp dir and verify it against the manifest.

The v0.2 trust core: `reconstruction_verified` is true IFF every non-skipped manifest
entry's recomputed git-normalized hash matches. This is what stops `verify` from
"proving" a reconstruction that is internally consistent but not the original tree.

Step-1 reconstructs from the *local* source repo (its object store), not a remote
clone — that exercises the full manifest/verify machinery offline. The remote-clone
path is a publish-time concern (later step).
"""
import os
import shutil
import stat
import subprocess
import tempfile

from . import gitutil as g
from .cas import CAS
from .snapshot import load_manifest


def _apply(dest, diff_path):
    if os.path.getsize(diff_path) == 0:
        return
    p = subprocess.run(
        ["git", "-C", dest, "apply", "--whitespace=nowarn", diff_path],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        raise RuntimeError("git apply %s failed: %s" % (diff_path, p.stderr.strip()))


def _materialize_tree(tarball, dest, want_tree_sha):
    """Lay down a history-free base from `git archive` output.

    `git add -A` alone is NOT enough: it honours the .gitignore that just came out
    of the tarball, and repos do track files their own ignore rules match (litellm
    tracks `cookbook/misc/config.yaml` against `.gitignore:92` — 246 such paths).
    Without --force those files silently vanish from the reconstruction. --force is
    load-bearing; do not drop it.
    """
    os.makedirs(dest, exist_ok=True)
    subprocess.run(["tar", "-xzf", tarball, "-C", dest], check=True, capture_output=True)
    for args in (["init", "-q"], ["config", "user.email", "agentcap@local"],
                 ["config", "user.name", "agentcap"], ["add", "-A", "--force"]):
        subprocess.run(["git", "-C", dest] + args, check=True, capture_output=True)
    got = subprocess.run(["git", "-C", dest, "write-tree"], check=True,
                         capture_output=True, text=True).stdout.strip()
    if want_tree_sha and got != want_tree_sha:
        raise RuntimeError("tree snapshot mismatch: expected %s, rebuilt %s"
                           % (want_tree_sha, got))
    subprocess.run(["git", "-C", dest, "commit", "-q", "-m", "agentcap base snapshot",
                    "--no-verify"], check=True, capture_output=True)
    return got


def reconstruct(capture_dir, source, dest, cas_root=None):
    """`source` is a git repo/bundle path, or a `git archive` tarball (tree snapshot).
    A tarball carries no history, so the base is verified by tree hash instead of by
    checking out base_sha."""
    man = load_manifest(capture_dir)
    base_sha = man["meta"]["base_sha"]
    cas = CAS(cas_root or os.path.join(capture_dir, "blobs"))

    if str(source).endswith((".tar.gz", ".tgz")):
        _materialize_tree(source, dest, man["meta"].get("base_tree_sha"))
    else:
        subprocess.run(["git", "clone", "--quiet", source, dest],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", dest, "checkout", "--quiet", "--detach", base_sha],
                       check=True, capture_output=True)

    # tracked final content = base + staged(HEAD->index) + unstaged(index->worktree)
    _apply(dest, os.path.join(capture_dir, "staged.diff"))
    _apply(dest, os.path.join(capture_dir, "unstaged.diff"))

    # untracked files aren't in any diff -> materialize from CAS
    for e in man["entries"]:
        if not e.get("untracked") or e["status"] != "present":
            continue
        target = os.path.join(dest, e["path"])
        os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
        if e["type"] == "symlink":
            if os.path.lexists(target):
                os.remove(target)
            os.symlink(e["symlink_target"], target)
        else:
            with cas.open(e["content_hash"]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if e["exec_bit"]:
                os.chmod(target, os.stat(target).st_mode | 0o111)
    return man


def verify(capture_dir, source_repo, cas_root=None):
    """Returns (ok, report). ok=True iff every non-skipped entry's hash matches."""
    dest = tempfile.mkdtemp(prefix="agentcap-verify-")
    try:
        man = reconstruct(capture_dir, source_repo, dest, cas_root)
        mismatches, skipped, missing = [], [], []
        for e in man["entries"]:
            if e["status"] != "present":
                skipped.append(e["path"])          # non-verifying by design
                continue
            target = os.path.join(dest, e["path"])
            if not os.path.lexists(target):
                missing.append(e["path"])
                continue
            if e["type"] == "symlink":
                got = g.hash_bytes(dest, os.readlink(target).encode())
            else:
                got = g.hash_file(dest, e["path"])
            if got != e["content_hash"]:
                mismatches.append({"path": e["path"],
                                   "expected": e["content_hash"], "got": got})
        ok = not mismatches and not missing
        report = {
            "reconstruction_verified": ok,
            "checked": len(man["entries"]) - len(skipped),
            "mismatches": mismatches,
            "missing": missing,
            "skipped_nonverifying": skipped,
            "snapshot_inconsistent": man["meta"]["snapshot_inconsistent"],
            # benchmark-eligible requires verified AND consistent AND nothing non-verifying
            "benchmark_eligible": ok
            and not man["meta"]["snapshot_inconsistent"]
            and not man["meta"]["has_nonverifying_entries"],
        }
        return ok, report
    finally:
        shutil.rmtree(dest, ignore_errors=True)
