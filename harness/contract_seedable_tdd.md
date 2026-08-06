# Task contract — reproduce-then-fix (capture-ready TDD)

You are fixing one GitHub issue. This session is **captured and later re-executed**
by a replay harness, so the *shape* of your work matters as much as the fix.
Work only inside the current worktree.

## Issue
<<< PASTE osmind pack / issue body here >>>

## Hard requirements — do not skip

1. **Reproduce first (RED).** Before touching any implementation code, add or extend
   a test that *fails on the current code because of this exact bug*. Run it and
   paste the failing output. If it doesn't fail, you haven't reproduced the bug yet.

2. **Then fix (GREEN).** Change implementation until that test passes. Do **not**
   edit the test's assertions to force green.

3. **Tracked paths only.** The new test and every fixture/data file it reads MUST
   live under version-controlled paths (`tests/`, `tests/data/`, …). Never write
   them into gitignored dirs (`output/`, `build/`, `dist/`, `.venv`, `__pycache__`).
   When unsure run `git check-ignore <path>` — it must print nothing.

4. **Self-contained.** The test must pass from a clean checkout using only the
   repo's declared deps — no network, no files outside the repo, no machine-local
   state.

## Do NOT

- **Do not rename or delete existing test functions/nodes** mid-session — a stale
  node id makes the whole pytest run abort at replay time (`no tests ran`).
- **Do not hardcode the expected value or special-case the test input.** The fix
  must generalize; a reviewer would reject `if input == X: return Y`. Green earned
  by short-circuiting the checker does not count.
- **Do not widen scope.** Fix only this issue — no drive-by refactors, no unrelated
  reformatting.
- **Do not weaken, skip, or xfail existing tests** to reach green.
- **Do not `git commit`.** Leave your work in the working tree. The capture boundary
  is drawn from outside this session by two snapshots; committing makes the worktree
  clean against your own new HEAD, which empties the recorded diffs and hides which
  tests you wrote. Staging (`git add`) is fine.

## If you cannot reproduce it

If, after a genuine attempt, you cannot write a test that fails *because of this
bug* (not reproducible, needs hardware/network, or the issue is underspecified),
**STOP and make no code changes.** Write one paragraph saying why. A clean
"not reproducible" is a correct outcome — never invent a passing test to look
successful.

## Definition of done

- The new test is **red at the start commit, green now**.
- The **full existing test suite still passes**.
- `git status` shows only intended changes, all in **tracked** files.
- Close with a short summary: root cause, the test you added (`path::node`), and
  the fix.
