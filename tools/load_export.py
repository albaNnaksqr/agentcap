#!/usr/bin/env python3
"""Assemble agentcap task instances using NOTHING but an export directory.

Why this exists
---------------
agentcap's exports claim to be self-contained and reproducible elsewhere. That
claim had never been exercised, because the only thing that ever rebuilt them —
`agentcap replay` — reads the session store, not the export:

    ~/.agentcap/sessions/<sid>/env_start|env_end/    the capture dir
    session["cas_root"] = ~/.agentcap/blobs          774 MB, OUTSIDE the export

The export's own `env/blobs/` is a 4 KB copy of the slice a session referenced.
So every number ever reported ("22 verified, 3302 witnesses") was measured with
the factory parts bin open. This script closes the bin: it may read the export
directory and nothing else, and it reports which instances can actually be
assembled into a runnable RL task from that alone.

It deliberately imports NOTHING from agentcap — stdlib only. What enforces the
independence is that fact plus the FORBIDDEN path guard below, not which
directory the file sits in: a loader that reuses the producer's helpers would
reach for `cas_root` the same way replay does and discover nothing. It lives in
this repo because the artifact it checks is this repo's; test_loader_independence
pins the no-import rule so a future refactor cannot quietly undo it.

What it answers / does not answer
---------------------------------
Answers: is each instance STRUCTURALLY sufficient (prompt, verifier spec, source
tree that verifies against its own manifest, runtime lock), and — with --build —
does the verifier actually reproduce RED at the start state.

Does not answer: whether the task is GOOD RL data. Difficulty, hint leakage (the
problem statements are currently authored task briefs, which give a lot away) and
reward density are out of scope here.

Usage
-----
    python3 tools/load_export.py <export_dir>
    python3 tools/load_export.py <export_dir> --build [--max-build N]
    python3 tools/load_export.py <export_dir> --json report.json

--build creates a venv per DISTINCT runtime lock (they are shared: 14 litellm
instances have one identical lock) and needs network for pip. Without it the
runtime check only confirms a lock is present and parseable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

# --- the guard -----------------------------------------------------------------
# The whole point of this script is that it cannot reach the producer's machine
# state. Enforced rather than intended: every path we touch goes through _p(),
# and subprocesses get HOME pointed at a temp dir so `~/.agentcap` cannot be
# resolved implicitly and pip cannot use the user cache.
FORBIDDEN = ("/.agentcap", "/osmind-repos", "/.codex", "/wt/")


class GuardViolation(RuntimeError):
    pass


def _p(path: str) -> str:
    real = os.path.realpath(path)
    for bad in FORBIDDEN:
        if bad in real:
            raise GuardViolation(
                "refused to touch %s (matches %r) — this script may only read the "
                "export directory; if it needs that path, the export is not "
                "self-sufficient and THAT is the finding" % (real, bad))
    return path


def _read(path: str, mode: str = "r"):
    with open(_p(path), mode, errors=None if "b" in mode else "replace") as f:
        return f.read()


def _json(path: str):
    return json.loads(_read(path))


def _run(cmd, cwd=None, env=None, timeout=900, home=None):
    """Subprocess with HOME redirected, so nothing can expand ~ back to the
    producer's home. Returns (rc, out+err)."""
    e = dict(os.environ)
    e.pop("PYTHONPATH", None)
    if home:
        e["HOME"] = home
        e["XDG_CACHE_HOME"] = os.path.join(home, ".cache")
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# --- checks --------------------------------------------------------------------

# The same harness preamble agentcap learned to skip. Checked independently here:
# if a statement is nothing but this, a trainer would be prompting on boilerplate.
_PREAMBLE = re.compile(
    r"<(recommended_plugins|environment_context|system[-_]reminder|INSTRUCTIONS)\b[^>]*>.*?</\1>"
    r"|^\#+[ \t]*(?:AGENTS|CLAUDE)\.md instructions[^\n]*", re.S | re.M)


def check_prompt(task):
    st = (task or {}).get("problem_statement")
    if not st or not st.strip():
        return False, "no problem_statement"
    if not _PREAMBLE.sub("", st).strip():
        return False, "problem_statement is only harness preamble"
    return True, "%d chars" % len(st)


def check_verifier(task):
    ftp = (task or {}).get("fail_to_pass") or []
    if not ftp:
        return False, "no fail_to_pass"
    ptp = task.get("pass_to_pass") or []
    if ptp:
        return True, "ftp=%d ptp=%d" % (len(ftp), len(ptp))
    reason = task.get("regression_reason")
    if reason:
        # An empty guard is acceptable to a consumer only because it says why.
        return True, "ftp=%d ptp=0 (%s)" % (len(ftp), reason)
    return False, "ftp=%d but ptp empty with no stated reason" % len(ftp)


def materialize(inst_dir, side, work, home):
    """Rebuild one side's worktree from the export alone -> (path, note).

    Raises on anything the export cannot supply. `side` is env_start|env_end.
    """
    env = os.path.join(inst_dir, "env")
    src = _json(os.path.join(env, "source.json"))
    dest = os.path.join(work, side)
    os.makedirs(dest, exist_ok=True)
    man = _json(os.path.join(env, side, "manifest.json"))
    base = man["meta"]["base_sha"]

    kind = src.get("kind")
    if kind == "tree_snapshot":
        fn = (src.get("trees") or {}).get(base)
        if not fn:
            raise RuntimeError("tree_snapshot has no archive for base %s" % base[:12])
        tar = os.path.join(env, "trees", fn)
        with tarfile.open(_p(tar)) as t:
            t.extractall(dest)
        note = "tree_snapshot"
    elif kind == "bundle":
        bundle = os.path.join(env, "repo.bundle")
        if not os.path.exists(_p(bundle)):
            raise RuntimeError("source.json says bundle but repo.bundle is absent")
        rc, out = _run(["git", "clone", "--quiet", _p(bundle), dest], home=home)
        if rc != 0:
            raise RuntimeError("git clone of the bundle failed: %s" % out.strip()[:200])
        rc, out = _run(["git", "checkout", "--quiet", "--detach", base], cwd=dest, home=home)
        if rc != 0:
            raise RuntimeError("bundle lacks base %s: %s" % (base[:12], out.strip()[:160]))
        shutil.rmtree(os.path.join(dest, ".git"), ignore_errors=True)
        note = "bundle"
    else:
        # local_repo means the producer's own path -- by definition not portable
        raise RuntimeError("artifact kind %r is not shippable" % kind)

    # git init so diffs can be applied and blobs hashed, all inside the temp dir
    _run(["git", "init", "--quiet"], cwd=dest, home=home)
    for diff in ("staged.diff", "unstaged.diff"):
        path = os.path.join(env, side, diff)
        if os.path.exists(_p(path)) and os.path.getsize(_p(path)) > 0:
            rc, out = _run(["git", "apply", "--whitespace=nowarn", _p(path)],
                           cwd=dest, home=home)
            if rc != 0:
                raise RuntimeError("%s did not apply: %s" % (diff, out.strip()[:200]))
            note += "+%s" % diff.split(".")[0]

    # Untracked files the capture referenced come from the export's own blobs --
    # but ONLY those with status "present". `skipped_size` entries carry a hash
    # and deliberately no content (GB-scale checkpoints, big jsonl); the manifest
    # flags this as meta.has_nonverifying_entries and verify.json counts them as
    # skipped_nonverifying. Ignoring `status` was my own bug and it produced a
    # second false alarm -- the producer was explicit at every step.
    skipped_by_design = sum(
        1 for e in man["entries"]
        if e.get("untracked") and e.get("type") == "file" and e.get("status") != "present")
    missing_blobs = 0
    for ent in man["entries"]:
        if not ent.get("untracked") or ent.get("type") != "file":
            continue
        if ent.get("status") != "present":
            continue
        h = ent["content_hash"]
        # CAS layout is oid[:2]/oid[2:] -- git-style fan-out, NOT oid[:2]/oid.
        # I got this wrong on the first run and it produced a spectacular false
        # alarm ("21078 blobs missing"); verify.json's own checked/missing counts
        # were what contradicted it. Belt and braces: accept both spellings.
        blob = os.path.join(env, "blobs", h[:2], h[2:])
        if not os.path.exists(_p(blob)):
            alt = os.path.join(env, "blobs", h[:2], h)
            if os.path.exists(_p(alt)):
                blob = alt
            else:
                missing_blobs += 1
                continue
        tgt = os.path.join(dest, ent["path"])
        os.makedirs(os.path.dirname(tgt) or dest, exist_ok=True)
        shutil.copyfile(_p(blob), tgt)
    if missing_blobs:
        raise RuntimeError("%d untracked blob(s) with status=present are not in "
                           "env/blobs" % missing_blobs)
    if skipped_by_design:
        # Not a defect, but material to a consumer: the rebuilt tree really is
        # missing these, so a task whose tests touch them cannot work.
        note += "; %d file(s) skipped by size" % skipped_by_design
    return dest, note


def verify_manifest(tree, inst_dir, side, home, sample=None):
    """Hash the rebuilt tree against its own manifest -> (checked, mismatches, missing)."""
    man = _json(os.path.join(inst_dir, "env", side, "manifest.json"))
    # only entries the export actually claims to carry
    ents = [e for e in man["entries"]
            if e.get("type") == "file" and e.get("content_hash")
            and e.get("status") == "present"]
    if sample:
        ents = ents[:sample]
    paths, want = [], {}
    for e in ents:
        paths.append(e["path"])
        want[e["path"]] = e["content_hash"]
    missing = [p for p in paths if not os.path.exists(os.path.join(tree, p))]
    present = [p for p in paths if p not in set(missing)]
    # --stdin-paths takes the list on stdin, so this one cannot go through _run
    p = subprocess.run(["git", "hash-object", "--stdin-paths"], cwd=tree,
                       input="\n".join(present) + "\n", capture_output=True,
                       text=True, env=dict(os.environ, HOME=home))
    got = p.stdout.split()
    mismatch = [pth for pth, h in zip(present, got) if want[pth] != h]
    return len(present), mismatch, missing


def check_runtime(inst_dir, record):
    lock = os.path.join(inst_dir, "env", "runtime_lock.txt")
    rp = ((record or {}).get("replay") or {}).get("runtime_portability")
    if not os.path.exists(_p(lock)):
        return False, "no runtime_lock.txt (runtime_portability=%s)" % rp
    text = _read(lock)
    pins = [l for l in text.splitlines() if l.strip()]
    bad = [l for l in pins if "==" not in l]
    if bad:
        return False, "%d unpinned line(s), e.g. %r" % (len(bad), bad[0][:60])
    return True, "%d pins, sha=%s" % (
        len(pins), hashlib.sha256(text.encode()).hexdigest()[:12])


def build_env(lock_path, work, home):
    """venv from a lock, using only the lock -> (python, note)."""
    venv = os.path.join(work, "venv-" + hashlib.sha256(
        _read(lock_path).encode()).hexdigest()[:8])
    if os.path.exists(os.path.join(venv, "bin", "python")):
        return os.path.join(venv, "bin", "python"), "cached"
    rc, out = _run([sys.executable, "-m", "venv", venv], home=home, timeout=300)
    if rc != 0:
        return None, "venv creation failed: %s" % out.strip()[:200]
    py = os.path.join(venv, "bin", "python")
    rc, out = _run([py, "-m", "pip", "install", "--no-cache-dir", "-r", _p(lock_path)],
                   home=home, timeout=3600)
    if rc != 0:
        tail = "\n".join(out.strip().splitlines()[-4:])
        return None, "pip install failed: %s" % tail[:300]
    return py, "built"


def check_red(inst_dir, task, py, work, home):
    """The decisive check: at the START state with the test files overlaid, the
    fail_to_pass tests must NOT pass. Anything else means the instance cannot be
    used as a task, whatever the record claims."""
    start, _ = materialize(inst_dir, "env_start", work, home)
    end, _ = materialize(inst_dir, "env_end", work, home)
    overlaid = 0
    for rel in task.get("test_files") or []:
        s = os.path.join(end, rel)
        if not os.path.exists(s):
            continue
        d = os.path.join(start, rel)
        os.makedirs(os.path.dirname(d) or start, exist_ok=True)
        shutil.copyfile(s, d)
        overlaid += 1
    pp = os.pathsep.join(task.get("test_pythonpath") or ["."])
    red = notred = 0
    detail = []
    for node in task["fail_to_pass"]:
        rc, out = _run([py, "-m", "pytest", "-q", "--no-header", node], cwd=start,
                       env={"PYTHONPATH": pp, "PYTHONDONTWRITEBYTECODE": "1"},
                       home=home, timeout=600)
        if rc == 0:
            notred += 1
            detail.append("PASSED-at-start: " + node.split("::")[-1])
        elif rc in (4, 5) or "no tests ran" in out:
            notred += 1
            detail.append("not-collected: " + node.split("::")[-1])
        else:
            red += 1
    ok = red > 0 and notred == 0
    return ok, "overlaid=%d red=%d not-red=%d%s" % (
        overlaid, red, notred, ("; " + "; ".join(detail[:3])) if detail else "")


# --- driver --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir")
    ap.add_argument("--build", action="store_true",
                    help="build a venv per distinct lock (needs network) and run the "
                         "RED check")
    ap.add_argument("--max-build", type=int, default=0,
                    help="cap how many instances get the RED check (0 = all buildable)")
    ap.add_argument("--manifest-sample", type=int, default=0,
                    help="hash only the first N manifest entries (0 = all)")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    root = os.path.abspath(args.export_dir)
    _p(root)
    insts = sorted(d for d in os.listdir(root)
                   if os.path.isdir(os.path.join(root, d)))
    print("export: %s" % root)
    print("instances: %d   (guard: %s)" % (len(insts), " ".join(FORBIDDEN)))
    print()

    work = tempfile.mkdtemp(prefix="agentcap-consume-")
    home = os.path.join(work, "home")
    os.makedirs(home, exist_ok=True)
    rows = []
    try:
        for sid in insts:
            d = os.path.join(root, sid)
            row = {"sid": sid}
            task = record = None
            try:
                record = _json(os.path.join(d, "record.json"))
            except Exception as e:
                row["skip"] = "record.json unreadable: %s" % e
                rows.append(row); continue
            tp = os.path.join(d, "task.json")
            if os.path.exists(_p(tp)):
                task = _json(tp)
            row["verified_claimed"] = bool((task or {}).get("verified"))
            if not task:
                row["skip"] = "no task.json (not a task instance)"
                rows.append(row); continue

            row["prompt"], row["prompt_note"] = check_prompt(task)
            row["verifier"], row["verifier_note"] = check_verifier(task)
            row["runtime"], row["runtime_note"] = check_runtime(d, record)

            iwork = os.path.join(work, sid[:40])
            os.makedirs(iwork, exist_ok=True)
            try:
                tree, note = materialize(d, "env_start", iwork, home)
                n, bad, miss = verify_manifest(tree, d, "env_start", home,
                                               args.manifest_sample or None)
                row["artifact"] = not bad and not miss
                row["artifact_note"] = "%s, hashed %d, mismatch %d, missing %d" % (
                    note, n, len(bad), len(miss))
                if bad or miss:
                    row["artifact_note"] += "; e.g. %r" % (bad or miss)[0]
            except GuardViolation:
                raise
            except Exception as e:
                row["artifact"] = False
                row["artifact_note"] = "%s: %s" % (type(e).__name__, e)
            shutil.rmtree(iwork, ignore_errors=True)
            rows.append(row)
            flag = lambda b: "ok " if b else "FAIL"
            print("%-46s prompt=%s verifier=%s artifact=%s runtime=%s" % (
                sid[:46], flag(row["prompt"]), flag(row["verifier"]),
                flag(row["artifact"]), flag(row["runtime"])))
            for k in ("prompt", "verifier", "artifact", "runtime"):
                if not row[k]:
                    print("      %-9s %s" % (k + ":", row["%s_note" % k]))

        # ---- stage B: build each distinct lock, then the RED check ------------
        if args.build:
            print("\n--- building environments from locks (network) ---")
            built, n_red = {}, 0
            for row in rows:
                if row.get("skip") or not all(row.get(k) for k in
                                              ("prompt", "verifier", "artifact", "runtime")):
                    continue
                if args.max_build and n_red >= args.max_build:
                    break
                d = os.path.join(root, row["sid"])
                lock = os.path.join(d, "env", "runtime_lock.txt")
                key = hashlib.sha256(_read(lock).encode()).hexdigest()[:12]
                if key not in built:
                    py, note = build_env(lock, work, home)
                    built[key] = py
                    print("  lock %s -> %s" % (key, note if py else "FAILED: " + note))
                py = built[key]
                if not py:
                    row["red"] = False; row["red_note"] = "environment could not be built"
                    continue
                iwork = os.path.join(work, "red-" + row["sid"][:32])
                os.makedirs(iwork, exist_ok=True)
                task = _json(os.path.join(d, "task.json"))
                try:
                    row["red"], row["red_note"] = check_red(d, task, py, iwork, home)
                except Exception as e:
                    row["red"] = False
                    row["red_note"] = "%s: %s" % (type(e).__name__, e)
                shutil.rmtree(iwork, ignore_errors=True)
                n_red += 1
                print("  %-44s red=%s  %s" % (row["sid"][:44],
                                              "ok " if row["red"] else "FAIL",
                                              row["red_note"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ---- the number that did not exist before ---------------------------------
    tasks = [r for r in rows if not r.get("skip")]
    structural = [r for r in tasks if all(r.get(k) for k in
                                          ("prompt", "verifier", "artifact", "runtime"))]
    print("\n" + "=" * 66)
    print("instance dirs        : %d" % len(rows))
    print("task instances       : %d" % len(tasks))
    print("  claimed verified   : %d" % sum(1 for r in tasks if r["verified_claimed"]))
    for k in ("prompt", "verifier", "artifact", "runtime"):
        print("  %-18s : %d ok / %d" % (k, sum(1 for r in tasks if r.get(k)), len(tasks)))
    print("structurally usable from the export alone: %d / %d"
          % (len(structural), len(tasks)))
    if args.build:
        red = [r for r in rows if r.get("red")]
        tried = [r for r in rows if "red" in r]
        print("RED reproduced from the export alone     : %d / %d attempted"
              % (len(red), len(tried)))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(rows, f, indent=2, sort_keys=True)
        print("per-instance detail -> %s" % args.json_out)


if __name__ == "__main__":
    main()
