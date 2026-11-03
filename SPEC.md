# Crucible protocol v1

The engine runs trusted, registered local software programs. It does not execute model-written shell or sandbox experiment code. State lives in the canonical project's `.crucible/`.

## Configuration

`init` creates usable synthetic example code and `.crucible/config.json`. Required fields:

- `version: 1`, `goal`, `initial` decision.
- `experiments`: name to command argv, typed `parameters`, required `artifacts`, declared `inputs`, optional `timeout_seconds`.
- `evaluator`: command argv, declared inputs, timeout (default 30s).
- `models`: name/backend/model, optional command argv prefix/effort. Backends: opencode, claude. Explicit distinct model IDs; effort variants alone are not different models. May be empty for deterministic workflows.
- `planner`: configured model name; defaults to first model.
- `limits`: max_runs (8), max_calls (12), call_timeout_seconds (120), debate_rounds (1).

All attempts/calls, including failures, count against budgets. These are operational defaults, not scientific thresholds or guaranteed money/token caps. Experiments have a deadline only if explicitly configured.

Commands expand only `{python}`, `{project}`, `{attempt}` and declared parameter names. Parameters support integer, finite number, string, boolean, optional enum/min/max. No shell, attribute access, format conversion or undeclared parameter. Inputs and artifacts must stay inside project/attempt respectively.

Each attempt saves normalized configuration, input snapshots/hashes, parameters, argv, available Git HEAD/declared-input diff, runtime information, raw artifacts and runner receipts. Declare all relevant support modules and data manifests: undeclared dependencies and complete environment cloning are outside this provenance contract.

## Evaluator

One JSON object on stdin: `attempt` contains id/project/path/proposal/command/artifacts/returncode/provenance; artifacts map names to absolute paths. `history` contains previous id/proposal/evaluation summaries. Cwd is the attempt directory.

One JSON object on stdout, for example:

```json
{"event":"normal","summary":"Registered objective improved","metrics":{"loss":4.0003},"comparison_key":"synthetic-quadratic-v1-seed-7","next":{"action":"run","experiment":"objective","parameters":{"x":2,"seed":7},"reason":"Next registered point"}}
```

Events: normal, anomaly, regression, stagnation, theory_conflict, insufficient, error. Summary/comparison_key must be nonempty; metrics must be finite numbers. The controller checks format and input/artifact identity, not the scientific correctness of a project's evaluation policy.

Normal with next skips models; normal without next asks planner. Insufficient/error block unless evaluator supplied a concrete validated recovery decision. Other events require independent reviews. Every failure and original result remains retained.

## Reviews and decisions

First-round reviewers receive identical evidence and no other review or parent's current conclusion. Packet includes current/prior evaluations, provenance, allowed experiment parameters and UTF-8 artifact text up to 65536 bytes per file. Larger/binary files have hashes/sizes and explicit omission notices; omitted content is unavailable to reviewers. Packet-only workspace denies model tools/MCP and avoids project instruction discovery. This is not an OS security boundary.

Review JSON:

```json
{"supported_findings":["A same-condition repeat exists"],"concerns":["The optimum is unresolved"],"evidence_refs":["evaluation:2"],"needs_rebuttal":false,"next_test":{"hypothesis":"A changed x can improve the objective","predictions":{"changed_point":"Lower loss if a better point exists"}}}
```

References must exist in packet. Requested rebuttals are bounded by configured rounds. Failed required answers block synthesis. Planner receives all completed opinions and returns action run/stop/hold plus nonempty reason. Run also requires registered experiment and exact bounded parameters; after investigation it requires hypothesis and nonempty predictions. No vote counting is used.

Raw prompt/stdout/stderr, requests/responses, session metadata and parsed answers are retained. Nonzero exit, error events/fields, abnormal/missing terminal output, denied tools and malformed JSON fail. Requested/observed models remain separate; unavailable server identity is not guessed. An operator can provide the same validated decision via `decide --file` when no active attempt exists, retaining previous failures and an audit event.

## Explicit manual debates

`debate --question TEXT` snapshots supplied project-relative UTF-8 files or the latest completed attempt when no files are specified. A question-only case is explicitly labeled as lacking empirical evidence. Explicit file evidence is limited to 65536 bytes per file; missing, non-UTF8, oversized or escaping paths fail. Implicit latest-run artifacts retain their complete hash and size while binary/oversized content is explicitly omitted; recorded hash mismatches still fail. Cases live under `.crucible/debates/ID/`, with captured configuration/evidence, a snapshot digest, raw calls, `report.json` and `REPORT.md`.

Manual cases use the same tool-disabled providers and review schema. Each configured model reviews independently, optional requested rebuttals are bounded, and planner returns a conclusion, agreements, disagreements, next_checks and actual evidence_refs. Default manual rebuttal rounds are zero; the separate default call cap is models*(rounds+1)+1 and failures count. Resume reuses captured inputs and successful calls; explicit retry is limited to known failures, never unresolved/live calls.

A manual case does not launch experiments, acquire the experiment worker lock, consume its call budget or change its next decision. It appends a deduplicated completion event only. Model suggestions are not automatically executed. OpenCode exposes action=debate and `/crucible-debate`; the shared skill supports `/crucible debate` in Claude Code.

## Persistence and recovery

SQLite journals attempts, calls, decisions, control and events. Attempt completion, next decision and decision event commit transactionally. REPORT.md is a human view, not control state or a replacement for existing research records.

One worker lock owns a project. Detached experiment/evaluator runners atomically write matching-token request/start/child/result receipts. Worker death does not erase a completed receipt. Input/configuration changes during active work fail. Artifact hashes bind the evidence used for evaluation/decisions.

Pause/stop prevent subsequent stages, leaving active jobs alive. Cancel explicitly requests job-child process group termination and preserves a cancellation receipt. During synchronous model calls, control changes apply at the next boundary; the call retains its own timeout. Waiting timeout alone does not cancel an experiment.

Recovery uses receipts and process identity. Unknown launches/in-flight provider outcomes remain reconciliation_required; never silently rerun them. Explicit retry applies to failed stages and reuses successful evidence. Rerunning a failed experiment creates a new budgeted attempt. Exactly-once external effects are not promised.

Provider requests have detached timeout supervision, matching-token/intent/artifact-hash terminal receipts and explicit recovery. A known live actor is awaited; an unknown actor or partial/mismatching receipt is not retry-authorized. Configuration is reloaded at boundaries and declared input/artifact identity is checked before subsequent external work and decision commit. Detection of a live or unresolved execution preserves ownership and prevents retries.

Events form a durable outbox. Delivery is explicit; ack follows successful session admission. A delivery/ack crash may cause the same event ID to be replayed. Admission is not proof that an agent consumed or scientifically accepted evidence.

## Hosts

OpenCode V2 bridge uses setup(ctx) with typed native tools, fixed project scope and argv. No arbitrary shell/project path from model input. The worker progresses without a live TUI. No periodic LLM polling starts.

The ai-prompts shared skill locates the separate engine through CRUCIBLE_ROOT, an installed CLI, a recorded installation connection or HOME discovery. Research state is not copied into the kit. Linux is integration-tested. macOS ps-based process identity has mocked tests only; Windows is outside the fcntl/process-group execution path.
