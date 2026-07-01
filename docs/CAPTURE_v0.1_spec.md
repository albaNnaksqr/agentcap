# agentcap — v0.1 spec: capture agent trajectory + environment at the source

> v0.1 supersedes v0 (`CAPTURE_v0_spec.md`) after a Codex technical feasibility review.
> The core architecture was judged **viable — nothing structurally infeasible**. The changes
> here fix where v0 **overstated reliability and safety**. Three reframes drive everything below:
> (1) the watcher is a *collection default, not a trust boundary*; (2) *public repo ≠ safe to
> publish*; (3) it's a *code snapshot + dependency declaration*, not a "reproducible environment."

## Problem / gap
Agent trajectory data today (from LLM proxy/relay logs, or tools like `dataclaw`) captures
the model↔human conversation + tool calls, but **not the environment** (filesystem/git state)
the agent operated on. Everyone reconstructs the env *after the fact* — lossy, the hard 90%.
The env can only be captured **where the agent runs** (the developer's machine); a relay/proxy
in the network middle can never see the agent's filesystem.

## Thesis
A lightweight, zero-friction tool on the developer's machine that captures, per agent coding
session in a git repo: the **trajectory** + a **verifiable git-working-tree snapshot** at
session start and end. The paired unit `{initial tree, trajectory, final tree, delta}` is a
real, environment-grounded data unit — obtained by **capture, not reconstruction**.

### Fidelity contract (say exactly this, no more)
- ✅ **Reconstructs git working-tree *contents*** (tracked files at `base_sha` + captured diff
  + selected untracked files) — verifiably, hash-checked.
- ❌ **Does NOT reproduce the runtime environment** (OS packages, compiler/interpreter versions,
  native libs, env vars, services, DBs, GPU/CUDA, network deps, credentials). Those are out of
  scope; we capture a **dependency *declaration*** (lockfile), not a runnable image.
- Every capture carries **confidence metadata** (start/end/join/reconstruction). Low-confidence
  captures are usable as fuel but **must not** back a benchmark.

## Architecture — hybrid capture model
Codex's recommended shape: watcher for coverage, optional hooks for fidelity, a robust source
primitive, confidence on everything, and reconstruction-validation built into the flow.

- **Watcher** (background daemon; launchd/systemd) — the **default, zero-friction collection
  mechanism, NOT the correctness boundary.** Watches agent session dirs
  (`~/.claude/projects/*`, `~/.codex/sessions/*`) via **versioned per-agent adapters** (these
  dirs are private implementation details of other tools — treat each as a tested adapter, not
  a stable API). Plus **periodic reconciliation** to survive sleep/crash/missed FS events.
- **Optional high-fidelity hooks** (opt-in, when available): a CLI/editor/shell integration or
  a manual `agentcap mark-start` / `mark-end` that pins exact session boundaries. When a hook
  fires, boundary confidence = `high`; watcher-only = `best-effort`.
- **Snapshot** — capture the working tree (schema below), recording `snapshot_inconsistent`
  if the tree mutates mid-capture.
- **Store** — content-addressed blob store under `~/.agentcap/` with dedup, compression, and
  size caps (see Storage).
- **Join** — correlate captures with the trajectory by session id / cwd / time overlap, emitting
  a **join_confidence** (not a boolean) + the signals; manual repair supported.
- **Verify** — `agentcap verify <capture>` reconstructs start/end into a clean temp dir and
  compares hashes. **Required to pass before any publish.**
- **Publish** (opt-in) — strict safety gate (below); reference-format only.

## Env snapshot — schema (host-agnostic; NO Docker images)
v0's `diff = git diff HEAD` was underspecified. Capture, separately:
```
base_sha            = git rev-parse HEAD
staged_diff         = git diff --cached --binary --full-index
unstaged_diff       = git diff        --binary --full-index
untracked[]         = tar stream of `git status --porcelain --untracked=all` files,
                      with mode/symlink/exec-bit metadata (respecting size caps)
deleted[]           = tracked files removed in the worktree
submodules[]        = { path, sha, dirty }        # submodule manifest
lfs                 = detected? which paths are LFS pointers vs materialized objects
git_meta            = branch, remotes, relevant core.* / .gitattributes / info/exclude
lockfiles[]         = uv.lock | poetry.lock | package-lock.json | requirements.txt | ...
snapshot_inconsistent = bool                       # tree changed during capture
```
**Consistency:** take a fast index/worktree read first, then process; if state changes mid-capture,
set `snapshot_inconsistent: true` rather than silently emitting a torn snapshot.

### Two encodings
- **Reference format** (canonical for publishable/public work): the schema above, no file
  archive. Reconstruct: `clone remote → checkout base_sha → apply staged+unstaged diffs →
  write untracked/submodules`. Requires `base_sha` **anonymously fetchable** from the remote.
  Works for GitHub / **GitLab** / Bitbucket / self-hosted — any git URL.
- **Bundle format** (private / no reachable remote / unpushed base) — **`git bundle` + working-
  tree overlay**, replacing v0's `git archive`. A bundle preserves the commit graph and refs
  (so `git blame` / `merge-base` / `describe` and tools expecting a real repo still work);
  archive gives only a tree. **Private / local-only, never published.**

## Confidence metadata (on every capture)
```json
{
  "start_confidence": "high | best-effort",
  "start_snapshot_after_first_event_ms": 1234,
  "end_confidence":   "high | best-effort",
  "join_confidence":  "high | medium | low",
  "join_signals":     ["session_id", "cwd", "time_overlap", "agent_log_path"],
  "reconstruction_verified": true
}
```
`start_confidence=best-effort` means the session file appeared after the agent's first action,
so the initial diff may miss the earliest edits. `base_sha` is still stable, so the base is
sound; the delta is what's at risk. Benchmarks consume only `high` + `reconstruction_verified`.

## Publish safety gate (the hard, non-optional part)
**"public repo ≠ safe to publish."** A public repo + local diff/untracked files can still leak
private code, customer logic, unreleased patches, or credentials. Publishing is **default-off**
and passes only if **all** hold:
- reference format only (never publish bundle/archive);
- every referenced remote + `base_sha` is **anonymously fetchable** (don't infer publishability
  from the host name);
- **secret scan passes** (defense in depth — see below);
- **`agentcap verify` reconstructs cleanly** (hashes match) in a fresh temp dir;
- no private submodules; no oversized untracked blobs;
- diff introduces no files absent from the public repo **unless the user explicitly confirms**;
- provenance + license preserved;
- final **explicit user confirmation** on a dry-run report.

### Secret scanning (HARD — reduces risk, never guarantees)
Skipping `.env` is not enough. Secrets live in source, fixtures, logs, tool/terminal output,
stack traces, notebooks, lockfiles, commit messages, remote URLs, and untracked files —
**and in the trajectory itself**, not just env files. Layers:
denylist paths · entropy + pattern scanner · known-provider scanners · binary reject/quarantine ·
manual review mode · publish dry-run report · never publish bundle format · publishing default-off.

## Storage (build the limits early — retrofitting hurts)
Content-addressed blob store · gzip/zstd compression · hard max capture size · hard max per-file
size · default ignored-path denylist (node_modules-like trees, build outputs, vendored deps) ·
cross-session dedup · retention limits · `agentcap gc` · `"capture skipped due to size"` metadata.

## Data unit produced
Per session: `{session_id, repo, agent, env_start, trajectory (steps + tool outputs), env_end,
delta, confidence_metadata}`. If the trajectory contains test runs → a self-verifying task.
Distributed public-repo publishing (dataclaw-style HF tag) → a diverse, env-grounded corpus
(which also dissolves the "single-source can't be a benchmark" problem).

## v0.1 scope
- Git projects only.
- Reference + bundle encodings for CODE reconstruction; lockfile capture for deps declaration.
- Watcher (versioned adapters) for Claude Code + Codex, + periodic reconciliation; optional
  `mark-start`/`mark-end` hook.
- Confidence metadata on every capture.
- `agentcap verify` (reconstruct + hash-compare).
- Local storage with dedup/caps/gc; manual `agentcap publish` behind the full safety gate.

## Non-goals (v0.1)
- Runtime/environment reproduction (only tree contents + lockfile declaration).
- Non-git environments (databases, services, remote state).
- Real-time/streaming capture (start/end snapshots + confidence suffice).
- Guaranteeing zero leakage or exact session-boundary detection — both are best-effort with
  explicit confidence, not promises.

## Positioning (one line)
> **agentcap captures verifiable git working-tree snapshots around local agent sessions, with
> best-effort trajectory joins.** — not "full environment capture" or "guaranteed reproducibility."

## Resolved from v0's open questions (Codex review)
1. **Session-boundary detection** — not a correctness guarantee; fine as a v0 heuristic if
   confidence is recorded. → watcher-first + reconciliation + optional hook + confidence.
2. **Reference fidelity** — reproduces tracked contents + selected untracked, **not** the exact
   host env. → `--binary --full-index`, separate staged/unstaged, submodule manifest, LFS
   detection, git-config capture.
3. **Unpushed base** — **`git bundle`** for robustness (private); anonymously-fetchable `base_sha`
   for public. Nearest-ancestor+spanning-diff only as an advanced, validation-gated public path.
4. **Start lag** — not acceptable *if claimed as the true initial env*; acceptable as
   `best-effort` with `start_snapshot_after_first_event_ms`; exclude low-confidence from benchmarks.
5. **Public/private detection** — cannot guarantee safety by itself → the full publish gate above.
6. **Storage** — real but manageable → dedup/caps/compression/gc from day one.
7. **Structural** — nothing infeasible; the hybrid model above is the materially better shape.

## Build order (suggested)
1. Snapshot engine + schema + `agentcap verify` (the trust core — reconstruct & hash-compare).
2. Watcher with one versioned adapter (Claude Code) + `mark-start/end` hook + confidence.
3. Storage (dedup/caps/gc).
4. Join (with confidence) against a dataclaw export.
5. Publish gate + secret scan + HF push (last — most safety-sensitive).
