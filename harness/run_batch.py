#!/usr/bin/env python3
"""
Layer-1 capture harness for the osmind x agentcap batch (plan B).

This is deliberately dumb and deterministic. It does NOT solve issues — it only
frames each coding session so agentcap can capture and replay it:

    for each queued issue:
        1. add a fresh git worktree at a clean base commit
        2. agentcap mark-start   (snapshot BEFORE any edit -> honest RED base)
        3. codex exec            (Layer 2: codex solves the issue, all intelligence here)
        4. agentcap mark-end     (snapshot AFTER -> delta)

The measured agent (codex) never sets its own capture boundary — steps 2 and 4
are exogenous. That ordering is the whole point (see contract.md rationale).

After the batch, run the funnel yourself:
    python3 -m agentcap join && seed && value && replay && export

Queue format: one JSON object per line (batch_queue.jsonl):
    {"repo": "/home/kps_spark/workspace/osmind", "issue_no": 42,
     "difficulty": "easy", "base": "main",
     "pack_path": "packs/osmind-42.md"}     # or inline "pack": "..."
`base` and `difficulty` are optional. Exactly one of pack_path / pack is used
if present; otherwise the contract runs with an empty issue slot (not advised).

Docker-only runtimes (slime, sglang-omni): add "docker_image" (and optionally
"docker_workdir", default /work) to the queue item. codex still runs on the
host — it has to, the image has no codex CLI and agentcap's capture boundary is
host-side — but the runner verifies the image up front and appends an exact
`docker run` recipe to the prompt, so every command codex measures runs inside
the image against THIS worktree. Without it each pack hand-rolls the mount and
they drift.

stdlib only.
"""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path

PACK_SLOT = "<<< PASTE osmind pack / issue body here >>>"


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def sh(cmd, **kw):
    """Run a command, return CompletedProcess (never raises on nonzero)."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def agentcap_env(args) -> dict:
    """Env for `python3 -m agentcap`: agentcap isn't pip-installed, so put its
    repo root on PYTHONPATH (it's only importable with cwd on sys.path otherwise)."""
    root = os.path.abspath(os.path.expanduser(args.agentcap_root))
    env = dict(os.environ)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def contract_provenance(contract_path: Path) -> dict:
    """Which rules this batch was judged under.

    The contract carries the anti-gaming clauses — reproduce first, do not
    special-case the input, do not weaken existing tests — so it IS the standard a
    captured session is later held to. Recording only "a contract was used" makes
    an old capture unauditable once the text moves on; record its content hash, and
    its commit when it lives in a repo.
    """
    prov = {"path": contract_path.name}
    try:
        prov["sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()[:16]
    except OSError:
        return prov
    d = contract_path.resolve().parent
    r = sh(["git", "-C", str(d), "log", "-1", "--format=%H", "--", contract_path.name])
    if r.returncode == 0 and r.stdout.strip():
        prov["commit"] = r.stdout.strip()[:12]
        dirty = sh(["git", "-C", str(d), "status", "--porcelain", "--", contract_path.name])
        prov["uncommitted"] = bool(dirty.stdout.strip())
    return prov


def load_queue(path: Path) -> list[dict]:
    items = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as e:
            sys.exit(f"queue line {i}: bad JSON: {e}")
    return items


def issue_pack(item: dict, queue_dir: Path) -> str:
    if item.get("pack"):
        return item["pack"]
    if item.get("pack_path"):
        p = Path(item["pack_path"])
        if not p.is_absolute():
            p = queue_dir / p
        return p.read_text()
    return f"(no pack provided) Fix issue #{item.get('issue_no', '?')} in this repo."


DOCKER_RUN_TEMPLATE = (
    'docker run --rm --gpus all --ipc=host --shm-size=16g '
    '--ulimit memlock=-1 --ulimit stack=67108864 '
    '-v "{wt}:{workdir}" -w {workdir} {image} <command>'
)


def docker_block(item: dict, wt: Path, args) -> str:
    """Exec recipe for repos whose runtime is an image, not a host env.

    Returns "" when the item has no docker_image. The recipe is appended to the
    prompt so every command codex measures runs inside the image against this
    worktree — same mount, same flags, every time.
    """
    image = item.get("docker_image") or args.docker_image
    if not image:
        return ""
    workdir = item.get("docker_workdir", "/work")
    recipe = DOCKER_RUN_TEMPLATE.format(wt=os.path.abspath(str(wt)), workdir=workdir, image=image)
    return (
        "\n\n## Runtime — this repo runs in a container, not on the host\n\n"
        "The host has no working interpreter for this repo. Run EVERY command you "
        "measure (imports, tests, repro scripts) inside the image, with this exact "
        "invocation:\n\n"
        f"```bash\n{recipe}\n```\n\n"
        f"Your worktree is mounted at `{workdir}`, so repo-relative paths work unchanged. "
        "Edit files on the host as usual — the mount is live.\n\n"
        "- Do NOT `pip install` or rebuild anything inside the container; the image is "
        "already provisioned and changes to it are discarded (`--rm`) anyway.\n"
        "- Do NOT fall back to the host interpreter to get a green run. If the image "
        "cannot run your test, stop and report that instead.\n"
    )


def compose_prompt(contract: str, pack: str) -> str:
    if PACK_SLOT in contract:
        return contract.replace(PACK_SLOT, pack.strip())
    # fall back: append if the slot marker was edited away
    return contract.rstrip() + "\n\n## Issue\n" + pack.strip() + "\n"


def run_one(item: dict, args, contract: str, queue_dir: Path, logroot: Path) -> dict:
    repo = os.path.abspath(os.path.expanduser(item["repo"]))
    issue_no = item.get("issue_no", "x")
    base = item.get("base", args.default_base)
    tag = f"{Path(repo).name}-{issue_no}-{datetime.now():%Y%m%d-%H%M%S}"
    wt = Path(args.worktrees).expanduser() / tag
    logdir = logroot / tag
    logdir.mkdir(parents=True, exist_ok=True)
    rec: dict = {"repo": repo, "issue_no": issue_no, "difficulty": item.get("difficulty"),
                 "base": base, "worktree": str(wt), "logdir": str(logdir), "tag": tag}

    image = item.get("docker_image") or args.docker_image
    if image:
        rec["docker_image"] = image
    print(f"\n=== {tag}  (base={base}, difficulty={item.get('difficulty','?')}"
          f"{', image=' + image if image else ''}) ===")

    # fail fast: a missing image means every measured command in this session
    # would silently fall back to the host, which is exactly what we forbid.
    if image and not args.dry_run:
        di = sh(["docker", "image", "inspect", image])
        if di.returncode != 0:
            rec["status"] = "docker_image_missing"; rec["error"] = di.stderr.strip()
            print(f"  ! docker image not found: {image}"); return rec

    if args.dry_run:
        rec["status"] = "dry_run"
        print("  [dry-run] would create worktree, mark-start, codex exec, mark-end")
        return rec

    # 1. fresh worktree at clean base
    r = sh(["git", "-C", repo, "worktree", "add", "--detach", str(wt), base])
    if r.returncode != 0:
        rec["status"] = "worktree_failed"; rec["error"] = r.stderr.strip()
        print("  ! worktree add failed:", r.stderr.strip()); return rec
    rec["base_sha"] = sh(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()

    # 2. mark-start BEFORE any edit
    ms = sh(["python3", "-m", "agentcap", "mark-start", str(wt), "--agent", "codex"],
            env=agentcap_env(args))
    if ms.returncode != 0:
        rec["status"] = "mark_start_failed"; rec["error"] = ms.stderr.strip()
        print("  ! mark-start failed:", ms.stderr.strip()); return rec
    try:
        sid = json.loads(ms.stdout)["session_id"]
    except Exception:
        rec["status"] = "mark_start_parse_failed"; rec["raw"] = ms.stdout
        print("  ! could not parse mark-start output"); return rec
    rec["session_id"] = sid
    print(f"  mark-start ok  session_id={sid}")

    # 3. codex exec — Layer 2, codex owns everything here
    pack = issue_pack(item, queue_dir)
    prompt = compose_prompt(contract, pack) + docker_block(item, wt, args)
    (logdir / "prompt.md").write_text(prompt)
    cmd = ["codex", "exec", "-C", str(wt), "-o", str(logdir / "last_message.txt")]
    if args.bypass_sandbox:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if args.model:
        cmd += ["-m", args.model]
    if args.reasoning:
        cmd += ["-c", f"model_reasoning_effort={args.reasoning}"]
    cmd.append("-")  # read prompt from stdin
    # optional: put a prebuilt venv on PATH so codex's `python`/`pytest` resolve to an
    # interpreter where the target repo + its test deps are installed (Track-A repos).
    cx_env = None
    if args.venv:
        vbin = os.path.join(os.path.abspath(os.path.expanduser(args.venv)), "bin")
        cx_env = dict(os.environ)
        cx_env["PATH"] = vbin + os.pathsep + cx_env.get("PATH", "")
        cx_env["VIRTUAL_ENV"] = os.path.dirname(vbin)
        cx_env.pop("PYTHONHOME", None)
    t0 = time.time()
    try:
        cx = subprocess.run(cmd, input=prompt, text=True, timeout=args.timeout, env=cx_env,
                            stdout=open(logdir / "codex.stdout", "w"),
                            stderr=open(logdir / "codex.stderr", "w"))
        rec["codex_exit"] = cx.returncode
        rec["codex_timed_out"] = False
    except subprocess.TimeoutExpired:
        rec["codex_exit"] = None; rec["codex_timed_out"] = True
        print(f"  ! codex timed out after {args.timeout}s")
    rec["codex_secs"] = round(time.time() - t0, 1)
    print(f"  codex done  exit={rec.get('codex_exit')}  {rec['codex_secs']}s")

    # 4. mark-end (always close the boundary, even if codex crashed/timed out)
    me = sh(["python3", "-m", "agentcap", "mark-end", "--repo", str(wt),
             "--session-id", sid], env=agentcap_env(args))
    if me.returncode != 0:
        rec["status"] = "mark_end_failed"; rec["error"] = me.stderr.strip()
        print("  ! mark-end failed:", me.stderr.strip()); return rec
    try:
        rec["delta"] = json.loads(me.stdout).get("delta")
    except Exception:
        pass
    rec["status"] = "captured"
    print(f"  mark-end ok  delta={rec.get('delta')}")

    if args.rm_worktree:
        sh(["git", "-C", repo, "worktree", "remove", "--force", str(wt)])
        rec["worktree_removed"] = True
    return rec


def main():
    ap = argparse.ArgumentParser(description="Layer-1 capture harness (plan B).")
    ap.add_argument("--queue", required=True, help="batch_queue.jsonl")
    ap.add_argument("--contract", default=str(Path(__file__).parent / "contract_seedable_tdd.md"),
                    help="task contract prepended to every issue pack; versioned next to "
                         "this script so a capture can be tied to the rules it ran under")
    ap.add_argument("--agentcap-root", default=str(Path(__file__).resolve().parent.parent),
                    help="agentcap repo root (put on PYTHONPATH; it isn't pip-installed)")
    ap.add_argument("--worktrees", default="~/wt", help="dir to hold per-issue worktrees")
    ap.add_argument("--logs", default=None,
                    help="run log dir (default: ~/workspace/batch_b/runs/<ts>). Deliberately "
                         "OUTSIDE this repo: prompts and per-issue logs are run products, not "
                         "source, and a script-relative default would quietly fill the repo")
    ap.add_argument("--default-base", default="main", help="base ref if a queue item omits it")
    ap.add_argument("--timeout", type=int, default=1800, help="per-issue codex timeout (s)")
    ap.add_argument("--model", default=None, help="pass to codex -m (optional)")
    ap.add_argument("--venv", default=None,
                    help="prebuilt venv whose bin/ is prepended to PATH for the codex step "
                         "(so python/pytest resolve to the repo's installed env)")
    ap.add_argument("--docker-image", default=None,
                    help="fallback image for items without their own docker_image; when set, "
                         "the prompt carries an exact `docker run` recipe and the image is "
                         "verified before codex starts")
    ap.add_argument("--reasoning", default=None,
                    help="codex reasoning effort, injected as -c model_reasoning_effort=<v> (e.g. high)")
    ap.add_argument("--bypass-sandbox", action="store_true", default=True,
                    help="run codex with --dangerously-bypass-approvals-and-sandbox (worktrees are disposable)")
    ap.add_argument("--no-bypass-sandbox", dest="bypass_sandbox", action="store_false")
    ap.add_argument("--rm-worktree", action="store_true",
                    help="remove worktree after mark-end (capture is already in the store)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    queue_path = Path(args.queue).expanduser()
    contract = Path(args.contract).expanduser().read_text()
    items = load_queue(queue_path)
    logroot = Path(args.logs).expanduser() if args.logs else \
        Path.home() / "workspace/batch_b/runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    logroot.mkdir(parents=True, exist_ok=True)
    prov = contract_provenance(Path(args.contract).expanduser())
    print(f"queue: {len(items)} issues | contract: {args.contract} {prov}\nlogs:  {logroot}")
    _write_json(logroot / "run_meta.json",
                {"contract": prov, "queue": str(queue_path), "model": args.model,
                 "reasoning": args.reasoning, "started_at": datetime.now().isoformat()})

    summary = []
    for item in items:
        try:
            summary.append(run_one(item, args, contract, queue_path.parent, logroot))
        except Exception as e:  # never let one issue kill the batch
            print("  !! unexpected error:", e)
            summary.append({"repo": item.get("repo"), "issue_no": item.get("issue_no"),
                            "status": "exception", "error": str(e)})
        (logroot / "summary.jsonl").write_text(
            "\n".join(json.dumps(s) for s in summary) + "\n")

    n = len(summary)
    cap = sum(1 for s in summary if s.get("status") == "captured")
    print(f"\n=== batch done: {cap}/{n} captured -> {logroot}/summary.jsonl")
    print("next: python3 -m agentcap join && seed && value && replay && export")


if __name__ == "__main__":
    main()
