# Spec: cost-control

Capability: `cost-control` · Status: canonical

---

### Requirement: estimate_cost runs before any translation spend and covers only PENDING chunks

`TranslatorEngine.estimate_cost(job_id)` SHALL compute the expected cost for all chunks with `status != DONE` (i.e. only work not yet paid for). It SHALL use `TranslationProvider.count_tokens` to count prompt + expected output tokens per chunk and `TranslationProvider.price(model)` to compute USD cost. It SHALL return a `CostEstimate` with `input_tokens`, `output_tokens`, `usd`, `model`, and `within_budget`.

No provider translation call SHALL be made during `estimate_cost`.

#### Scenario: estimate covers only PENDING chunks

Given a 5-chunk job where chunks 0–1 are DONE and chunks 2–4 are PENDING,

When `engine.estimate_cost(job_id)` is called,

Then the returned `CostEstimate.input_tokens` SHALL reflect token counts for chunks 2–4 only (not 0–1), and the estimate SHALL include 0 cost for the DONE chunks.

#### Scenario: estimate returns 0 cost for a fully completed job

Given a job where all chunks are DONE,

When `engine.estimate_cost(job_id)` is called,

Then `CostEstimate.usd == 0.0` and `CostEstimate.input_tokens == 0`.

#### Scenario: estimate includes within_budget flag

Given a job with `config.budget_usd = 1.00` and an estimated cost of `$0.75`,

When `engine.estimate_cost(job_id)` is called,

Then `CostEstimate.within_budget == True`.

Given a job with `config.budget_usd = 0.50` and an estimated cost of `$0.75`,

When `engine.estimate_cost(job_id)` is called,

Then `CostEstimate.within_budget == False`.

---

### Requirement: cost estimation and tracking reflect quality_mode

When `JobConfig.quality_mode == "reflective"`, both `estimate_cost` and real-time cost tracking SHALL account for the additional critique + revise provider passes per chunk (translate + critique + revise = 3 model passes). When `quality_mode == "fast"`, exactly 1 pass per chunk SHALL be counted.

#### Scenario: reflective estimate exceeds fast for the same job

Given two identical 10-chunk jobs A (`quality_mode="fast"`) and B (`quality_mode="reflective"`) on the same model and content,

When `estimate_cost` is called on both,

Then `CostEstimate.usd` for B SHALL be strictly greater than for A, reflecting the extra critique + revise passes.

#### Scenario: fast mode counts exactly one pass per chunk

Given a fast-mode job with 4 PENDING chunks,

When `estimate_cost` runs,

Then exactly one translate pass per PENDING chunk (4 total) SHALL be counted.

---

### Requirement: prompt caching of static instructions is applied when the cacheable block meets the provider minimum size

The `ContextManager` SHALL mark the static instruction block (the portion of the system prompt containing neutral-Spanish rules, output format, and fixed task description) as cacheable. Caching SHALL be applied only when the static block is ≥ the provider's documented minimum cacheable size. For the Anthropic provider the minimum is 1,024 tokens; for providers that do not support prompt caching the caching flag is silently ignored.

This is a behavioral rule, not an assumption: the adapter is responsible for honoring or ignoring the cache hint, and the domain is responsible for signaling the boundary.

#### Scenario: caching is applied when static block exceeds Anthropic minimum

Given an Anthropic provider and a static instruction block of 1,100 tokens,

When the system prompt is assembled by `ContextManager`,

Then the system prompt structure SHALL mark the static block with a caching boundary (e.g. via `anthropic.types.TextBlockParam` with `cache_control`), and `CostEstimate.cached == True` when `estimate_cost` is called.

#### Scenario: caching is NOT applied when static block is below minimum

Given an Anthropic provider and a static instruction block of 900 tokens (below the 1,024-token minimum),

When the system prompt is assembled by `ContextManager`,

Then no caching boundary SHALL be emitted and `CostEstimate.cached == False`.

#### Scenario: caching flag is silently ignored for providers without cache support

Given a `FakeTranslationProvider` (or an Ollama provider) that does not support prompt caching,

When the system prompt is assembled with a cacheable static block,

Then no exception SHALL be raised; the provider simply ignores the hint.

---

### Requirement: budget cap hard-stops the job BEFORE the call that would exceed it

When `JobConfig.budget_usd` is set, the orchestrator SHALL check the projected cost of the NEXT chunk call before making it. If `job.cost_usd + projected_next_chunk_cost > config.budget_usd`, the job SHALL be stopped BEFORE that call, `job.status` set to `JobStatus.PAUSED`, and a `BudgetExceeded` domain exception raised. Already-completed chunks SHALL remain `DONE` and their cost SHALL remain in `job.cost_usd`.

#### Scenario: job pauses before the chunk that would exceed budget

Given a budget of `$1.00`, a job where 8 chunks are DONE totaling `$0.93`, and chunk 9 is estimated at `$0.10`,

When the orchestrator prepares to translate chunk 9,

Then the orchestrator SHALL NOT make the provider call for chunk 9, SHALL set `job.status = JobStatus.PAUSED`, SHALL raise `BudgetExceeded`, and SHALL leave `job.cost_usd == 0.93` (the 8 DONE chunks' cost, unchanged).

#### Scenario: budget cap of None means no limit

Given `config.budget_usd = None`,

When any chunk is about to be translated regardless of accumulated cost,

Then no budget check is performed and the job proceeds without stopping.

#### Scenario: budget-stopped job is resumable after cap is raised

Given a job in `PAUSED` state due to `BudgetExceeded`,

When a caller updates `config.budget_usd` to a higher value and calls `engine.resume_job(job_id)`,

Then translation SHALL continue from the first non-DONE chunk and SHALL not re-translate already-DONE chunks.

Note: the mechanism for updating `budget_usd` on a persisted job is an API-layer concern; the spec requires the resume itself to use the updated cap.

---

### Requirement: cost is tracked and accumulated per chunk using REAL token usage

After each provider call completes (whether the chunk succeeds, fails, or retries), `job.cost_usd` SHALL be incremented by the REAL cost of that call, computed from `TranslationResult.usage` (input_tokens and output_tokens as reported by the provider), multiplied by `provider.price(model)`.

Real token counts are used for ALL call types: draft, critique, revise (reflective mode), tag-mismatch retries, strip/reinsert fallback calls, and failed-chunk calls. Pre-run `estimate_cost` continues to use `count_tokens` heuristics and may differ from actual accumulated cost.

The `Progress` object emitted via `on_progress` SHALL contain the current `cost_usd` value at the time of emission.

#### Scenario: cost accumulates correctly across chunks

Given a `FakeTranslationProvider` where each `translate` call reports usage derived from `count_tokens(system + user)` for input and `count_tokens(translation)` for output, and the model price is `$1.00/Mtok` input and `$5.00/Mtok` output,

When a job runs to completion,

Then `job.cost_usd` SHALL be strictly greater than 0 and SHALL grow monotonically with each additional chunk processed.

#### Scenario: reflective mode accrues cost for all 3 passes per chunk

Given a reflective-mode job (quality_mode="reflective") with 1 chunk,

When the chunk is translated (draft + critique + revise = 3 provider calls),

Then `job.cost_usd` SHALL equal the sum of the 3 individual call costs, NOT just 1 pass.

#### Scenario: failed chunks accrue non-zero cost

Given a job where chunk 0 fails all tag-mismatch retry attempts (3 primary + 1 fallback = 4 calls),

When the job ends PAUSED,

Then `job.cost_usd` SHALL be greater than 0, reflecting the token cost of all calls made before the failure was recorded.

#### Scenario: pre-run CostEstimate stays a conservative heuristic

The `CostEstimator` (used by `estimate_cost`) continues to use `count_tokens` heuristics for pre-run estimates; it does NOT use real usage (unavailable before translation). The estimate is intentionally conservative and may differ from actual accumulated cost after the job runs.

#### Scenario: progress callback carries current cost

Given a 3-chunk job running with `on_progress=callback`,

When chunk 1 completes (the second chunk),

Then the `Progress` object passed to `callback` SHALL have `cost_usd` equal to the accumulated real-usage cost of all calls made for chunks 0 and 1.
