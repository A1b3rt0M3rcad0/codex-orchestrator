# Repository development contract

This repository builds a local Codex orchestrator, not an application domain.
Python 3.11+, standard library only at runtime. Test with:
`python -m unittest discover -s tests -v`.

Keep scheduling deterministic. Models do implementation, review and meaningful
planning, never just forward messages. Do not weaken snapshots, scope checks,
independent review, attempt budgets or approval gates to make a test pass.
Never silently switch model IDs, use `--yolo`, bypass hook trust, or push/merge
consumer code. No implementation may approve itself.

Use the official released Codex documentation as the integration contract.
Changing model IDs or CLI flags requires source verification and tests.
Fake-CLI integration tests are not real-model benchmarks or live smoke tests.
Keep the README and workflow reference consistent with executable behavior.
