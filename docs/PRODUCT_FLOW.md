# COLFI Institution-Agent Stress Lab — v5 product and experiment contract

## Document status

This is the target contract for the real product. It replaces the March 2020
execution-pacing demo as the product definition. Existing v4 results remain
useful only as **execution-pacing scenarios** and must not be described as a
test of contagion, shared-model amplification or actual firm behaviour.

The build remains SQLite-first until the experiment below works end to end and
has completed a defensible repeated run. PostgreSQL, account authentication,
tenancy and production data licensing are deliberately outside this phase.
There is no mock-up track: each milestone must operate in the application and
persist its real inputs, outputs and audit records.

### Current implementation status

- M0 engineering is complete: six live models across three providers, neutral case inputs,
  reference-only control evidence, content hashing, base currencies,
  matched-date GBP/USD and provider-default confirmatory sampling are present.
- M1 engineering is complete: the seven workbook risk profiles and user-supplied
  editable holdings defaults are typed and persisted; the discriminated action
  union exists; normalized append-only SQLite tables are migrated; and the
  confirmatory preregistration is stored under its SHA-256.
- M2 engineering is complete: a point-in-time evidence snapshot and the exact
  preregistration require separate named SHA-256 approvals; the deterministic
  210-cell schedule is stored only after both approvals; and the live-only
  runner persists every provider attempt, raw response, typed decision and
  normalized action in append-only SQLite records.
- The pairwise estimator enforces a complete 7 × 6 × 5 grid, reports four
  components, whole-repetition bootstrap intervals, deterministic permutation
  nulls and model main effects, then freezes its decision-set and result hashes.
- The M2 operational gate remains open: no confirmatory collection is claimed
  until an operator approves the final evidence and preregistration hashes,
  confirms the materialized plan hash and completes all 210 accepted live
  decisions.
- M3 engineering is complete: 60 immutable worlds resolve only exact accepted
  same-repetition decisions; the first transition emits one common and seven
  isolated private snapshots; 420 feedback cells preserve those isolation
  boundaries; and the second transition freezes auditable world trajectories.
- The transition policy is part of the preregistration. Its current ADV, depth
  and impact values are plainly labelled normalized, operator-editable exercise
  defaults—not observed market data—and therefore require explicit approval.
  EUR/JPY, USD/JPY and EUR/USD paths make base-currency translation explicit.
- The M3 operational gate remains open until the approved 210 initial and 420
  feedback calls have actually completed. M4 paired safeguards is next.

## Governed operator flow — zoomed out

This is the supervisor-facing product flow. The detailed contracts later in
this document implement it; they do not replace it.

```text
1. Pick the bad day
   ↓
2. Collect and approve the facts ─────── human evidence approval + SHA-256
   ↓
3. Set up and approve seven firms ────── human preregistration approval + SHA-256
   ↓                                      steps 1–3 are now frozen
4. Ask every firm through every AI, repeatedly
   ↓
5. Add up one world's actions and move the scenario market
   ↓
6. Show each firm the common market move and only its own new position; ask again
   ↻ back to step 5 to aggregate the second answers
   ↓
7. Run the matched loop with the approved limits switched on
   ↓
8. Compare agreement, amplification and safeguard effects like with like
   ↓
9. A person reviews and signs the exact report/input hashes
```

Steps 1–3 happen once per preregistration version. Later model output cannot
change their facts, profiles, holdings, models, metrics or policies. A material
edit creates a new immutable version and requires new human approval.

Step 4 is repeated deliberately: five calls per institution/model cell across
all six models. This establishes within-model stability and supports the
same-model versus cross-model comparison rather than treating one completion as
evidence of agreement.

The first confirmatory feedback cycle is `5 → 6 → 5`: initial answers create T2,
T2 is supplied to seven feedback decisions inside the same world, and those
second answers are aggregated once more to measure amplification. Additional
decision cycles require a new preregistered stage count; the engine never loops
until a desired result appears.

Step 7 keeps the incoming intent paired. Execution limits are first applied to
the exact same accepted actions, producing a safeguarded market/private state.
Fresh feedback decisions may then respond to that different safeguarded state;
they are labelled downstream safeguard responses, never presented as if the
cap itself changed the original intent. Mandate changes remain a separate
safeguard family.

## Questions the product answers

### UC-01 — convergence despite heterogeneity

Under the same approved 5 August 2024 shock, how aligned are the actions of
seven institution archetypes with different objectives, exposures, constraints
and private risk states? Does alignment increase after they observe the first
round of system feedback?

### UC-02 — additional shared-model dependency

At the initial stage, holding institution, evidence and repetition constant,
how much higher is pairwise action agreement for same-model institution pairs
than for cross-model pairs? In the feedback stage, how do the trajectories of
worlds where every archetype uses model `k` differ from balanced heterogeneous
worlds after each world evolves through its own market state?

UC-02 is not a separate data-collection arm. It is a set of contrasts derived
from the same crossed run grid as UC-01. This prevents one hand-picked shared
model from determining the result.

### UC-03 — information-clock transmission

How does stress propagate as information reaches markets in different time
zones, and what changes under market-structure safeguards?

UC-03 is a later, separate experiment. It must not replace UC-01/UC-02 or be
inferred from their stage-based simulation.

## Non-negotiable experimental design

The initial-stage collection grid is:

```text
institution × model × repetition
```

For the first valid run:

- seven preregistered institution archetypes;
- all six configured models;
- five repetitions per initial cell;
- randomised call order;
- equal completed cell counts before comparison.

This produces 210 initial decisions:

```text
7 institutions × 6 models × 5 repetitions = 210
```

The full grid supports the primary pairwise estimator and supplies actions for
the feedback worlds. It does **not** carry forward as 210 independent feedback
cells. A feedback state is caused by seven institutions acting together, so its
unit is a versioned world:

```text
assignment world × repetition → seven stage-one actions → one transition
→ one common market snapshot + seven private institution snapshots
→ seven feedback decisions
```

There are six `SHARED-k` assignments and six cyclic heterogeneous cohorts. With
five repetitions, feedback therefore requires 420 decisions:

```text
(6 shared worlds + 6 heterogeneous worlds) × 5 repetitions × 7 institutions
= 420
```

The initial and feedback stages total 630 institution calls before any fresh
mandate-safeguard decisions. Replicate `r` in a feedback world may consume only
replicate `r`'s seven initial decisions for that exact assignment; pooled,
averaged or consensus transitions are prohibited.

The app's low-cost default rotation is only for interactive and smoke runs:

1. DeepSeek V4 Flash
2. Claude Haiku 4.5
3. Grok 4.3
4. DeepSeek V4 Pro
5. Claude Sonnet 5
6. Grok 4.3
7. Grok 4.6

This makes Grok 4.3 more frequent than Grok 4.6 during ordinary use. A
preregistered comparative run remains balanced across models; changing sample
counts to save money would confound the model contrast. DeepSeek V4 Flash,
Claude Haiku 4.5 and Grok 4.3 are the cheap general-purpose paths. A cheaper
coding-only or experimental model is not added merely to increase the count.

## Fixed case definition

### Event

UC-01/UC-02 use the 5 August 2024 yen-carry unwind. The evidence set also
contains the 31 July Bank of Japan rate decision and the 2 August US jobs report
as timestamped antecedents.

Closing values alone are insufficient, but tick data is not required. Approved
daily/session OHLC records provide the Japanese close, VIX pre-open high and US
open/close; exchange calendars provide the session boundaries. Each value still
needs an `observed_at` and `available_at`, and the case must distinguish:

- the pre-event reference state;
- the Japanese close and European observations available before T1;
- the VIX pre-open peak and US opening observations available by T1;
- the engine-generated T2 counterfactual; and
- the actual US close, retained only as an out-of-sample comparison.

Instrument selection is part of the preregistration. The initial set must use
tradable proxies with consistent units, currencies, sessions and timestamp
semantics. GBP/USD is derived only from matched-date ECB reference rates.

### Preregistration

Before calls begin, the supervisor approves a machine-readable record containing:

- research question and hypotheses;
- event, instruments and evidence cut-off;
- institution-profile version;
- model roster and exact model identifiers;
- repetitions, call-order randomisation method and supported seed policy;
- heterogeneous-cohort construction rule and feedback-world definitions;
- decision timestamps, evidence cut-offs and counterfactual boundary;
- sampling/temperature policy and sensitivity run;
- stage-transition rule;
- action and impact-model versions;
- heterogeneous assignment;
- metrics, permutation procedure and safeguard policies; and
- release-blocking validity checks.

Canonical JSON is hashed as `preregistration_sha256`. The approval actor, time,
note and approved hash are stored in SQLite. Any material change creates a new
version and requires a fresh approval; it never mutates an approved run.

### Phase-zero decisions — locked

1. **Cohorts:** order institutions and models by their preregistered identifiers,
   then define heterogeneous cohort `c` by assigning institution `i` to model
   `M[(i + c) mod 6]` for `c = 0..5`, so every institution uses every model once
   across cohorts and the unavoidable seventh assignment rotates evenly.
2. **Decision time:** T1 is `2024-08-05T09:35:00-04:00` (`13:35:00Z`), five
   minutes after the US core open; only evidence available by that instant is
   model-visible, while T2 is the engine-generated counterfactual at
   `2024-08-05T10:05:00-04:00` and the actual later rebound is evaluation-only.
3. **Size basis:** every action carries an explicit `size_basis`; UC-01/UC-02
   trades and hedges use percent of institution footprint notional, deleveraging
   uses percent of gross exposure to reduce, and liquidity withdrawal uses
   percent of the institution's displayed/provided depth.
4. **Double counting:** directed trades are applied first and a deleverage target
   creates only the residual forced orders required after those trades, so a
   trade and deleverage instruction affecting the same instrument are never
   simply added.
5. **Temperature:** the confirmatory run omits temperature and uses each exact
   model's recorded provider default; one separately labelled sensitivity grid
   uses `temperature=0.2` only for models that accept that control and never
   enters the primary cross-model estimator.

The T1 choice follows the 09:30 US core open and occurs after Tokyo's then-15:00
close and the documented pre-open VIX high. An OHLC field is admissible only if
its underlying event occurred by T1; a full-day high, low or close is not made
available early merely because it later appears in a daily row.

## Institution input contract

Every archetype receives a private, versioned profile from the concept-note
Inputs sheet. The profile is present from the first decision, not added later
inside the impact engine.

Required fields are:

- institution type and investment/intermediation objective;
- primary constraint and active trigger;
- declared system footprint;
- holdings and exposure matrix by instrument and currency;
- current loss or drawdown state;
- cash or liquidity buffer;
- margin or capital headroom;
- VaR, risk-budget, redemption, inventory or tracking state where applicable;
- permitted action types, hedges and mandate limits; and
- base currency and mark-to-market rule.

The seven declared footprints from the input material are used; they replace
the old equal-notional assumption. Profiles are synthetic archetypes, never
claims about the named firms that inspired the source material.

The initial holdings matrix uses these user-supplied, UI-editable defaults:

| Institution | Default tradable-proxy holdings |
| --- | --- |
| Institution 1 | 100% S&P 500 |
| Institution 2 | 100% Nasdaq 100 |
| Institution 3 | 100% Nikkei 225 |
| Institution 4 | 100% STOXX 50 |
| Institution 5 | 50% S&P 500; 50% Nasdaq 100 |
| Institution 6 | 50% Nikkei 225; 50% STOXX 50 |
| Institution 7 | 25% each S&P 500, Nasdaq 100, Nikkei 225 and STOXX 50 |

Each selected institution's weights must total 100%. Operator changes are
validated, stored in the assessment request and included in the case-content
hash; they never mutate the catalog default. These holdings are scenario inputs,
not actual institutional positions.

No example action path is prepopulated. In particular, the product does not
encode “hold in control”, “sell under stress” or a desired safeguarded response.
Actions must be accepted live-model decisions under the typed schema, except
for deterministic execution-safeguard replays of already accepted intent.

An isolation test must prove that institution `i`'s private profile cannot
appear in any other institution's provider request.

## Model-visible case contract

One neutral system instruction is used for every institution, model, repetition
and stage. Model-visible input must not contain:

- experiment arm names such as `control`, `stress`, `HET` or `SHARED`;
- a desired diagnosis such as contagion, convergence or fire sale;
- the assigned model name;
- a future stage or post-cut-off observation;
- safeguard outcome claims; or
- generated case wording that changes with assignment.

The provider-independent payload is canonicalised and hashed as
`case_input_sha256`. It must be identical when only the model assignment or
repetition changes. The actual provider request is separately hashed as
`provider_request_sha256` and records provider, exact model identifier,
supported sampling controls, latency, token use, finish reason and raw-response
digest.

Control/look-ahead tests use only the pre-event reference observation. They are
validity and negative-control tests, not the UC-01 headline arm.

## Decision and action contract

An institution returns one typed decision containing zero or more of these
simultaneous action categories:

| Action | Required size semantics | Engine treatment |
| --- | --- | --- |
| Sell | Named instrument; `footprint_notional_pct` | Directed sale flow |
| Hedge | Named permitted hedge instrument, direction; `footprint_notional_pct` | Full order in the hedge instrument against that instrument's own depth |
| Deleverage | `gross_exposure_reduction_pct` plus liquidation priority | Residual forced orders required after directed trades |
| Withdraw liquidity | Named market; `provided_depth_pct` | Reduces available depth; never added to sell volume |
| Buy/support | Named instrument; `footprint_notional_pct` | Offsetting purchase/support flow |

The decision also records stance, urgency, confidence, constraints considered,
cited evidence identifiers and a short rationale. Absence of an action differs
from a zero-size action. Units, bounds and instrument permissions are validated
before a response enters aggregation. The schema stores `size_value`,
`size_basis` and unit separately; it never infers a basis from action type or
silently converts percent-of-position into percent-of-footprint.

Hedges are orders, not a discounted pressure score. A futures hedge is routed
to the named futures/proxy instrument and consumes that instrument's declared
depth. Its risk effect and its market flow are stored separately.

When one decision contains both directed trades and a deleverage target, the
engine marks positions through the directed trades first, recomputes gross
exposure, then generates only the residual orders required to reach the target
using the declared priority rule. If directed trades already meet or exceed the
target, the residual is zero. These generated orders are labelled `forced` and
retain their originating `DeleverageAction` identifier.

Recommended, forced, hedge, liquidity and support actions remain separately
identifiable throughout storage, simulation, scoring and reporting.

## Stage flow

### 1. Initial decision

Each crossed cell receives the same approved event snapshot plus its one private
institution profile. Decisions are immutable once accepted.

### 2. Build immutable worlds

For each assignment and replicate, the engine resolves exactly seven accepted
initial decisions from the full grid:

- `SHARED-k/r` selects every institution's model-`k`, replicate-`r` decision;
- `HET-c/r` selects the model assigned by cohort `c` for each institution, always
  at replicate `r`.

The ordered decision identifiers and their content digests form
`stage1_action_set_sha256`. A world becomes immutable before transition.

### 3. Deterministic transition

Each world transitions independently. The engine aggregates only that world's
seven actions using declared footprints and holdings and emits:

- one common market snapshot containing prices, depth and system-wide flow; and
- one private snapshot per institution containing its executed positions,
  remaining cash, loss, margin/capital headroom and trigger state.

The common and private snapshots have separate canonical hashes. No world may
consume another world's actions or snapshot, and no private snapshot may reach
a different institution.

### 4. Feedback decision

Each world makes seven feedback calls using the same model assignment that
created it. A call receives the original approved case, that world's common
snapshot and only the matching institution's private snapshot. The run cell
records `world_id`, `stage1_action_set_sha256`, `common_snapshot_sha256` and
`private_snapshot_sha256`. Stage-one intent is never overwritten.

This closed loop answers whether alignment and amplification rise as stress
develops. A pooled transition, a replay of initial orders, or a feedback call
without the institution's post-execution state does not count as feedback.

### 5. Safeguard evaluation

Safeguard families remain separate:

- **Execution safeguard:** deterministic paired replay of the exact same intent
  with a cap or staging rule. No second model call is allowed.
- **Mandate safeguard:** a fresh crossed set of decisions after an explicit
  mandate or constraint change. Intent changes and execution effects are
  reported separately.
- **Market-structure safeguard:** circuit breakers, venue rules or information
  timing. This belongs to UC-03 unless specifically preregistered otherwise.

A cap cannot be credited for lower urgency, changed confidence, or a new buy
action because those are changes in intent.

## Fire-sale and amplification engine

The v5 engine replaces equal-notional index weights with:

- an instrument-level holdings matrix;
- base-currency mark-to-market;
- declared institution footprints;
- named per-archetype triggers;
- configurable pro-rata or waterfall liquidation priority;
- distinct AI-directed, forced, hedge and support order streams;
- liquidity withdrawal applied as a reduction in round depth; and
- versioned instrument-level ADV/depth assumptions with provenance.

Primary impact uses a square-root ADV specification on tradable instruments.
The existing bounded concave function remains a labelled sensitivity analysis,
not a second result silently substituted into the headline.

Every cross-market effect must carry a machine-readable path:

```text
shock instrument → affected holding → institution trigger → generated order
→ destination instrument → modelled impact
```

Required negative controls are:

- zero portfolio overlap produces zero cross-asset transmission;
- disabled triggers produce zero forced selling;
- withdrawal of liquidity changes depth but not net sale volume; and
- an S&P-only shock cannot affect Nikkei without a recorded holdings path.

## Metrics and inference

Pairwise agreement is the primary UC-01/UC-02 estimator. For each repetition and
unordered institution pair, the initial grid supplies agreement observations
for every ordered model pair. The shared-dependency contrast for metric `A` is:

```text
delta_shared(A) = mean A(same exact model pairs)
                - mean A(cross-model pairs)
```

Institution pairs, exact models and repetitions receive equal preregistered
weight. Confidence intervals account for the fact that pairwise observations
reuse institution decisions. Heterogeneous cohorts remain useful for
supervisor-facing scenarios and for producing coherent feedback worlds, but
cohort choice does not define the primary initial-stage estimator.

Agreement is reported as components, not hidden behind one headline score:

- action-category agreement;
- direction agreement;
- active-action and seller rates;
- size/magnitude dispersion;
- urgency dispersion; and
- timing by simulation stage/round.

The implemented initial-stage component definitions are explicit:

- action-category agreement is Jaccard overlap of the two action-type sets,
  with two inactive decisions equal to one;
- direction agreement is one only when the typed stance is identical;
- size similarity compares each `(action type, target instrument, size basis)`
  coordinate as `1 - |x-y| / max(x,y)` and averages the union of coordinates;
  distinct size bases are never added together; and
- urgency similarity is `1 - |u1-u2|/5`, using the maximum action urgency and
  zero for an inactive decision.

The reported 95% interval resamples whole repetitions, preserving the reused
decisions inside every repetition. The permutation null independently shuffles
model labels within each institution/repetition block, preserving institution
behavior and balanced model counts while breaking the same-label link. Both
procedures are seeded from the frozen preregistration and record their
resampling counts in the immutable metric artifact.

Each component includes uncertainty across repetitions and the preregistered
within-institution/repetition model-label permutation null. If a composite is
ever shown, its weights must be preregistered and
weight-sensitivity results must appear beside it. The composite is never the
only result.

The feedback-stage outcome is pairwise agreement within each immutable world.
Shared-world trajectories are compared with the balanced heterogeneous worlds;
their different feedback snapshots are endogenous treatment outcomes and are
never described as if stage-two evidence had been held constant.

UC-02 reports every `SHARED-k`, not a single selected shared model. Alongside
the pairwise contrast, the report shows each model's main effects across
institutions: action/category rates, mean signed direction, mean magnitude,
urgency and dispersion. This lets a reader distinguish a general same-model
effect from one model that is uniformly aggressive, cautious or decisive.
Contrasts show raw values, absolute differences, uncertainty and sample counts;
provider family and exact model-version effects are not conflated.

Amplification reporting separates:

- recommended directional flow;
- forced flow caused by a declared trigger;
- hedge and support flow;
- depth reduction caused by liquidity withdrawal;
- executed pressure by round; and
- modelled price impact.

No result is described as actual market impact or actual firm behaviour.

## Supervisor boundary

The supervisor may propose the taxonomy and preregistration plan before human
approval. After deterministic metrics exist, it may rank competing explanations
such as institution effect, model main effect, shared-model effect and shock
effect, with links to the stored estimates it considered.

The supervisor never calculates, rounds, standardises or repairs numerical
responses. Action standardisation is enforced by the typed response schema, not
an agent task. Every published number, interval, contrast and sample count is a
deterministic query over accepted SQLite records.

## SQLite persistence for the valid experiment

SQLite with WAL mode remains the operational database. Schema migrations add
normalised, append-only records for:

- experiment and preregistration versions;
- approvals and audit events;
- evidence items, observations and feedback snapshots;
- institution-profile and holdings versions;
- provider/model specifications;
- randomised initial run cells and attempts;
- assignment worlds, world members and their seven initial decision links;
- common world snapshots and institution-private post-execution snapshots;
- feedback run cells carrying a mandatory `world_id`;
- accepted decisions and typed actions;
- stage transitions and engine orders;
- safeguard replays and mandate reruns;
- metric definitions, estimates and permutation results; and
- release checks and report snapshots.

Large raw provider/evidence payloads may remain content-addressed JSON blobs,
with their SHA-256, media type, size and storage path recorded in SQLite. Tables
store relationships and queryable values; one mutable assessment JSON document
is not the final experiment schema.

Initial run-cell uniqueness is `(assessment, institution, model, replicate,
stage)`. Feedback run-cell uniqueness is `(assessment, world, institution,
stage)`; model is determined by the immutable world membership but is also
denormalised on the attempt for audit. `world_id` is null for initial cells and
mandatory for feedback cells. A database constraint prevents a world member's
initial replicate, assigned model or institution from disagreeing with the
linked initial decision.

No PostgreSQL, OIDC, user accounts or tenancy work begins before the first valid
UC-01/UC-02 run passes the release gates below.

## Release-blocking gates

A result cannot be released unless all of these pass:

- approved preregistration hash matches the executed plan;
- all 7 × 6 × 5 initial cells are complete and balanced;
- all 12 × 5 required worlds contain seven accepted feedback decisions;
- call order was randomised within the initial stage and within each feedback
  execution batch;
- model-visible case-content hashes match across assignments;
- no post-cut-off data reaches an initial/control payload;
- no private institution block reaches a different institution;
- every response passes schema, unit, permission and evidence-ID checks;
- every world links exactly the seven preregistered same-replicate initial
  decisions and has a matching `stage1_action_set_sha256`;
- every feedback decision points to that world's common snapshot and its own
  institution's private snapshot;
- initial decisions remain immutable;
- action streams reconcile using explicit size bases and residual deleveraging,
  without treating liquidity withdrawal as flow;
- hedge orders are routed to their named instruments and use those instruments'
  depth assumptions;
- every cross-market effect has a holdings/trigger path;
- execution safeguards conserve paired intent and obey their policy;
- mandate and execution safeguard effects are not combined;
- displayed metrics recompute from stored typed values; and
- permutation settings and repetition-level results are retained;
- provider-default sampling is recorded and the temperature sensitivity remains
  excluded from the confirmatory estimator.

Validity gates are pass/fail. Discriminating evaluations are reported as model
failure rates using planted false evidence, post-cut-off observations and
out-of-mandate instruments. If every model passes every replicate, the
evaluation is not assumed successful; it is redesigned to discriminate.

## Delivery order

### M0 — stop invalid claims and repair the foundation

- Archive v4 as an execution-pacing scenario.
- Use neutral prompts and remove model-visible condition/model labels.
- Make reference-only evidence genuinely reference-only.
- Add `case_input_sha256` and keep it distinct from provider request identity.
- Add base currency and matched-date ECB GBP/USD derivation.
- Configure and live-check the six-model roster.
- Lock the cohort rule, T1/T2 timestamps, action size bases, residual
  deleveraging rule and provider-default temperature policy.

Gate: identical case-content hashes across assignments, plus an automated
look-ahead test.

### M1 — real institutions, actions and preregistration

- Add versioned institution profiles and holdings from the input material.
- Add the five typed action categories and unit validation.
- Store and approve the machine-readable preregistration hash.
- Migrate from assessment JSON columns to append-only experiment tables in
  SQLite.

Gate: profile-isolation, action-schema and approval-hash tests pass.

### M2 — crossed initial-stage collection

- Generate the full randomised grid.
- Record provider metadata and both input hashes.
- Execute five balanced repetitions per initial cell with retry/idempotency
  rules.
- Render pairwise components, model main effects and their permutation nulls.

Engineering status: implemented. Operational status: awaiting the approved
evidence snapshot and the first complete 210-decision live collection.

Gate: 7 × 6 × 5 accepted initial decisions, equal cell counts and no headline
composite.

### M3 — closed-loop feedback and amplification

- Materialise the six shared and six cyclic heterogeneous assignment worlds for
  every replicate.
- Build each world's footprint/holdings transition and its common/private
  snapshot split.
- Run the complete feedback stage within those immutable worlds.
- Add triggers, forced orders, depth withdrawal and instrument-level impact.
- Retain an open-loop replay for execution safeguards.

Gate: 12 × 5 complete worlds, each with exactly seven feedback decisions, valid
common/private snapshot hashes and a holdings path for every cross-market
effect.

Engineering status: implemented, including the second `6 → 5` transition and
hashed trajectory artifact. Operational status: awaiting the first approved
630-decision collection.

### M4 — safeguards and discriminating evaluations

- Run execution safeguards as paired replays.
- Run mandate safeguards as fresh, labelled decisions.
- Add false-evidence, look-ahead and mandate-boundary evaluations.

Gate: each safeguard family and each failure rate is independently readable.

### M5 — valid worked run

- Complete one approved seven-institution run with five repetitions per
  confirmatory cell/world.
- Verify all release gates and generate the deterministic report.
- Have a supervisor approve the exact report/input hashes.

Only after M5: consider workers, PostgreSQL, authentication, tenancy, licensed
data feeds and retention policy.

## Definition of done

A supervisor can read, without narrative repair:

1. how aligned the seven heterogeneous archetypes were on the matched shock;
2. whether alignment rose at the feedback stage;
3. how each `SHARED-k` result differed from the heterogeneous assignment;
4. which orders were recommendations, hedges/support, or trigger-forced;
5. the holdings path behind every cross-market effect; and
6. each safeguard family's effect without attribution leakage.

The result is complete only when all six are backed by stored, replayable typed
data and the preregistered release checks pass.
