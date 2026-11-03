# Crucible

Build a small, complete local research controller. Python 3.11+ standard library; OpenCode first, Claude Code supported. Preserve raw evidence and fail clearly on partial results. No model agreement is scientific proof.

Use isolated temporary projects for tests and smoke runs. Treat active research projects supplied as references as read-only: do not edit files, change Git state, run experiments, or install configuration there. Do not restart unrelated user services while implementing.

Run `python3 -m unittest discover -s tests -v` and the documented local example. Real-provider checks must be small, bounded, and labeled separately from mocked transport tests. One writer per working tree; delegated changes use isolated copies and parent integration. Children do not delegate.

Keep each attempt, exit status, inputs, parameters, evaluator result, model request/response, decision, and recovery action recoverable. A waiting timeout is not experiment failure. Never silently substitute model answers or metrics.
