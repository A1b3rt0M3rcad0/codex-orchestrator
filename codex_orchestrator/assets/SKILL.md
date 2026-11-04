---
name: codex-orchestrator
description: Use when the user explicitly requests hierarchical Codex orchestration, cost-aware implementation with Luna/Terra/Sol and Astra validation, or a managed multi-task implementation/review workflow. Do not use for ordinary questions, isolated reviews, or inside an already managed worker.
---

# Codex Orchestrator

Astra plans; a deterministic local controller dispatches implementation and review.
Routes: Luna -> Terra review; Terra -> Sol review; Sol -> Astra review. Astra
validates the integrated result. Models are configurable; these are role names.

## Start

1. Read the project's AGENTS.md and applicable skills. Inspect relevant source,
   tests, existing changes and the user's acceptance criteria. Do not load the
   whole repository or conversation into every task.
2. If `CO_MANAGED=1`, do NOT invoke this skill/controller recursively. Complete
   only the contract passed by the running controller.
3. Run `codex-orchestrator doctor`. It does not verify authentication/model access.
   Missing CLI, checks or model access is a blocker, not permission to invent
   outcomes or silently switch models.
4. Prepare a strict JSON plan using the reference below. Select the lowest tier
   appropriate to each task; use explicit paths, dependencies and observable
   acceptance criteria. Checks must name real commands in `orchestrator.toml`.
5. Run `codex-orchestrator validate path/to/plan.json`. The starting checkout must
   be committed. Never commit unrelated user changes to make this prerequisite
   pass. Follow the user's authorization and repository commit conventions.
6. Execute `codex-orchestrator run path/to/plan.json --session-id SESSION_ID`, using
   the id supplied by the SessionStart hook. Omit the flag only if no session id
   is available; the controller still enforces its gates without hook enrollment.
7. Let that command manage all local loops. Do not concurrently edit its worktree,
   spawn duplicate workers, or launch another controller for the same repository.
8. Read `codex-orchestrator status RUN_ID`, then `codex-orchestrator gate RUN_ID`.
   Only a successful gate permits claiming an approved delivery. Report the
   branch, evidence and real token usage; missing telemetry is unknown, not zero.
9. Leave the original checkout unchanged. Ask/obtain normal authorization before
   merging or pushing the resulting branch. The controller does neither.

## Recovery

Use `list` to discover a run after interruption. Use `status` before `resume RUN_ID`.
Resume consumes the original budgets and preserves earlier approvals; it is not a
free retry/reset. A changed worktree, new scope, or exhausted budget requires
inspection and a new plan, not deletion/forging of state or weakened checks.

A blocked run may be reported as blocked. Never keep consuming models merely to
satisfy a Stop hook. Hooks do not create reviews or prove semantic correctness.

Read [the workflow reference](references/workflow.md) only when preparing a plan,
reasoning about delegation, or investigating an execution failure.
