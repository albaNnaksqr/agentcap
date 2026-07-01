# agentcap — v0.2 spec: capture agent trajectory + environment at the source

> v0.2 supersedes v0.1 after a **second-pass Codex review**. v0.1's direction held (nothing
> structurally infeasible); v0.2 fixes the trust core, which v0.1 left underspecified.
> Two defaults are now locked:
> - **Fidelity = git-normalized blob content, not exact disk bytes.** We hash against git blobs.
>   ("Exact bytes" is unkeepable across CRLF / clean-smudge filters / LFS — it would make
>   `verify` prove a lie.)
> - **Gnarly repos are captured but marked ineligible, never silently mis-reconstructed.**
>   (LFS objects not anonymously fetchable, dirty submodules → `ineligible_for_publish` /
>   `ineligible_for_benchmark`, still kept as local fuel.)
> One capability is now explicitly **supported** (v0.1 wrongly deferred it): **local commits
> made during a session but not yet pushed** — see "Unpushed work."

## Problem / gap
Agent trajectory data today (proxy/relay logs, `dataclaw`) captures the model↔human
conversation + tool calls, but **not the environment** the agent operated on. Everyone
reconstructs env *after the fact* — lossy, the hard 90%. Env can only be captured **where the
agent runs**; a relay in the network middle never sees the agent's filesystem.

## Thesis
A zero-friction tool on the developer's machine that captures, per agent coding session in a
git repo: the **trajectory** + a **verifiable git-working-tree snapshot** at session start and
end. The paired unit `{initial tree, trajectory, final tree, delta}` is a real,
environment-grounded data unit — obtained by **capture, not reconstruction**.

### Fidelity contract (say exactly this)
- ✅ **Reconstructs git-normalized working-tree content** (tracked files' blob content at
  `base_sha` + captured diffs + selected untracked files), **verified against a per-file
  manifest of content hashes.** "Normalized" = as git records it after `.gitattributes` /
  CRLF / clean filters; not guaranteed byte-identical to the original disk.
- ❌ **Does NOT reproduce the runtime environment** (OS packages, compiler/interpreter versions,
  native libs, env vars, services, DBs, GPU/CUDA, network, credentials). Out of scope; we
  capture a **dependency declaration** (lockfile), not a runnable image.
- Every capture carries **confidence metadata**; only fully-`high` + verified captures are
  benchmark-eligible (exact rule below).

## The manifest (the trust core)
Every snapshot emits a **canonical per-file manifest**. `verify` reconstructs into a clean temp
dir and compares against it — **not** merely "does the patch apply." Per entry:
```
{ path, type (file|symlink|submodule), mode, exec_bit, symlink_target,
  content_hash (git blob hash of normalized content), size,
  status: present | skipped_size | quarantined_secret | ineligible }
```
`reconstruction_verified = true` **iff** every non-skipped entry's reconstructed hash matches.
This is what closes v0.1's sharpest risk (a reconstruction that's internally consistent but not
the original tree). No manifest ⇒ no verified ⇒ no publish, no benchmark.

## Env snapshot — schema (host-agnostic; NO Docker images)
```
base_sha            = git rev-parse HEAD
staged_diff         = git diff --cached --binary --full-index
unstaged_diff       = git diff        --binary --full-index
untracked[]         = files from `git ls-files --others --exclude-standard -z`
                      (NOT porcelain parsing); deterministic tar metadata;
                      reject path-traversal / abs symlinks; size-check BEFORE read
submodules[]        = { path, sha, dirty }   # dirty ⇒ ineligible (see below)
lfs                 = { detected, paths, objects_present, anon_fetchable }
git_meta            = branch, remotes (tokens stripped), core.autocrlf,
                      .gitattributes, .git/info/exclude
lockfiles[]         = uv.lock | poetry.lock | package-lock.json | requirements.txt | ...
manifest[]          = per-file entries (above)
fingerprint_pre / fingerprint_post   # index+worktree fingerprints bracketing capture
snapshot_inconsistent = (fingerprint_pre != fingerprint_post)
```
**Consistency:** fingerprint the index+worktree before and after capture. If they differ,
`snapshot_inconsistent = true` → the capture is **blocked from publish and benchmark** (kept as
low-confidence fuel only), never emitted as a torn-but-silent snapshot.
`deleted[]` is **derived metadata** from the diffs for readability — never a reconstruction op
(else it can contradict the diffs).

### Reconstruction order
`clone/bundle → checkout base_sha → apply staged_diff (index+worktree) → apply unstaged_diff
(worktree) → write untracked (with mode/symlink) → verify against manifest.`

## Two encodings
- **Reference format** (canonical for publishable work): the schema above, **no full-repo
  archive** (it still carries untracked tar + patches). Reconstruct by cloning the remote.
  Requires the base + every remote **required for reconstruction** (incl. submodules, LFS) to
  be **anonymously fetchable**. Any git host — GitHub / **GitLab** / Bitbucket / self-hosted.
- **Bundle format** (private / no reachable remote): **`git bundle` + working-tree overlay**
  (not `git archive` — bundle keeps the commit graph so `blame`/`merge-base`/`describe` work).
  Note bundle does **not** carry LFS objects, the index, local config, or dirty-submodule
  content — those ride in the overlay/manifest or trigger ineligibility. Cap full-history
  bundle size (see Storage). **Private / local-only, never published.**

## Unpushed work (local commits made during the session — SUPPORTED)
Committing mid-session is common; capture must handle it. The insight: uncommitted changes and
committed-but-unpushed commits are the **same problem** — "stuff above a fetchable base that the
remote doesn't have yet." So:
- **Capture** always succeeds, **offline, regardless of push state.** The bundle carries all
  local commits; you already have them. Capture never blocks on the network.
- **Push-state is resolved lazily at PUBLISH time, not frozen at capture** — because unpushed
  work is often pushed minutes later. At publish:
  ```
  base  = walk HEAD first-parent back to the newest commit that is anonymously fetchable
          from the remote (verified via `git ls-remote` + `merge-base --is-ancestor`,
          NOT stale remote-tracking refs)
  delta = local commits above base as a format-patch stack (preserves commit
          boundaries + messages) + worktree diff + untracked
  ```
  If the whole branch got pushed by publish time → `base = HEAD`, cleanest path.

## Confidence metadata (on every capture)
```json
{
  "start_confidence": "high | best-effort",
  "start_snapshot_after_first_event_ms": 1234,
  "end_confidence":   "high | best-effort",
  "join_confidence":  "high | medium | low",
  "join_signals":     ["session_id", "cwd", "time_overlap", "agent_log_path"],
  "snapshot_inconsistent": false,
  "reconstruction_verified": true,
  "ineligible_for_benchmark": false,
  "ineligible_for_publish":   false
}
```
**Benchmark-eligible iff:** `start_confidence == high ∧ end_confidence == high ∧
join_confidence == high ∧ snapshot_inconsistent == false ∧ reconstruction_verified == true ∧
ineligible_for_benchmark == false`. Everything else is fuel, not benchmark.

## Ineligibility rules (capture, don't mis-reconstruct)
Set `ineligible_*` (still captured as fuel) when:
- **LFS** objects aren't present locally AND aren't anonymously fetchable → publish-ineligible.
- **Dirty submodule** (uncommitted content in a submodule) → benchmark- & publish-ineligible
  unless recursively snapshotted (v0.2: not recursed → ineligible).
- **`snapshot_inconsistent`** → both-ineligible.
- Manifest verify fails → both-ineligible.

## Architecture — hybrid capture model
- **Watcher** (background daemon; launchd/systemd) — the **default collection mechanism, NOT
  the correctness boundary.** Watches agent session dirs (`~/.claude/projects/*`,
  `~/.codex/sessions/*`) via **versioned per-agent adapters** (these dirs are private
  implementation details of other tools) + **periodic reconciliation** (survives
  sleep/crash/missed FS events).
- **Optional high-fidelity hooks** — `agentcap mark-start` / `mark-end`, or shell/editor
  integration, pinning exact boundaries → boundary confidence `high`. **Association:** a mark
  binds to a session via `{capture_id, cwd, agent_pid, timestamp}`; if no session log yet, hold
  the mark and repair the join when the session file appears.
- **Snapshot / manifest / verify** — as above.
- **Store** — content-addressed blob store with dedup/compression/caps (Storage).
- **Join** — session id / cwd / time overlap → `join_confidence` + signals; manual repair.
- **Publish** — strict gate (below); reference format only.

## Publish safety gate (default-off; all must hold)
**"public repo ≠ safe to publish."** Public repo + local diff/untracked can still leak private
code or credentials. Passes only if:
- reference format only (never bundle/archive);
- **every remote required for reconstruction** — base, submodules, LFS — is anonymously
  fetchable (don't infer from host name); tokenized/private remote URLs stripped or rejected;
- **secret scan passes** (defense in depth — below);
- **`agentcap verify` reconstructs cleanly against the manifest** (all hashes match);
- `snapshot_inconsistent == false`; no `ineligible_for_publish`;
- untracked/new files (absent from the public repo) require **explicit per-file confirmation**;
- provenance + license preserved; final explicit user confirmation on a dry-run report.

### Secret scanning (HARD — reduces risk, never guarantees)
Secrets live in source, fixtures, logs, tool/terminal output, stack traces, notebooks,
lockfiles, commit messages, remote URLs, untracked files, **and the trajectory itself**. Layers:
denylist paths · entropy + pattern scanner · known-provider scanners · binary reject/quarantine ·
manual review mode · publish dry-run report · never publish bundle · publishing default-off.

## Storage (part of the trust core — built in step 1, not later)
Caps, blob hashes, skipped-file metadata **shape the snapshot itself**, so storage is not a
late add-on. Content-addressed blob store · gzip/zstd compression · hard max capture size ·
hard max per-file size (over cap → `status: skipped_size` in manifest) · default ignored-path
denylist (node_modules-like, build outputs, vendored deps) · cross-session dedup · retention ·
`agentcap gc`.

## Data unit produced
Per session: `{session_id, repo, agent, env_start, trajectory (steps + tool outputs), env_end,
delta, confidence_metadata, value}`. If the trajectory contains test runs → it carries **observed
validation evidence** (test logs are evidence, not self-verification). Distributed public-repo
publishing (dataclaw-style HF tag) → a diverse, env-grounded corpus (dissolving the
"single-source can't be a benchmark" problem).

## Trajectory value (supersedes the "verifiable task seed" framing)
The original plan graded sessions by mining `FAILED → PASSED` as a verifiable-task signal. That
framing is wrong for agent sessions and is **downgraded to a neutral sub-signal**:
- **Agent-authored tests are normal, not a defect.** TDD (write test → red → implement → green)
  is the default agent rhythm, so `FAILED → PASSED` is a *recall* signal, not a *quality* one,
  and the self-authored test is not an independent oracle.
- Instead we score each session's **training value** on three deterministic axes (no model at
  runtime), computed by `agentcap value` → `value.json`:
  - **A. groundedness** — is the success trustworthy/reproducible? A self-authored test that ends
    green is a **reproducibility anchor** (grounded), not an independent judge.
    `grounded | weakly_grounded | ungrounded`.
  - **B. process richness** — difficulty + failure→recovery density, over **code files only**
    (docs/artifacts excluded so volume can't fake it). `rich | moderate | thin`.
  - **C. focus/coherence** — one coherent problem vs sprawl. `focused | diffuse | sprawling`.
  - **`high` = grounded + rich + focused**; diffuse caps at `medium`; a mega multi-task session
    → `low`. All raw signals exposed; the tier is a transparent proxy, not a benchmark grade.
- Candidate `FAIL_TO_PASS` is still emitted (by `agentcap seed`) as a neutral sub-signal a
  sandbox-builder can use — **agentcap grades and hands off raw material; it does not build
  runnable sandboxes or manufacture independent verifiers.**

## v0.2 scope
- Git projects only.
- Reference + bundle encodings; per-file manifest + `agentcap verify`.
- Lockfile capture (dependency declaration).
- Unpushed local commits: supported (capture offline; resolve push-state at publish).
- Watcher (versioned adapters) for Claude Code + Codex + reconciliation; `mark-start/end` hook.
- Confidence + ineligibility metadata on every capture.
- Content-addressed storage with dedup/caps/gc.
- Manual `agentcap publish` behind the full safety gate.

## Non-goals (v0.2)
- Runtime/environment reproduction (only normalized tree content + lockfile declaration).
- Byte-exact reconstruction across CRLF/filters/LFS.
- Non-git environments (databases, services, remote state).
- Real-time/streaming capture (start/end + confidence suffice).
- Guaranteeing zero leakage or exact session-boundary detection (both best-effort + confident).
- Recursive dirty-submodule snapshotting; anonymous-unfetchable LFS (→ ineligible, not blocked).

## Positioning (one line)
> **A provenance-rich collector of coding-agent sessions — trajectory + verifiable git
> environment, captured at the source — that scores each session's training value
> (grounded × rich × focused).** Not a sandbox builder, not a benchmark-task factory, not
> "full environment capture" or "guaranteed reproducibility." See `../agentcap/README.md`.

## Build order (storage is in step 1)
1. **Snapshot engine + manifest schema + minimal CAS/storage/caps** (the trust core).
2. **Reconstruction + `verify` against the manifest** (hash-compare in clean temp dir).
3. Manual `mark-start` / `mark-end`.
4. Watcher + one adapter (Claude Code) + reconciliation.
5. Join + confidence against a dataclaw export.
6. Publish dry-run gate + secret scan.
7. HF push (last — most safety-sensitive).
