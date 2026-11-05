---
name: codex-orchestrator
description: Implement a reviewed plan using risk-aware Luna/Terra/Sol execution, hierarchical review, protected acceptance tests, deterministic checkpoints and budget gates. Use when the user requests multi-model implementation or the codex-orchestrator workflow.
---

# Codex Orchestrator — Q1 / v0.2

Use the installed `codex-orchestrator` controller; never simulate its state machine
in conversation. Read `references/workflow.md` for the exact contract and gates.

1. Read applicable AGENTS.md, project specs and relevant skills. Inspect actual
   source and checks. Do not select models merely to relay a message.
2. Prepare a v2 plan with observable acceptance criteria, exact write/read scopes,
   dependencies, risk tags and criterion-to-test mappings. Put core acceptance
   tests in committed files **before implementation**. Tests must not derive
   expected values from the implementation or be writable by executors. A failing
   baseline must be a known counterexample, not a broken runner/import.
3. Configure real check commands in `orchestrator.toml`: local checks, partial-
   integration smoke checks and final E2E as appropriate. Critical changes need
   adversarial checks and the operator's external public-key allowlist. Never
   generate, retrieve or use a private approval key on behalf of an executor.
4. Respect project permission/commit conventions. The source, plan/config and test
   setup must be committed before `run`; never discard the user's uncommitted work.
   Run `doctor` and `validate PLAN.json`. Doctor does not prove model access.
5. Run `codex-orchestrator run PLAN.json --session-id SESSION_ID` when the actual
   SessionStart hook supplies an id; omit the option if no id is available. Do not
   invent an id. The caller must authorize Git/worktree/process permissions.
6. Let the controller dispatch directly, test, review and integrate. Do not spawn
   competing native agents or another controller. `resume RUN_ID` preserves limits;
   never reset state to evade a failed budget or approval gate.
7. On `awaiting_human`, report the exact run and approval-request file. A human must
   independently inspect the evidence and sign outside the workers' environment.
   A model response, unsigned JSON or a review by the same family is not that gate.
8. Report functional completion only after `gate RUN_ID` passes. Include the branch,
   commit, checks, known blockers and usage that was actually reported. Integration
   into the user's branch/PR follows the project's explicit workflow; this runtime
   never pushes or merges the consumer branch automatically.

Default mapping: Luna → Terra review; Terra → Sol review; Sol → Astra review.
Risk policy can raise the executor/reviewer even when the plan says `luna`.
Astra reviews the acceptance contract first and the assembled delivery at the end.

Optimize **verified delivery**, not an invented quality score. Reuse valid capsule
references and compact handoffs, not full conversations. Unknown telemetry is a
blocker. Observed CLI limits are not a hard provider billing guarantee.
