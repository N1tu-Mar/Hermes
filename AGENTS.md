# Agent collaboration rules

These rules apply to every coding agent working in this repository. The goal is
parallel delivery without shared-file edits, lost work, or unsafe integration.

## Before editing

1. Work from a named task packet containing an outcome, owned paths, read-only
   interfaces, acceptance checks, and a base commit.
2. Use a dedicated branch and, when agents run concurrently, a dedicated Git
   worktree. Never run two write-enabled agents in the same worktree.
3. Run `git status --short` before editing. Stop if an owned path already has
   uncommitted changes. Do not clean up changes belonging to another agent.
4. Read only this file, the task packet, and the directly relevant symbols and
   tests. Use `rg` and bounded file reads instead of loading the whole repository.

## Exclusive ownership

An active task must have exclusive write ownership of every path it changes.
All paths not named in its task packet are read-only.

The integration owner alone edits these high-contention surfaces:

- `app/contracts.py`
- `app/campaigns.py`
- `app/api.py`
- dependency and lock files
- root JSON templates and schema versions
- shared fixtures, repository configuration, and `README.md`

Prefer these disjoint feature areas for parallel tasks:

- persistence: `app/storage.py`, `app/cache.py`, dedicated persistence tests
- jobs: `app/jobs.py`, dedicated coordinator tests
- research: `app/research.py`, `app/openai_client.py`, dedicated research tests
- drafting: `app/outlines.py`, `app/writer.py`, dedicated drafting tests
- Gmail/MCP: `app/gmail.py`, `app/mcp_server.py`, dedicated adapter tests
- frontend: `app/templates/**`, `app/static/**`, dedicated UI tests

`tests/test_core.py` is a shared legacy test file and is integration-owned. New
tasks add uniquely named test modules instead of appending to it.

If a task needs a shared contract change, do not edit the contract. Create a
uniquely named request at `docs/change-requests/<task-id>.md` or report the exact
requested signature in the handoff. The integrator applies accepted changes.

## Hard three-minute commit rule

Every write-enabled agent must create a commit no later than three minutes after
its previous commit while it is actively changing the repository.

- If a coherent unit and its focused tests are complete, make a normal commit:
  `<TASK-ID> <type>: <imperative summary>`.
- If work is incomplete or tests are not passing, make an isolated-branch
  checkpoint commit: `checkpoint(<TASK-ID>): <specific current state>`.
- Stage only task-owned paths. Inspect `git diff --cached --stat` before every
  commit. Never stage `.env`, credentials, campaign data, caches, or unrelated
  changes.
- Checkpoint commits never go directly to `main`. The integrator reviews and
  squash-merges or otherwise curates a completed, passing task branch.
- Read-only review agents are exempt because they do not change the repository.
- Do not use empty commits merely to satisfy the clock. A checkpoint must capture
  actual task-owned progress.

The three-minute rule is a recovery cadence, not permission to bypass testing or
ownership boundaries. The final task state must be committed, focused tests must
pass, and the owned worktree must be clean before handoff.

## Implementation and verification

- Make the smallest change that satisfies the task; do not refactor neighboring
  modules without explicit ownership.
- Keep domain logic independent of FastAPI and MCP. External providers and time
  remain injectable so tests stay offline and deterministic.
- Preserve campaign isolation, atomic JSON replacement, bounded queues, sourced
  evidence, approval invalidation, and Gmail idempotency.
- Test the narrow behavior first. The integrator runs the full suite after each
  integration.
- Agents may commit on their own branch. They must not merge, rebase, push,
  amend another agent's commits, or modify another task's branch.

## Handoff format

Return only:

1. commit hashes and one-line purposes;
2. tests run and their results;
3. changed paths;
4. assumptions;
5. blockers or requested contract changes.

Do not paste full files or repeat the product specification.
