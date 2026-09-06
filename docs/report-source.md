# Archived research note — superseded by correctness MVP v1

> This 3 September note described the earlier recommendation-only harness. Its
> exclusion of a market-pressure model and its “supervisor report” design are
> superseded by [PRODUCT_FLOW.md](PRODUCT_FLOW.md). It is retained only as a
> dated research record and must not be used to interpret schema-v4 results.

Audience: COLFI hackathon team  
Date: 3 September 2026  
Scope: a two-day, real-provider demonstration of institution-agent behaviour
under historical market stress.  
Exclusions: live trading, actual firm attribution, calibrated market impact,
regulatory decisions.

## Executive answer

The earlier product decision was an evaluation-only harness. MVP v1 retains the
evaluation boundary but adds a transparent deterministic execution, pressure
and feedback scenario model so safeguards can be assessed against identical
agent intent. The model remains uncalibrated and is not a forecast.

## Evidence synthesis

- Bank of England SWES supports common-scenario participant submissions,
  institution-specific response drivers, aggregation and a second comparison
  round. It also warns that collective actions can amplify shocks and that
  outcomes depend on initial positions and intermediation capacity.
- FSB identifies market correlation, common data/models, third-party
  concentration and model governance as plausible AI-related financial
  stability vulnerabilities.
- NIST AI RMF calls for transparent, documented and repeatable TEVV with
  contextual metrics, uncertainty and review.
- Inspect AI separates datasets, agents, tools, scorers and logs; this is a
  suitable conceptual shape for an institution-agent stress harness.
- OpenAI Evals similarly separates data-source schema from testing criteria and
  supports running an evaluation across model configurations.
- LangGraph interrupts/checkpoints support durable human-in-the-loop execution;
  the demo uses database-backed state gates with the same control boundary.
- USWDS recommends that the current step be visually distinct and have an
  explicit heading. W3C guidance requires programmatically exposed status and
  clear, actionable errors.

## Material limitations

- The portfolio mandates are user-supplied sandbox inputs, not actual holdings.
- News aggregation is key-free and may omit historical Reuters results.
- Provider outputs do not establish actual institution behaviour.
- Equal-notional recommendation pressure does not estimate market impact.
- Supervisor-generated rubrics require human approval and are not validated
  regulatory thresholds.

## Claim-to-source ledger

| Claim | Source | Publisher | Date | URL | Access |
| --- | --- | --- | --- | --- | --- |
| System-wide exercises aggregate participant actions and use subsequent rounds to study interactions | SWES final report | Bank of England | 29 Nov 2024 | https://www.bankofengland.co.uk/financial-stability/boe-system-wide-exploratory-scenario-exercise/boe-swes-exercise-final-report | HTML read 3 Sep 2026 |
| AI may amplify market correlations and third-party concentration | Financial Stability Implications of AI | Financial Stability Board | 14 Nov 2024 | https://www.fsb.org/2024/11/fsb-assesses-the-financial-stability-implications-of-artificial-intelligence/ | HTML read 3 Sep 2026 |
| TEVV should be objective, repeatable, contextual, transparent and documented | AI RMF Core — Measure | NIST | current page | https://airc.nist.gov/airmf-resources/airmf/5-sec-core/ | HTML read 3 Sep 2026 |
| Agent evals should separate datasets, agents, tools, scorers and logs | Inspect AI | UK AI Security Institute | current docs | https://inspect.aisi.org.uk/ | HTML read 3 Sep 2026 |
| Evals separate data-source schema and testing criteria | Create eval | OpenAI | current docs | https://developers.openai.com/api/reference/java/resources/evals/methods/create | HTML read 3 Sep 2026 |
| Interrupts persist state and resume after human input | Interrupts | LangChain | current docs | https://langchain-ai.github.io/langgraph/concepts/breakpoints/ | HTML read 3 Sep 2026 |
| Step indicators should emphasise the current step and use explicit headings | Step indicator | U.S. Web Design System | current docs | https://designsystem.digital.gov/components/step-indicator/ | HTML read 3 Sep 2026 |
| Dynamic status and form errors need clear accessible notification | Forms notifications | W3C WAI | current guidance | https://www.w3.org/WAI/tutorials/forms/notifications/ | HTML read 3 Sep 2026 |

## Search closure

Research stopped after primary sources covered the material design claims:
system-wide stress methodology, AI-correlation risk, eval primitives,
human-in-the-loop persistence, TEVV governance, known-event grounding and
workflow UX. Additional generic articles were unlikely to change the
architecture.
