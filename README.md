# COLFI Institution-Agent Stress Lab

The lab evaluates real, provider-backed institution agents against historical
or custom market conditions. It is an evaluation harness: the supervisor,
institution agents, analytical tools, deterministic scorers and human approvals
are separate runtime components.

See [the researched product flow](docs/PRODUCT_FLOW.md) for the architecture,
evidence base and explicit boundaries.

## What happens in a run

1. Select a known event or a custom date window.
2. Select any of seven declared institution profiles, edit its scenario
   holdings if required, and assign a live model to each institution agent.
3. Select the independent supervisor model and run budget.
4. Fetch and approve runtime evidence with timestamps, source URLs and hashes.
5. Ask the supervisor to classify the event; approve or override it.
6. Ask the supervisor to generate the control/stress/safeguard suite and rubric;
   approve or amend it.
7. Build isolated case packs. An institution never receives another
   institution's response.
8. Run each LangGraph institution agent: required deterministic
   portfolio/evidence/constraint tools, then one fresh model-backed decision.
9. Reuse each stress decision for the safeguarded condition. Deterministic code
   applies the per-round cap, creates dated tranches and runs five feedback rounds.
10. Show raw intent, execution, feedback and impact values with their formulas;
    generate a verified deterministic report and require human release approval.

There is no mock provider, response fixture, response cache or hard-coded agent
decision. The event catalog and portfolio catalog are versioned evaluation
inputs. They are never represented as actual institutional data.

## Data sources

- Yahoo Finance delayed chart endpoint for selected index and ETF observations.
- Official Cboe VIX daily-history CSV.
- Official ECB SDMX exchange-rate API.
- Google News RSS restricted to Reuters-domain results.
- For known cases, the linked BIS, FDIC, Bank of England or Federal Reserve
  reference is downloaded and hashed during the run.

Source failures are isolated and retained in diagnostics. A run stops if a
required portfolio market series or known-event reference fails correctness
checks. Optional news-source failures remain visible but non-blocking.

## Provider configuration

Copy `.env.example` to `.env`, add provider keys, and list the exact models
that operators may assign:

~~~dotenv
ADCS_LLM_MODE=live
ADCS_AVAILABLE_MODELS=deepseek:deepseek-v4-flash,deepseek:deepseek-v4-pro,anthropic:claude-haiku-4-5-20251001,anthropic:claude-sonnet-5,xai:grok-4.3,xai:grok-4.6
ADCS_DEFAULT_MODEL_ROTATION=deepseek:deepseek-v4-flash,anthropic:claude-haiku-4-5-20251001,xai:grok-4.3,deepseek:deepseek-v4-pro,anthropic:claude-sonnet-5,xai:grok-4.3,xai:grok-4.6
DEEPSEEK_API_KEY=...
OPENAI_API_KEY=...
CLAUDE_API_KEY=...
XAI_API_KEY=...
ADCS_LIVE_DB=adcs_live.sqlite3
~~~

Supported provider prefixes are `deepseek`, `openai`, `anthropic`, `xai`,
`google`, `mistral`, `groq`, `openrouter`, `together` and `ollama`.
Only entries in `ADCS_AVAILABLE_MODELS` appear in the assignment controls.
`ADCS_DEFAULT_MODEL_ROTATION` may repeat inexpensive models for the default
seven-institution assignment while keeping the selector itself deduplicated.
For compatibility with this deployment, xAI also accepts `GROQ_API_KEY`, but
new installations should use `XAI_API_KEY`; Groq and xAI are different services.
Institution-agent sampling defaults to each exact provider/model default. The
UI's explicit `0.2` option is a separately labelled sensitivity setting.

## Version-5 experiment foundation

The normalized, append-only SQLite experiment path is created beside the
transitional v4 workflow tables. It persists the seven versioned risk profiles,
user-supplied default holdings, exact model registrations, frozen evidence,
hash-addressed approvals, randomized cells, provider attempts, typed decisions,
actions, deterministic metrics and a hash-chained audit log.

The v5 operator sequence is:

1. Inspect `GET /api/v5/profiles`; holdings or transition-assumption changes
   create new immutable profile/preregistration versions through
   `POST /api/v5/profiles` and `POST /api/v5/preregistrations`.
2. Read and approve the exact preregistration through
   `GET /api/v5/preregistration` and
   `POST /api/v5/preregistration/approve`.
3. Freeze evidence with `POST /api/v5/evidence`, inspect it through
   `GET /api/v5/evidence/{snapshot_id}`, and approve that exact snapshot hash.
4. Materialize the 210-cell schedule with `POST /api/v5/initial-runs/plan`.
   This endpoint makes no provider calls.
5. Confirm the returned plan hash and start a bounded live batch through
   `POST /api/v5/initial-runs/{preregistration_id}/execute`.
6. Inspect durable progress with `GET /api/v5/initial-runs/{preregistration_id}`.
7. After all 210 cells are accepted, freeze pairwise results with
   `POST /api/v5/initial-runs/{preregistration_id}/estimates`.
8. Resolve and transition the 60 exact worlds through
   `/api/v5/feedback-worlds/{preregistration_id}/plan` and
   `/api/v5/feedback-worlds/{preregistration_id}/transition`.
9. Build, confirm and execute the 420 isolated feedback cells through the
   `/api/v5/feedback-runs/{preregistration_id}` endpoints.
10. Apply the second transition, freeze the trajectory artifact, and verify the
   audit chain through `GET /api/v5/governance/audit`.

The execution endpoint stores the exact provider request, raw response, hashes,
latency, usage, finish reason and validation outcome for every call. An invalid
response is retained as rejected evidence before a fresh repair call is made.
The preregistered transition uses normalized reference prices plus explicit
system-notional ADV/depth and square-root impact assumptions. These are
operator-editable exercise inputs, not claims about observed market liquidity.

The holdings editor starts with 100/0, 50/50 or 25/25/25/25 weights supplied by
the operator. Selected holdings must total 100% and are stored as run inputs.
No example institution decisions are used by the runtime.

## Run

~~~bash
python -m pip install -r requirements.txt
python -m uvicorn adcs.api:app --host 0.0.0.0 --port 8001
~~~

Open `http://localhost:8001`. OpenAPI documentation is at
`http://localhost:8001/docs`.

## Verify

~~~bash
pytest -q
python -m py_compile adcs/api.py adcs/config.py adcs/live/*.py adcs/llm/client.py
node --check web/app.js
~~~

The original UI-led workflow remains schema v4 and uses `colfi-mvp-v1`; the v5
API now implements the governed crossed initial runner and the complete
`5 → 6 → 5` feedback loop. Paired safeguard worlds and final human report
sign-off are the next milestone, so no v5 safeguard-effect result is claimed
yet.
Existing recommendation-only results are retained for audit integrity but are
shown as archived metrics and cannot be interpreted as impact evidence.
