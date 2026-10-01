# Resource-Vector And Provider-Aware Admission

Design for issue #651 under epic #645. Date: 2026-10-01 (GMT). Verified against `origin/main` d2470a43; every `path:line` below is pinned to that SHA.

This document is design-only. It promotes no implementation slice. It replaces the stub in [Task admission](task-admission.md) ("Resource-Vector Direction") with decisions, open questions, and evidence-gated follow-up issues. Where it disagrees with the stub's constraint list, the constraint list wins; the one sentence it amends is called out in [Decision 3](#decision-3-division-of-labor-with-request-admission).

## Problem Statement

Task admission leases scheduler resources before a worker is spawned. Request admission bounds concrete provider/model/domain calls and owns AIMD. The task lease is held through the worker's terminal path, including while the worker waits inside request admission (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:2131`; [Task admission](task-admission.md) "V1 Lease Boundary"). The open risk is the cross-provider case in [Architecture](architecture.md) "Two-Stage Admission": tasks blocked on a cooled-down provider could occupy scheduler slots while another provider has ready work.

Three changes already narrow that risk:

- Bounded borrow (#650/#693) is on by default (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:238`).
- The request-pressure advisory (#661) skips a pressured candidate when an eligible unpressured peer exists, and the builder enables it in production (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:841-867`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/dataset_builder.py:1010`).
- #730 (4fe479c1, merged 2026-06-02, fixes #725) added the `request:{provider}/{model}` scheduler resource. Its description says it bounds "scheduler model-task admission with provider/model request capacity" and keeps "the existing request-pressure advisory as a fairness hint rather than adding a second pressure-budget dispatch policy". #743 (646ee21b) later localized deferred admission. The #730 harness showed 47/128 rows completed with 81 dropped before and 128/128 with 0 dropped after. That is evidence the per-key cap prevents request-admission timeout row drops. It is not a measurement of cross-provider idle time, and this document does not use it as one.

The residual gap is therefore narrower than "invent a resource vector". The vector shape and per-provider/model keys exist. What is unresolved:

1. Whether the current key set is sufficient: the request domain is dropped from the key, and each key has one scalar cap.
2. Whether work that is not bounded by the per-key cap (`custom_model` tasks, shared `llm_wait` and `submission` pools) can still hold every slot while one provider is cooled down.
3. Whether the task lease should be held during request wait.
4. Whether per-resource debt is enough for tasks that request several typed resources.
5. The missing measurement: no metric today reports zero in-flight request time per provider/model/domain.

Whether item 2 reproduces after #730 is unknown. This document treats it as a hypothesis with a benchmark gate, not as an observed failure.

## Success Metrics

Primary metric, defined in [Zero-Inflight Idle Metric](#zero-inflight-idle-metric): per provider/model/domain zero-inflight idle seconds, as #651 defines it: total workflow wall time where that resource has no in-flight generation request while the workflow is still active. Reported as per-resource values, total, max, and delta against a named baseline. A narrower ready-gated variant (idle only while dependency-ready work for that resource exists) is reported beside it as a diagnostic and is not the gated number.

Guardrails, reusing `plans/645/benchmark-plan.md` thresholds:

- Neutral scenarios no worse than 5 percent mean wall time (`plans/645/benchmark-plan.md:229`).
- No permit leaks and deterministic output equality (`plans/645/benchmark-plan.md:233`).
- Cross-provider cooldown: provider B ready work keeps receiving scheduler task leases while provider A is cooling down (`plans/645/benchmark-plan.md:242`).
- Single-resource and single-group workloads stay live (`plans/645/task-admission.md:171-173`).
- Paired same-machine runs, at least five measured iterations, mean/p50/p95/min/max/stddev reported (`plans/645/benchmark-plan.md:227`).

No behavior claim ("improves utilization", "reproduces") is made here without a cited benchmark artifact.

## Key Requirements

- Scheduler-internal. No new public metadata beyond #641 (`plans/645/task-admission.md:187`).
- Consume resolved metadata from `TaskSchedulingResolver` (`plans/645/task-admission.md:188`).
- Read request pressure only through the read-only `RequestPressureSnapshotProvider` (`packages/data-designer-engine/src/data_designer/engine/models/request_admission/pressure.py:46`).
- No duplicated AIMD. Request admission owns provider/model/domain limits ([Request admission](request-admission.md)).
- Every task lease is released exactly once; acquire stays all-or-nothing across a request's resources (`plans/645/architecture.md:124`).
- `FairTaskQueue` owns ready ordering only. It does not own resources, pressure, or provider state (`plans/645/task-admission.md:49`, `plans/645/task-admission.md:60`).
- Single-resource and single-group liveness is preserved (`plans/645/task-admission.md:171`).
- A durable policy must subsume the #661 advisory, not stack on it.

## Current State

All rows were read on d2470a43.

| Fact | Location |
| --- | --- |
| `SchedulerResourceKey` is a `str`. `SchedulerResourceRequest.amounts` is already a `Mapping[str, int]` validated for non-empty string keys and positive integer amounts | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resources.py:14`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resources.py:35-45` |
| Provider/model scheduler resource key is `request:{provider}/{model}`; the request domain (chat/embedding/image) is dropped | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resources.py:66-68` |
| Resolver: a `kind == "model"` task with a valid `RequestDomain` requests `submission`, `llm_wait`, and `request:<provider>/<model>`; local tasks request only `submission`; other model-like kinds (for example `custom_model`) request `submission` and `llm_wait` but no request key, so the per-key cap does not bound them | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resolver.py:99-121`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resolver.py:135-143` |
| Per-request-key scheduler limit is `max(1, metadata.weight)`, minimum across generators sharing a key | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resolver.py:123-132` |
| Per-group admitted limit is `max(1, min(cap, multiplier * weight))` | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resolver.py:108` |
| `llm_wait` limit is `max_model_task_admission`; request-key limits are merged in; the default admission config passes `BoundedBorrowTaskAdmissionPolicyConfig()` | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:226-240` |
| Bounded-borrow `evaluate` already loops over every resource in `item.resource_request.amounts` (strict share, peer pressure, debt, ceiling) | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:144-217` |
| Strict share rounds up by default; a solo group synthesizes one equal peer (2x weight); capped by `admitted_limit` and the resource limit | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:54`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:290-319` |
| Borrow ceiling precedence: per-(group, resource), then default, then dynamic | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:242-269` |
| Dynamic reserve defaults are fraction `0.0` and at most 8 slots, so a solo group may borrow up to the full resource limit | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:33-34`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:322-323` |
| Debt accrues in `on_acquire`; `on_release` repays by every resource in the lease, at group level, clamped by the controller | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:219-240` |
| Peer-pressure resources exclude `submission` when any typed resource exists; peer hard-eligibility is checked across all of the peer's resources | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:344-350`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:353-357` |
| Advisory: skip a pressured candidate when an eligible unpressured peer exists; read-only; pressure reasons are `provider_model_aggregate_cap`, `cooldown`, `waiters`, `resource_limit` | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:841-867`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:872-898`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:900-912` |
| Advisory defaults to off in the constructor and is switched on by the builder | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:216`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/dataset_builder.py:1010` |
| Event kind `request_pressure_advisory_skipped`; health snapshot exposes the skip counter | `packages/data-designer-engine/src/data_designer/engine/observability.py:102`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:605-606` |
| Task lease is released in the worker `finally` after the task terminal outcome, so it spans request wait; the spawn path releases only on spawn failure | `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:2108-2131`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/async_scheduler.py:914-937` |
| Request-stage events `request_lease_acquired`, `model_request_started`, `model_request_completed`, `request_lease_released`; the event dataclass carries `request_resource_key` | `packages/data-designer-engine/src/data_designer/engine/observability.py:127-129`, `packages/data-designer-engine/src/data_designer/engine/observability.py:136`, `packages/data-designer-engine/src/data_designer/engine/observability.py:186` |
| The executor acquires and releases one request lease per outbound attempt (the retry loop wraps the per-attempt function; the sync path at `:131-159` and the async path at `:161-174` are symmetric) and emits `model_request_started`/`model_request_completed` with the resource key; `model_request_started` is inside the lease, `model_request_completed` is emitted after its release | `packages/data-designer-engine/src/data_designer/engine/models/clients/model_request_executor.py:161-169`, `packages/data-designer-engine/src/data_designer/engine/models/clients/model_request_executor.py:171-174`, `packages/data-designer-engine/src/data_designer/engine/models/clients/model_request_executor.py:131`, `packages/data-designer-engine/src/data_designer/engine/models/clients/model_request_executor.py:314` |
| Existing idle metric: per generation column, zero-in-flight seconds over the whole run, with total, max, and baseline deltas. In-flight is the task interval `dispatch_at -> completed_at`, so it counts request wait as busy | `scripts/benchmarks/benchmark_bounded_borrow_admission.py:81-84`, `scripts/benchmarks/benchmark_bounded_borrow_admission.py:327-338`, `scripts/benchmarks/benchmark_bounded_borrow_admission.py:364-382` |

## Design Decisions

Default posture: keep the current internal shape, document why, and name what evidence would reverse each decision.

### Decision 1: Internal, not public

Resource-vector policy stays scheduler-internal and derives everything from `SchedulingMetadata` through `TaskSchedulingResolver`. No per-model scheduling field is added to public config or plugin metadata.

Reason: the resolver already produces the full vector from existing metadata (`weight`, kind, identity), and a public knob would freeze a shape before any benchmark shows the current one is wrong ([Task admission](task-admission.md) "Resource-Vector Direction"; `plans/645/task-admission.md:179` and `plans/645/architecture.md:138`: no public knob before benchmark evidence).

Reversed by: a benchmark showing a plugin-declared resource the resolver cannot derive, with measured idle time attributable to it.

### Decision 2: Keep the `request:{provider}/{model}` key shape

Keep one key per provider/model with the domain dropped and one scalar cap of `max(1, metadata.weight)`. Do not introduce per-domain or per-endpoint keys now.

Reason: no evidence yet that a real workload mixes domains on one provider/model with different limits, and request admission already enforces per-domain limits (`plans/645/request-admission.md`). Adding keys multiplies debt and strict-share state in the bounded-borrow loop (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:156-213`) for no demonstrated gain.

Known limitation, kept visible: `custom_model` tasks get no request key (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resolver.py:135-137`), so they are bounded only by `llm_wait` and `submission`. See Q1.

Reversed by: a mixed-domain or `custom_model` benchmark showing idle time on a healthy provider that a domain-aware or custom-model key removes.

### Decision 3: Division of labor with request admission

Request admission owns provider/model/domain limits, cooldown, and AIMD. The scheduler-side `request:` key is a static shadow cap derived from `metadata.weight` (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resolver.py:130`). It never reads the AIMD current limit and never replaces request admission. Task policy may order or skip candidates; it does not throttle requests, pre-acquire request permits, or run a second AIMD.

This is a clarification of the stub, not a reversal. The stub's sentence already allows "scheduler-owned task-stage resources derived from `SchedulingMetadata`" (`plans/645/task-admission.md:183`), and `request_scheduler_resource_key` is documented as exactly that kind of resource (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resources.py:66-68`). What the sentence did not anticipate is a scheduler resource named after a provider/model. `plans/645/task-admission.md:183` is edited to say so.

The #661 advisory is the fast path and #730 kept it as a fairness hint, not a second dispatch policy. Any durable provider-aware policy must replace the advisory's skip logic, not run beside it, so that one pressured candidate is not penalized twice.

Reversed by: a benchmark in which the static shadow cap blocks work that request admission would have admitted (the cap is tighter than the AIMD limit and idles a healthy provider).

### Decision 4: Bounded borrow carries forward unchanged

The bounded-borrow loop already evaluates every resource in the request and denies on the first failing one (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:156-213`). `request:` keys therefore already get strict share, peer pressure, debt, and ceiling like `llm_wait`. No policy change is proposed.

Two consequences:

- The default dynamic reserve is fraction `0.0`, so a solo group may borrow up to the full resource limit (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:33`, `packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:322-323`).
- Debt is group-level and repaid by every resource of any completed lease in the group (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/task_policies.py:237-240`). A request that holds several resources can repay debt on resources it did not borrow.

Bounded-borrow semantics stay policy-compatible with provider/model keys, and no policy revision is required now. The cross-resource repayment is acceptable for now because the controller clamps repayment to the group's outstanding debt, so the worst case is early forgiveness on one resource, never negative debt or a leaked permit, and no current workload is known to fail on it. If Q4's scenario fails, this repayment rule is the first candidate revision (per-resource repayment), taken in Follow-up C.

Whether this needs dominant-resource fairness is Q4. It stays open unless a concrete scenario fails.

### Decision 5: Idle metric from request-stage events

Defined in the next section. The decision is to measure at the request stage, not the task stage.

### Decision 6: Lifecycle and liveness deltas

- Keep the V1 rule: the task lease is held through request wait. Yield/reacquire is deferred (Q3), because it needs either a change to "released exactly once" (`plans/645/architecture.md:124`) or a new lease lifecycle state.
- Provider-aware lease selection (preferring candidates whose provider is not cooled down) is a candidate follow-up, gated on benchmark evidence, and would subsume the advisory (Decision 3).
- Liveness, release accounting, cancellation, error, stale/retry/salvage, and telemetry correlation follow the existing contracts in [Task admission](task-admission.md) ("Lease Lifecycle", lines 146-157) and [Observability](observability.md). This document adds no new rules to them.

## Zero-Inflight Idle Metric

The #693 harness already reports `generation_column_idle_seconds`, `total_generation_column_idle_seconds`, `max_generation_column_idle_seconds`, and baseline deltas via `_zero_inflight_idle_seconds` (`scripts/benchmarks/benchmark_bounded_borrow_admission.py:81-84`, `scripts/benchmarks/benchmark_bounded_borrow_admission.py:364-382`). Two properties make it unsuitable for #651 as is:

- It measures in-flight by task interval `dispatch_at -> completed_at` (`scripts/benchmarks/benchmark_bounded_borrow_admission.py:46-51`). A task parked in request admission for a cooled-down provider counts as busy, which hides the idleness #651 is about.
- It is keyed per generation column, not per provider/model/domain resource, and its window starts at `0.0` rather than at workflow start.

Specification for the provider-aware version, keeping the existing field names and delta shape:

- Key: `request_resource_key` (provider, model, domain). Aggregate to provider/model by grouping.
- In-flight interval source: `model_request_started` to `model_request_completed`, joined by `request_lease_id`. These are the outbound call, which matches the issue's definition of idle as no in-flight generation request. The request lease spans nearly the same interval but is not nested with it: the order is acquire, `model_request_started`, release, then `model_request_completed`, which is emitted in the outer `finally` after the release (`packages/data-designer-engine/src/data_designer/engine/models/clients/model_request_executor.py:131-159`). Follow-up A must therefore not assume the completed event precedes the lease release when joining on `request_lease_id`. The lease is held per attempt, not across retries (`packages/data-designer-engine/src/data_designer/engine/models/clients/model_request_executor.py:161-169`), so `request_lease_acquired`/`request_lease_released` (`packages/data-designer-engine/src/data_designer/engine/observability.py:127`, `packages/data-designer-engine/src/data_designer/engine/observability.py:136`) are a cross-check and also expose request-admission wait. Task leases are not a valid source.
- Idle window (gated, the #651 definition): wall time from workflow start to workflow end during which the resource has no in-flight request.
- Ready-gated idle (diagnostic only): the subset of that time during which dependency-ready work for that resource exists. This ties the diagnostic to `ready_idle_gap` (`plans/645/benchmark-plan.md:99`) and removes the pre-ready offset, so it separates idleness the scheduler could have avoided from idleness caused by missing work. It never replaces the gated number.
- Report per resource, total, max, and delta against the named baseline SHA.
- Artifact fields: reuse `scheduler_resource_key` and `request_resource_key` (`plans/645/benchmark-plan.md:85-86`). Snapshots are point samples, not intervals, so they cross-check only.

The metric is not implemented. The existing script is a simulation of the queue and controller without request admission, so building the real metric needs a mock-endpoint harness that emits request events (`plans/645/benchmark-plan.md:184`). That is Follow-up A.

## Rejected Alternatives

- Public per-model scheduling metadata. Reason in Decision 1.
- A second AIMD at task level. Duplicates request admission and breaks `plans/645/task-admission.md:189`.
- Pre-acquiring request permits at task admission. A task's metadata is not a promise of request count (`plans/645/architecture.md:112`).
- Per-task token or cost budgets. Excluded from V1 (`plans/645/task-admission.md:113`) and no evidence they are needed.
- Dominant-resource fairness now. Deferred to Q4 until a scenario with two typed resources fails.
- Yield/reacquire now. Deferred to Q3 until Follow-up B shows an idle gap.
- Making the #661 advisory the durable policy. It is read-only and per-selection by design (`plans/645/task-admission.md:105`).

## Open Questions

| # | Question | Owner | Needed evidence |
| --- | --- | --- | --- |
| Q1 | Is `request:<provider>/<model>` (domain dropped, scalar cap) the right granularity? `custom_model` tasks have no request key (`packages/data-designer-engine/src/data_designer/engine/dataset_builders/scheduling/resolver.py:135-137`): acceptable or a gap? | Follow-up B | A workload mixing domains or `custom_model` work on one provider/model, with idle time measured per resource |
| Q2 | After #730, does a cooled-down-provider idle gap still occur? #730 does not bound shared `llm_wait`/`submission` pools, `custom_model` tasks, or leases held during request wait | Follow-up B | Cross-provider cooldown run (`plans/645/benchmark-plan.md:201`) with the request-key cap and the advisory each on and off |
| Q3 | Yield/reacquire versus provider-aware lease selection only | Follow-up D, after B | Reproducible idle gap in B that selection alone cannot close |
| Q4 | Is per-(group, resource) debt with independent checks enough for requests holding two typed resources, or is dominant-resource accounting needed? | Follow-up C, after B | A concrete asymmetric-capacity scenario that fails under current debt rules |
| Q5 | Are request-stage events plus `request_resource_key` sufficient to derive per provider/model/domain in-flight intervals? Answer from the code read: yes for fields (`packages/data-designer-engine/src/data_designer/engine/observability.py:127-129`, `packages/data-designer-engine/src/data_designer/engine/observability.py:186`, `packages/data-designer-engine/src/data_designer/engine/models/clients/model_request_executor.py:314`), so [Observability](observability.md) is not edited. Remaining check: the Follow-up A harness confirms the events arrive in order under real concurrency | Follow-up A | A passing harness run |

## Follow-up Issues

Baseline for all four: d2470a43 (`origin/main` at the time of writing, which already contains bounded borrow on by default and the #661 advisory). Each issue re-pins to the then-current accepted SHA at filing time, per `plans/645/benchmark-plan.md:223`. These are drafts, not filed issues.

| ID | Scope | Depends on | Evidence gate |
| --- | --- | --- | --- |
| A | Add the per provider/model/domain zero-inflight idle metric to the benchmark harness from request-stage events, with a mock-endpoint pool | none | Metric reproduces hand-computed idle seconds on a scripted transcript; no change to scheduler or request-admission behavior; neutral wall time within 5 percent (`plans/645/benchmark-plan.md:229`) |
| B | Add the cross-provider cooldown scenario (provider A cooling down, provider B has ready independent work), run with the request-key cap and the advisory each on and off. Also add the mixed-domain and `custom_model` workload on one provider/model (Benchmark Matrix). Produces the evidence for Q1 and Q2 | A | Written result stating whether idle time on provider B is reproducible at the baseline SHA, with the five-iteration statistics from `plans/645/benchmark-plan.md:227` |
| C | Conditional: provider-aware task policy (selection or dominant-resource accounting) that subsumes the advisory | B shows a reproducible idle gap | Provider B keeps receiving task leases during provider A cooldown (`plans/645/benchmark-plan.md:242`); neutral scenarios within 5 percent (`plans/645/benchmark-plan.md:229`); no permit leaks (`plans/645/benchmark-plan.md:233`); idle delta against the named baseline is negative |
| D | Conditional: yield/reacquire of the task lease during request wait, with the lease-lifecycle change to `plans/645/architecture.md:124` | B shows a gap that C cannot close | Same gates as C, plus the hidden-waiter proof across success, failure, cancellation, and salvage paths (`plans/645/benchmark-plan.md:239`) and exactly-once lease release accounting |

Common gate for C and D. #651 requires the implementation acceptance bar to include "liveness, no permit leaks, multi-resource fairness tests, stale/retry/salvage behavior, correlated telemetry compatibility, and #649 benchmark evidence". Each of C and D must pass all six, in addition to its own row:

- Liveness: single-resource and single-group workloads stay live (`plans/645/task-admission.md:171-173`).
- No permit leaks: exactly-once release (`plans/645/architecture.md:124`, `plans/645/benchmark-plan.md:233`).
- Multi-resource fairness tests: requests holding two typed resources under asymmetric capacities; for C this is the gate that closes Q4.
- Stale/retry/salvage behavior: per the Lease Lifecycle in [Task admission](task-admission.md) (lines 146-157) and `plans/645/benchmark-plan.md:239`.
- Correlated telemetry compatibility: `scheduler_resource_key` and `request_resource_key` stay joinable (`plans/645/benchmark-plan.md:85-86`, [Observability](observability.md)).
- #649 benchmark evidence: the Benchmark Matrix below, run on the harness from Follow-up A, including the gated idle metric.

If B shows no reproducible gap, C and D are not filed and this document is closed as "per-key cap and advisory are sufficient at the baseline".

## Benchmark Matrix

Run through the reusable harness ([Benchmark plan](benchmark-plan.md)), baseline as named in Follow-up Issues:

- Asymmetric provider/model capacities.
- Different dominant resources per group.
- Neutral single-resource workload (guardrail).
- Heavy-root workload (`plans/645/benchmark-plan.md:231`).
- Correlated request traces.
- Cross-provider cooldown with advisory and request-key cap each on and off.
- Mixed-domain and `custom_model` workloads on one provider/model (Q1).
