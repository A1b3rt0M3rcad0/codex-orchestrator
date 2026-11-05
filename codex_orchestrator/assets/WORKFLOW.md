# Managed workflow v2

## Required plan

```json
{
  "version": 2,
  "objective": "Implement the specified behavior",
  "acceptance": ["The assembled feature meets the specification"],
  "integration_checks": ["e2e"],
  "checkpoint_checks": ["smoke"],
  "tasks": [{
    "id": "T1", "tier": "luna", "objective": "Implement a bounded behavior",
    "scope": ["src/feature.py"], "reads": ["src/contract.py"],
    "acceptance": ["Invalid input is rejected without side effects"],
    "checks": ["acceptance"], "depends_on": [], "risk_tags": [],
    "test_contract": {
      "files": ["tests/acceptance/test_feature.py"],
      "cases": [{"criterion": "Invalid input is rejected without side effects",
                 "check": "acceptance", "baseline": "fail",
                 "rationale": "Known counterexample on the baseline commit"}]
    },
    "adversarial_checks": []
  }]
}
```

All fields are required; unknown fields are rejected. `tier` is `luna`, `terra`
or `sol`; Astra is not an automatic implementer. `global`, `contract`, `repair-*`
are controller-reserved ids. Dependencies must exist and form an acyclic graph.
Scope/read paths are exact files or directory prefixes ending `/`; no globs,
absolute paths, parent traversal or symlink escapes. Core acceptance files must
be committed regular files outside every task's write scope.

## Optional per-task project skills

The external Astra planner/spec author first runs `codex-orchestrator skills`
to discover Git-tracked `.agents/skills/<name>/SKILL.md` entries. Add optional
`"skills": {"required": ["tdd"], "recommended": ["code-review"]}` to
individual tasks. If present, both arrays are required, unique, and refer to
existing catalog names. Controller skill cannot be bound to workers.

A pinned catalog includes names, descriptions, paths, SHA-256 fingerprints.
The implementation stage receives complete required SKILL.md text (40 KiB
total maximum); recommended skills are only references. Reviewers get
references, not redundant prompt text. Scripts/references are not automatically
loaded. Child workers inherit required bindings; they cannot expand the
planner-approved set. Final gate validates the pinned source and per-stage
request artifacts. This proves instructions were delivered, not that a remote
model followed them.

Personal/global/plugin skills are not enforced by this project-only mechanism.
The deterministic runtime does not automatically plan from arbitrary prose.

## Eight executable controls

1. **Risk floor.** Lexical risk hints set a Terra floor, while material
   sensitive code changes, declared critical tags, opaque changes and project
   globs require Sol/Astra and critical verification. The decision is
   monotonic through delegation and repair.
   Unknown/binary/large scoped content is treated conservatively. Regex/policy is
   not a proof of semantic risk detection; add project-specific critical paths.
2. **Independent oracle.** Acceptance criteria map to checks and baseline outcomes.
   The controller hashes core tests, explicit check scripts, configuration and
   instructions, runs the baseline and asks Astra to review the test contract in a
   fresh read-only invocation. Checks run as real processes, not model assertions.
   Every later phase verifies sealed files. Executors may add supplemental tests,
   but cannot modify or replace the preapproved core oracle.
3. **Checkpoints.** Run configured partial-integration checks every N integrated
   tasks, after each critical task and at the end of a partial batch. Failures stop
   further dependents. Astra diagnoses only a failure; targeted repairs are finite
   and inherit the original task's attempt and token accounting.
4. **Worktrees.** Integration and each task have separate worktrees. Ready tasks
   may run concurrently only when declared write/read scopes do not overlap.
   Critical tasks run alone. Results integrate in stable plan order. A changed
   base creates another candidate, applies the patch, reruns checks and review;
   conflicting patches block with both worktrees preserved. No force reset, push,
   user-branch merge or automatic conflict resolution. Persisted integration intents
   recover completed fast-forwards without replaying them.
5. **Handoff.** Store bounded factual attempts/outcomes, unresolved issues and patch/
   log references. No private model reasoning or transcript propagation. A stronger
   executor inspects the preserved patch. Full local artifacts remain available.
6. **Budgets.** Atomic per-call reservations cover run, task/subtree and model token
   ceilings; monetary ceilings require user-configured prices for all roles.
   Attempts/calls/tasks/repair rounds/timeouts/log/prompt sizes are finite. Children
   and repairs charge their ancestors. Missing/invalid telemetry retains reserved
   cost and blocks further inference. Resume does not reset limits.
7. **Correlated review.** Review contexts are blind to implementation narratives.
   Critical changes additionally require a separate adversarial invocation, real
   negative checks and an external human's Ed25519 approval over exact evidence.
   Context isolation does NOT imply statistical independence. Private signing keys
   must remain unavailable to workers; local state is not a hostile multi-user
   security boundary.
8. **Review capsules.** Bind spec, risk, oracle, source/read hashes, base/tree/commit,
   model, check logs and actual review responses. Reuse only fresh evidence. Changed
   bases trigger new review; subsequent edits trigger stale-capsule refresh. Global
   Astra receives compact references and integration evidence. No guarantee is made
   about a model never rereading a diff internally.

## States and loops

Run: `pending → running → approved | awaiting_human | blocked | interrupted`.
Task: `pending → implementing → testing → reviewing → local_approved → integrated`.
Delegation: `implementing → delegated → child flows → pending/integration`.
A check/review rejection returns to the same executor; exhaustion promotes to the
next tier and its reviewer. Sol exhaustion stops for replanning. No author approves
its own implementation. Read-only reviewers do not fix the code.

A parent may delegate once, before edits, to lower-tier children inside the reviewed
scope. Child criteria/checks are subsets of the immutable parent contract. Children
execute sequentially inside a parent flow; independent top-level tasks may run in
parallel. Tasks launched in one batch must not need another batch member's output.

## Observed consumption versus a hard invoice cap

`codex exec` emits usage after a turn. The optional trusted `PostModelRequest` hook
can give earlier request telemetry but cannot veto model requests. The controller
polls usage and cancels the local process on a ceiling; a request already sent can
still be billed and reported late. Parallel in-flight work can also overshoot its
reservation. `mode = "observed"` is admission control + a circuit breaker, **not a
hard monetary cap**. `mode = "strict"` refuses inference because the CLI adapter
cannot establish a hard provider-enforced cap. This is deliberate fail-closed
behavior, not a silent approximation. Request and turn telemetry are not added
together; cached input and reasoning output are not double-counted.

## Hooks and recovery

SessionStart supplies an enrollment id. Stop gates only that session, reports
blocked/awaiting-human states truthfully and prevents unbounded continuation.
Managed processes skip lifecycle Stop gates; controller transitions remain required
without hooks. PostModelRequest writes only compact usage, never transcripts and
never calls another model. Do not use SubagentStop for these independent CLI
processes. Review/trust hooks explicitly in Codex; installation cannot grant trust.

State/artifacts live in the Git common directory under `codex-orchestrator/runs`;
worktrees live outside the checkout/Git metadata. The source checkout must be clean.
`resume` checks plan/worktree integrity and preserves budgets. An unresolved usage
record requires operator investigation; there is no blind budget-reset override.

For critical work, `approval-request RUN_ID` produces canonical bytes. An external
reviewer signs with an Ed25519 key using OpenSSL 3+. `approve` verifies against a
public-key allowlist pinned when the run started. Wrong signer, wrong run, altered
code/config/evidence and replay of stale statements are rejected. The controller
never generates or consumes private keys.


## Q2: Mutation sensitivity, real-model smoke and comparative reports

`test_contract` optionally contains `mutations`: the recommended
`file_replacement` (file/content/check/failure_marker) or legacy exact
find/replace mutation. Each probe stays within implementation scope, uses
an approved check and must trigger the expected regression assertion. Surviving mutants, wrong failure
reasons and missing/stale probe evidence are NOT approvals. A mutation probe
cannot edit the accepted candidate, test oracle or Git state. The default
`verification.mutation_policy = critical` requires declared mutants for
critical work; `all` requires them for every task, `optional` explicitly relaxes.

`smoke-models --acknowledge-paid-requests` calls each configured model in a
read-only isolated CLI request, checking JSON schema and usage; it does not
validate code or hooks. `benchmark RUN_IDS...` compares only identical plans
and base commits. It reports observed outcomes, uncertainty and usage, never
a fictitious quality score or a remote billing guarantee. See docs/Q2-VERIFICATION.md.
