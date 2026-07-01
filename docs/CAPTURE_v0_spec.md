# agentcap — v0 spec: capture agent trajectory + environment at the source

## Problem / gap
Agent trajectory data today (from LLM proxy/relay logs, or tools like `dataclaw`) captures
the model↔human conversation + tool calls, but **not the environment** (filesystem/git state)
the agent operated on. Everyone reconstructs the env *after the fact* (from GitHub PRs, from
proxy logs, from asciinema recordings) — which is lossy and is the hard 90%. Crucially, the
env can only be captured **where the agent runs** (the developer's machine); a relay/proxy in
the network middle can never see the agent's filesystem.

## Thesis
A lightweight, zero-friction **background** tool on the developer's machine that captures, per
agent coding session in a git repo: the **trajectory** + a **reproducible snapshot of the git
environment** at session start and end. The paired unit `{initial env, trajectory, final env,
delta}` is a real, verifiable, environment-grounded data unit — obtained by **capture, not
reconstruction**.

## Architecture
- **Watcher** (background daemon; launchd/systemd) — NOT a CLI shim (shim requires the user to
  launch through it = not zero-friction). Watches agent session dirs (`~/.claude/projects/*`,
  `~/.codex/sessions/*`). On a new session whose `cwd` is a git repo → snapshot git state
  (start). On session idle (N min) or completion → snapshot git state (end).
- **Snapshot** — capture the git env in one of two formats (below).
- **Store** — `~/.agentcap/captures/<session_id>/` : `meta.json`, `env_start.json`, `env_end.json`.
- **Join** — correlate captures with the trajectory (from a `dataclaw` export or raw session
  logs) by `cwd` + time window / session id.
- **Publish** (opt-in, public repos only) — push to HuggingFace with a shared tag, forming a
  distributed dataset (dataclaw-style).

## Env snapshot — two formats (host-agnostic; NO Docker images)
The reproducible env for a git project = `{exact file tree} + {deps declaration}`, never a
Docker image. Two encodings:
- **Reference format** (public repos): `{remote_url, base_sha, diff=git diff HEAD, untracked[],
  lockfile}`. Tiny. Reconstruct: `clone remote → checkout base_sha → git apply diff → write
  untracked`. Works for GitHub / **GitLab** / Bitbucket / self-hosted — any git URL.
- **Archive format** (private / no reachable remote): `git archive base_sha` tarball + diff +
  untracked + lockfile. Self-contained. For local/private use.
- Deps: capture the lockfile (`uv.lock`/`poetry.lock`/`package-lock.json`/`requirements.txt`)
  as a declaration; full runtime reproduction (standard base image + install-from-lockfile) is
  the consumer's step / a later tier. **The tool never snapshots or ships an image.**

## Public / private split (resolves "publish vs proprietary code")
- Detect whether the repo is public (remote host + an unauthenticated fetch check, or user config).
- **Public** → reference format → publishable to HF (no code leak; the repo is already public).
- **Private** → archive format → **local only** (the dev's private RL/SFT fuel); never published.
- Desensitization at capture: skip `.env`/secret-like files; secret-scan before any publish.

## Data unit produced
Per session: `{session_id, repo, agent, env_start (reproducible), trajectory (steps + tool
outputs), env_end (reproducible), delta = start→end diff}`. If the trajectory contains test
runs → a self-verifying task. Distributed public-repo publishing → a diverse, env-grounded
corpus (which also dissolves the "single-source can't be a benchmark" problem).

## v0 scope
- Git projects only.
- Reference + archive formats for CODE reproducibility; lockfile capture for deps.
- Watcher for Claude Code + Codex session dirs.
- Local storage + a manual `agentcap publish` (public repos only).

## Non-goals (v0)
- Full Docker/runtime reproduction (only code tree + lockfile declaration).
- Non-git environments (databases, services, remote state).
- Real-time/streaming capture (start/end snapshots suffice).

## Open feasibility questions
1. Session-boundary detection by watching session-log dirs — is "new file → start / idle → end"
   reliable across Claude Code and Codex? Are there better signals (e.g. explicit session-end
   markers, file locks)?
2. Reference-format fidelity — does `clone + checkout + apply diff + write untracked` reliably
   reproduce the *exact* working tree? Edge cases: submodules, symlinks, file modes, CRLF,
   large/binary untracked files, `.gitignore`d build artifacts the agent depended on.
3. Unpushed base commits — nearest-pushed-ancestor + spanning-diff vs `git bundle`: which is
   more robust for the reference format?
4. The watcher's "start" snapshot lags (session file appears after the first action) → initial
   state may miss the agent's earliest edits. `base_sha` (HEAD) is stable, so the base is fine,
   but the initial diff may be incomplete. Acceptable? Better triggers?
5. Public/private detection — reliable enough to never accidentally publish private code? Failure modes?
6. Storage growth at scale (many sessions × diffs/archives) — concerns?
7. Anything structurally infeasible, or a materially better overall approach?
