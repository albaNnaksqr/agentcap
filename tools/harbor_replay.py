#!/usr/bin/env python3
"""Turn an agentcap export instance into a Harbor task package that REPLAYS it.

Why this is not the evaluation path
-----------------------------------
Harbor's task model assumes reference tests independent of the agent under
evaluation — trajmill gets that from PR history, where the tests were written by
the PR author and are hidden and path-protected. agentcap's tests were written by
the very agent that was captured, so "protected" would be an empty word there and
`reward` would be measuring whether a model can pass another model's test.

This tool uses Harbor for the other thing it owns: ENVIRONMENT LIFECYCLE. The
question it answers is the one open since 2026-08-18 —

    runtime_portability said `same_class`, and that was never checked on a
    second machine, because replay only ever ran on the machine that produced
    the capture.

The mapping that makes it work: treat the captured session's own source patch as
the candidate. Harbor then does exactly what it already does — record the patch,
refuse it if it touches a protected test path, overlay the reference tests, run
the fixed commands, write reward.json. reward=1 means the capture reproduced
green somewhere else. The RED control is the SAME package run with `-a nop`,
which leaves the patch unapplied -- Harbor's own agent slot gives both ends, so
one package carries the whole red->green, run by a plane agentcap does not own.

    harbor run -p <pkg> -a oracle   # applies solution/solve.sh -> expect reward 1
    harbor run -p <pkg> -a nop      # touches nothing            -> expect reward 0

Confirmed 2026-08-25 on codex-litellm-36999: oracle 1, nop 0, and
protected_test_integrity 1 in both, in a container built only from
runtime_lock.txt -- the capturing host venv was not on the path.

`protected_test_integrity` is not decorative here either: it checks that the
session's source patch and its test patch are cleanly separable, which is exactly
what taskseed's test_only_delta cares about and has never been enforced.

Scope, deliberately narrow
--------------------------
Only instances whose runtime is reproducible get packaged — in practice the ones
carrying env/runtime_lock.txt. A conda capture has no installable recipe and a
distro-python one describes a host; both are refused with a reason rather than
packaged into something that cannot start. That is the same line drawn in
record/task v5.

Usage
-----
    python3 tools/harbor_replay.py <export_dir>/<session_id> --output <dir>
    python3 tools/harbor_replay.py <export_dir> --output <dir> --all
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load_export as L  # noqa: E402  (sibling tool; stdlib-only, no agentcap import)

# A base image every venv lock can be installed into. Digest-pinned because
# task.toml requires it -- Harbor refuses an unpinned image, and rightly: an
# unpinned tag is the same class of claim as the portability field that started
# this whole thread.
DEFAULT_IMAGE = ("docker.io/library/python:3.12-slim@sha256:"
                 "afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63")


def _run(cmd, cwd=None, home=None, check=True):
    rc, out = L._run(cmd, cwd=cwd, home=home)
    if check and rc != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd), out.strip()[:300]))
    return out


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tar(src_dir, dest, arcname="."):
    with tarfile.open(dest, "w:gz") as t:
        t.add(src_dir, arcname=arcname, filter=_strip)


def _strip(info):
    # reproducible-ish archives: no owner names, no mtimes leaking the host
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def readiness(inst_dir):
    """(ok, reason). Refuse anything that cannot honestly start elsewhere."""
    task_p = os.path.join(inst_dir, "task.json")
    if not os.path.exists(L._p(task_p)):
        return False, "no task.json (not a task instance)"
    task = L._json(task_p)
    if not task.get("verified"):
        return False, "not verified — nothing to reproduce"
    if not task.get("fail_to_pass"):
        return False, "no fail_to_pass — no verifier spec"
    if not os.path.exists(L._p(os.path.join(inst_dir, "env", "runtime_lock.txt"))):
        rec = L._json(os.path.join(inst_dir, "record.json"))
        rp = ((rec.get("replay") or {}).get("runtime_portability"))
        return False, ("no runtime_lock.txt (runtime_portability=%s) — the "
                       "environment cannot be rebuilt elsewhere" % rp)
    if not task.get("test_files"):
        return False, "no test_files — reference tests cannot be separated"
    return True, "ok"


def build(inst_dir, out_dir, image=DEFAULT_IMAGE, work=None, home=None):
    """Write one Harbor task package. Returns a summary dict."""
    task = L._json(os.path.join(inst_dir, "task.json"))
    record = L._json(os.path.join(inst_dir, "record.json"))
    sid = os.path.basename(inst_dir.rstrip("/"))

    start, _ = L.materialize(inst_dir, "env_start", work, home)
    end, _ = L.materialize(inst_dir, "env_end", work, home)

    test_files = list(task["test_files"])
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "environment"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "tests"), exist_ok=True)

    # --- environment: the START tree, with the reference tests REMOVED --------
    # They ship separately and are overlaid only at verification, exactly as the
    # evaluation path does. Leaving them in the repo archive would hand the
    # candidate the oracle.
    for rel in test_files:
        p = os.path.join(start, rel)
        if os.path.exists(p):
            os.remove(p)
    _tar(start, os.path.join(out_dir, "environment", "repository.tar.gz"))

    # --- reference tests: the END versions of the test files ------------------
    staging = os.path.join(work, "refs-" + sid[:24])
    shutil.rmtree(staging, ignore_errors=True)
    for rel in test_files:
        src = os.path.join(end, rel)
        if not os.path.exists(src):
            continue
        dst = os.path.join(staging, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
    if not os.path.isdir(staging):
        raise RuntimeError("no reference test content found in the end tree")
    _tar(staging, os.path.join(out_dir, "tests", "reference-tests.tar.gz"))

    # --- the candidate: the session's SOURCE patch, tests excluded ------------
    # Excluded, not just unused: if the source patch touched a test path, Harbor's
    # own protected-path check would zero the reward, and that would be the
    # correct verdict about the capture rather than a packaging accident.
    # Remove the reference tests from BOTH trees before diffing. Taking them
    # out of START only makes the diff report each one as a whole new file --
        # the first build produced a 139 KB "source patch" that was mostly the
    # test file, i.e. it would have handed the oracle to the candidate through
    # the patch after carefully withholding it from the archive.
    for rel in test_files:
        for tree in (start, end):
            d = os.path.join(tree, rel)
            if os.path.exists(d):
                os.remove(d)
    patch = _run(["git", "-c", "core.fileMode=false", "diff", "--binary",
                  "--no-index", start, end], home=home, check=False)
    # `--no-index` writes absolute paths as `a/tmp/.../start/litellm/utils.py`.
    # Stripping "<start>/" WITH its trailing slash eats the slash that belongs
    # to `a/`, yielding `alitellm/utils.py`; `git apply -p1` then strips
    # `alitellm/` and dies with "utils.py: No such file or directory". Strip
    # the prefix without the trailing slash so `a/` survives intact.
    patch = patch.replace(start.rstrip("/"), "").replace(end.rstrip("/"), "")
    bad = [ln for ln in patch.splitlines()
           if ln.startswith(("--- ", "+++ "))
           and not re.match(r"^(---|\+\+\+) ([ab]/|/dev/null)", ln)]
    if bad:
        raise RuntimeError("patch paths not repo-relative: %s" % bad[:2])

    pp = ":".join(task.get("test_pythonpath") or ["."])
    commands = ["PYTHONPATH=%s python -m pytest -q %s" % (pp, node)
                for node in task["fail_to_pass"]]

    shutil.copyfile(os.path.join(inst_dir, "env", "runtime_lock.txt"),
                    os.path.join(out_dir, "environment", "runtime_lock.txt"))
    _write(os.path.join(out_dir, "environment", "Dockerfile"), _dockerfile(image))
    # Harbor has a first-class slot for a known-good fix, so the captured
    # session's patch belongs there rather than smuggled into environment setup.
    # Running the task WITHOUT the solution is the RED control and WITH it is the
    # GREEN reproduction -- replay's whole verdict, in Harbor's own vocabulary,
    # from one package instead of two.
    os.makedirs(os.path.join(out_dir, "solution"), exist_ok=True)
    _write(os.path.join(out_dir, "solution", "candidate.patch"), patch or "")
    _write(os.path.join(out_dir, "solution", "solve.sh"), _solve_sh())
    _write(os.path.join(out_dir, "tests", "test.sh"), _verifier_sh(commands, test_files))
    _write(os.path.join(out_dir, "instruction.md"), _instruction(task))
    _write(os.path.join(out_dir, "task.toml"), _task_toml(sid, task, record, image))
    prov = {
        "producer": "agentcap",
        "purpose": "replay",
        "session_id": sid,
        "repo": task.get("repo"),
        "base_commit": task.get("base_commit"),
        "fail_to_pass": task["fail_to_pass"],
        "pass_to_pass_count": len(task.get("pass_to_pass") or []),
        "runtime_lock_sha256": _sha256(os.path.join(inst_dir, "env", "runtime_lock.txt")),
        "artifact_portability": task.get("replay_artifact_portability"),
        "runtime_portability": task.get("replay_runtime_portability"),
        "reference_source": "self_authored",
        "reference_source_note":
            "The reference tests were written by the captured agent, not by an "
            "independent author. This package reproduces a recorded session; it is "
            "NOT an evaluation task and its reward must not be read as one.",
        "expected_reward": {"oracle": 1, "nop": 0},
    }
    _write(os.path.join(out_dir, "trajmill-provenance.json"),
           json.dumps(prov, indent=2, sort_keys=True) + "\n")
    return {"session": sid, "out": out_dir,
            "tests": len(test_files), "commands": len(commands),
            "patch_bytes": len(patch)}


def _dockerfile(image):
    """The environment is built, not assumed. The lock is installed at BUILD time
    so the verifier can run with no network -- if a pin has vanished from the
    index, the build fails loudly here instead of a test failing for a reason
    that has nothing to do with the capture."""
    return """FROM %s

RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY runtime_lock.txt /environment/runtime_lock.txt
RUN pip install --no-cache-dir -r /environment/runtime_lock.txt

COPY repository.tar.gz /environment/repository.tar.gz
RUN tar -xzf /environment/repository.tar.gz -C /app \\
    && rm /environment/repository.tar.gz

# A clean synthetic baseline so the verifier's `git diff` recovers exactly the
# candidate's own changes and nothing else.
RUN git init -q . \\
    && git add -A \\
    && git -c user.email=replay@agentcap -c user.name=agentcap \\
       commit -q -m "agentcap replay baseline"
""" % image


def _solve_sh():
    return """#!/bin/bash
set -euo pipefail
cd /app
# The captured session's source patch. Reference tests are excluded from it by
# construction, so applying this must not touch a protected path.
git apply --whitespace=nowarn /solution/candidate.patch
"""


def _verifier_sh(commands, protected):
    lines = "\n".join("run_test %s" % json.dumps(c) for c in commands)
    prot = "\n".join(json.dumps(p) for p in protected)
    return """#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
cd /app
git diff --binary > /logs/verifier/candidate.patch
git status --porcelain=v1 > /logs/verifier/git-status.txt

protected_paths=(
%s
)
changed="$(git status --porcelain=v1 | sed -E 's/^.. //' | sed -E 's/.* -> //')"
for path in "${protected_paths[@]}"; do
  if printf '%%s\\n' "$changed" | grep -Fqx -- "$path"; then
    printf '%%s\\n' "candidate modified protected test: $path" > /logs/verifier/test-output.txt
    printf '0\\n' > /logs/verifier/reward.txt
    printf '{"reward":0,"protected_test_integrity":0}\\n' > /logs/verifier/reward.json
    exit 0
  fi
done

tar -xzf /tests/reference-tests.tar.gz -C /app
: > /logs/verifier/test-output.txt
passed=1
run_test() {
  printf '\\n$ %%s\\n' "$1" >> /logs/verifier/test-output.txt
  bash -lc "$1" >> /logs/verifier/test-output.txt 2>&1 || passed=0
}
%s
printf '%%s\\n' "$passed" > /logs/verifier/reward.txt
printf '{"reward":%%s,"protected_test_integrity":1}\\n' "$passed" > /logs/verifier/reward.json
exit 0
""" % (prot, lines)


def _instruction(task):
    return ("# agentcap replay\n\n"
            "This package reproduces a recorded coding session on a machine other than\n"
            "the one that captured it. The session's own source patch sits in\n"
            "`solution/solve.sh`; the verifier overlays the reference tests and runs them.\n\n"
            "Run it two ways:\n\n"
            "    harbor run -p <pkg> -a oracle   # applies the patch -> expected reward 1\n"
            "    harbor run -p <pkg> -a nop      # applies nothing   -> expected reward 0\n\n"
            "oracle scoring 0 means the capture does NOT reproduce here, which is the\n"
            "finding this package exists to surface. nop scoring 1 means the reference\n"
            "tests were already green in the START state, so the capture proved nothing.\n\n"
            "No agent action is required in either case.\n")


def _task_toml(sid, task, record, image):
    """Harbor 0.21 schema 1.4 -- the shape `harbor task init` itself emits.

    The verifier runs with no network on purpose: everything it needs was
    installed at image build time, so a test that fails here failed for a reason
    belonging to the capture rather than to an index lookup."""
    name = "agentcap/%s" % sid[:48].rstrip("-")
    return """schema_version = "1.4"
artifacts = []

[task]
name = %s
version = "1.0.0"
description = "agentcap replay of a captured session (execution plane, not evaluation)"
authors = []
keywords = ["agentcap", "replay", "portability"]

[metadata]
producer = "agentcap"
purpose = "replay"
session_id = %s
repo = %s
base_commit = %s
reference_source = "self_authored"
expected_reward_with_solution = 1
expected_reward_without_solution = 0

[verifier]
timeout_sec = 1800.0
network_mode = "no-network"
# `collect` takes VerifierCollectConfig objects, not paths; left empty rather
# than guessed at. The verifier writes candidate.patch and test-output.txt into
# /logs/verifier either way, which is where Harbor looks.
collect = []

[verifier.env]

[agent]
timeout_sec = 600.0

[environment]
# NO docker_image here on purpose: when it is set Harbor uses that image as-is
# and never builds environment/Dockerfile, so the container arrives without the
# repository or the runtime lock and solve.sh dies with 127. The Dockerfile IS
# the environment definition; the pinned base is its FROM line.
workdir = "/app"
network_mode = "public"
build_timeout_sec = 1800.0
os = "linux"
mcp_servers = []

[environment.env]

[solution.env]
""" % (json.dumps(name), json.dumps(sid), json.dumps(task.get("repo") or ""),
       json.dumps(task.get("base_commit") or ""))


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)
    if path.endswith(".sh"):
        os.chmod(path, 0o755)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="an export instance dir, or an export dir with --all")
    ap.add_argument("--output", required=True)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    args = ap.parse_args()

    src = os.path.abspath(args.source)
    insts = ([os.path.join(src, d) for d in sorted(os.listdir(src))
              if os.path.isdir(os.path.join(src, d))] if args.all else [src])

    import tempfile
    work = tempfile.mkdtemp(prefix="agentcap-harbor-")
    home = os.path.join(work, "home")
    os.makedirs(home, exist_ok=True)
    built, refused = [], []
    try:
        for inst in insts:
            ok, why = readiness(inst)
            if not ok:
                refused.append((os.path.basename(inst), why))
                continue
            name = os.path.basename(inst)[:48].rstrip("-")
            out = os.path.join(os.path.abspath(args.output), name)
            iwork = os.path.join(work, name[:32])
            os.makedirs(iwork, exist_ok=True)
            try:
                built.append(build(inst, out, image=args.image,
                                   work=iwork, home=home))
                print("built  %s  (%d test files, %d commands, patch %d B)"
                      % (name, built[-1]["tests"], built[-1]["commands"],
                         built[-1]["patch_bytes"]))
            except Exception as e:
                refused.append((os.path.basename(inst), "%s: %s" % (type(e).__name__, e)))
            shutil.rmtree(iwork, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\nbuilt %d, refused %d" % (len(built), len(refused)))
    for n, why in refused[:12]:
        print("  refused %-46s %s" % (n[:46], why))


if __name__ == "__main__":
    main()
