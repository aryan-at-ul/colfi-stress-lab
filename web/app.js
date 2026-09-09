const state = {
  config: null,
  assessment: null,
  poll: null,
  history: null,
  historyQuery: {page: 1, pageSize: 10, eventId: "", status: ""},
  historyRequest: 0,
  disclosures: new Map(),
  assessmentView: "results",
  cacheReplayTimer: null,
};
const app = document.querySelector("#app");
const activeId = document.querySelector("#active-id");

const esc = value => String(value ?? "").replace(
  /[&<>"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[char])
);
const safeExternalUrl = value => {
  try {
    const parsed = new URL(String(value || ""), window.location.origin);
    return ["http:", "https:"].includes(parsed.protocol) ? esc(parsed.href) : "#";
  } catch {
    return "#";
  }
};
const fmt = (value, digits = 2) => value == null ? "—" : Number(value).toFixed(digits);
const percent = value => value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`;
const clock = seconds => new Date(seconds * 1000).toLocaleTimeString([], {
  hour: "2-digit", minute: "2-digit", second: "2-digit"
});

function displaySymbol(value) {
  return String(value ?? "").replace(/^\^/, "");
}

function marketPresentation(item) {
  const assetId = String(item.id || "").startsWith("MKT-")
    ? String(item.id).slice(4).toLowerCase()
    : "";
  const configured = state.config?.instruments?.[assetId];
  const capturedSymbol = displaySymbol(item.symbol);
  const configuredSymbol = displaySymbol(configured?.symbol);
  const sameInstrument = Boolean(
    capturedSymbol && configuredSymbol && capturedSymbol === configuredSymbol
  );
  return {
    label: (sameInstrument ? configured?.label : item.title)
      || configured?.label
      || capturedSymbol,
    symbol: capturedSymbol || configuredSymbol,
  };
}

function disclosureIsOpen(key) {
  if (state.disclosures.has(key)) return state.disclosures.get(key);
  try {
    const saved = sessionStorage.getItem(`colfi-disclosure:${key}`);
    if (saved !== null) return saved === "open";
  } catch {
    // In-memory state still preserves the disclosure during live rerenders.
  }
  return false;
}

function rememberDisclosure(key, isOpen) {
  state.disclosures.set(key, isOpen);
  try {
    sessionStorage.setItem(`colfi-disclosure:${key}`, isOpen ? "open" : "closed");
  } catch {
    // Browser storage can be unavailable; the in-memory fallback is sufficient.
  }
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: {"Content-Type": "application/json"},
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let value;
  try { value = text ? JSON.parse(text) : null; }
  catch { throw new Error(`Server returned invalid JSON (${response.status})`); }
  if (!response.ok) {
    const detail = value?.detail ?? value?.message ?? `${response.status} ${response.statusText}`;
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = response.status;
    error.payload = value;
    throw error;
  }
  return value;
}

function errorBox(error) {
  return `<div class="error" role="alert"><strong>Could not continue</strong>
    <div>${esc(error.message || error)}</div></div>`;
}

function dateValue(offsetDays = 0) {
  const value = new Date();
  value.setUTCDate(value.getUTCDate() + offsetDays);
  return value.toISOString().slice(0, 10);
}

function portfolioLabel(portfolio) {
  return portfolio.map(item => `${Math.round(item.weight * 100)}% ${item.label}`).join(" + ");
}

function selectedPortfolioLabel(holdings) {
  return holdings.map(item => {
    const asset = state.config.portfolio_instruments[item.asset_id];
    const weight = item.weight_pct ?? Number(item.weight || 0) * 100;
    return `${Number(weight).toFixed(0)}% ${asset?.label || item.asset_id}`;
  }).join(" + ");
}

function institutionFacts(item) {
  const portfolio = item.portfolio_state || {};
  const liquidity = item.liquidity_state || {};
  const facts = [
    `System footprint: ${fmt(item.system_footprint_pct, 1)}%`,
    `Current loss: ${fmt(portfolio.current_loss_pct, 1)}% of ${String(portfolio.current_loss_basis || "declared basis").replaceAll("_", " ")}`,
  ];
  if (portfolio.net_equity_exposure_pct != null) facts.push(`Net equity exposure: ${fmt(portfolio.net_equity_exposure_pct, 1)}%`);
  if (portfolio.gross_exposure_pct != null) facts.push(`Gross exposure: ${fmt(portfolio.gross_exposure_pct, 1)}%`);
  if (portfolio.long_only_global_equity_pct != null) facts.push(`Long-only global equity: ${fmt(portfolio.long_only_global_equity_pct, 1)}%`);
  if (portfolio.equity_risk_share_pct != null) facts.push(`Equity risk share: ${fmt(portfolio.equity_risk_share_pct, 1)}% (target ${fmt(portfolio.equity_risk_target_pct, 1)}%)`);
  if (portfolio.client_sell_flow_multiple != null) facts.push(`Client sell flow: ${fmt(portfolio.client_sell_flow_multiple, 1)}× normal`);
  if (liquidity.cash_buffer_pct != null) facts.push(`Cash buffer: ${fmt(liquidity.cash_buffer_pct, 1)}%`);
  if (liquidity.margin_headroom_pct != null) facts.push(`Margin headroom: ${fmt(liquidity.margin_headroom_pct, 1)}%`);
  if (liquidity.capital_headroom_pct != null) facts.push(`Capital headroom: ${fmt(liquidity.capital_headroom_pct, 1)}%`);
  if (liquidity.redemption_requests_pct != null) facts.push(`Redemption requests: ${fmt(liquidity.redemption_requests_pct, 1)}%`);
  if (liquidity.market_depth_pct_of_normal != null) facts.push(`Market depth: ${fmt(liquidity.market_depth_pct_of_normal, 1)}% of normal`);
  return facts;
}

function modelOptions(selected) {
  return state.config.models.map(model =>
    `<option value="${esc(model)}" ${model === selected ? "selected" : ""}>${esc(model)}</option>`
  ).join("");
}

function renderConfigure() {
  const c = state.config;
  activeId.textContent = "";
  const eventOptions = [
    `<option value="custom">Live/custom date window</option>`,
    ...Object.entries(c.known_events).map(([id, item]) =>
      `<option value="${esc(id)}">${esc(item.label)}</option>`
    ),
  ].join("");
  const sources = Object.entries(c.sources).map(([id, item]) => `
    <label class="check-card">
      <input type="checkbox" name="sources" value="${esc(id)}" ${item.default ? "checked" : ""}>
      <span><strong>${esc(item.label)}</strong><small>${esc(item.note)}</small></span>
    </label>`).join("");
  const instruments = Object.entries(c.instruments).map(([id, item]) => `
    <label class="check-card compact">
      <input type="checkbox" name="instruments" value="${esc(id)}" checked>
      <span><strong>${esc(item.label)}</strong><small>${esc(displaySymbol(item.symbol))} · ${esc(item.source)}</small></span>
    </label>`).join("");
  const rotation = c.default_model_rotation?.length ? c.default_model_rotation : c.models;
  const institutions = Object.entries(c.institutions).map(([id, item], index) => {
    const assigned = rotation[index % rotation.length];
    const privateFacts = institutionFacts(item);
    const defaults = new Map(item.portfolio.map(holding => [holding.asset_id, holding.weight * 100]));
    const holdingInputs = Object.entries(c.portfolio_instruments).map(([assetId, asset]) => `
      <label>${esc(asset.label)}
        <input type="number" min="0" max="100" step="0.1"
          name="holding_${esc(id)}_${esc(assetId)}"
          data-holding-institution="${esc(id)}" data-holding-asset="${esc(assetId)}"
          value="${esc(defaults.get(assetId) ?? 0)}">
      </label>`).join("");
    return `<article class="institution-card selected" data-institution-card="${esc(id)}">
      <div class="institution-top">
        <label class="institution-choice">
          <input type="checkbox" name="institution_ids" value="${esc(id)}" checked>
          <span><b>${esc(item.name)}</b><small>${esc(item.profile)}</small></span>
        </label>
        <span class="portfolio-pill" data-portfolio-pill="${esc(id)}">${esc(portfolioLabel(item.portfolio))}</span>
      </div>
      <div class="institution-meta">
        <span>${esc(item.institution_type)}</span><span>${esc(fmt(item.system_footprint_pct, 1))}% sandbox footprint</span>
      </div>
      <label>Assigned institution-agent model
        <select name="model_${esc(id)}">${modelOptions(assigned)}</select>
      </label>
      <details class="holdings-editor"><summary>Edit scenario holdings</summary>
        <p class="form-hint">These are editable defaults, not actual institutional holdings.</p>
        <div class="holding-grid">${holdingInputs}</div>
        <p class="holding-total">Total: <strong data-holding-total="${esc(id)}">100.0%</strong></p>
      </details>
      <details><summary>Mandate and constraints</summary>
        <p><strong>Objective:</strong> ${esc(item.objective)}</p>
        <ul>${item.constraints.map(value => `<li>${esc(value)}</li>`).join("")}</ul>
        <p><strong>Private scenario state</strong></p>
        <ul>${privateFacts.map(value => `<li>${esc(value)}</li>`).join("")}</ul>
        <p><strong>Active trigger</strong></p>
        <ul>${item.risk_triggers.map(value => `<li>${esc(value.description)}</li>`).join("")}</ul>
      </details>
    </article>`;
  }).join("");

  app.innerHTML = `
    <section class="product-intro">
      <div><span class="eyebrow">NEW ASSESSMENT</span>
        <h2>Configure an institution-agent stress test</h2>
        <p>Select the event window, evidence sources, institutions, and assigned models.
        The workflow collects the evidence, generates the test suite, and runs each
        approved institution agent against its portfolio.</p>
      </div>
      <div class="truth-stack">
        <span><i></i> Runtime market evidence</span>
        <span><i></i> Fresh model calls</span>
        <span><i></i> Human approval gates</span>
      </div>
    </section>
    <form id="create-form">
      <div id="builder-error" class="form-error-slot" aria-live="assertive"></div>
      <div class="builder-layout">
        <div class="builder-main">
          <section class="panel numbered">
            <span class="section-number">1</span>
            <div class="section-copy"><span class="eyebrow">EVENT UNDER TEST</span>
              <h2>Choose a known event or investigate a date</h2>
              <p>Known events are repeatable classification test cases. Their market
              observations and official source are still fetched at runtime.</p></div>
            <label class="wide">Market event
              <select id="event-select" name="event_id">${eventOptions}</select>
            </label>
            <div id="event-context" class="event-context">
              <strong>Live/custom window</strong><span>No expected label; the supervisor classifies only the fetched evidence.</span>
            </div>
            <div class="grid three">
              <label>Start date<input id="start-date" type="date" name="start_date" value="${dateValue(-3)}" max="${dateValue(-1)}" required></label>
              <label>End date<input id="end-date" type="date" name="end_date" value="${dateValue(-1)}" max="${dateValue(-1)}" required></label>
              <label>Operator<input name="created_by" value="Noha" required maxlength="100"></label>
            </div>
            <label>Reuters search query
              <input id="news-query" name="news_query" value="financial markets OR bank funding OR currency volatility" maxlength="240" required>
            </label>
            <details class="advanced"><summary>Evidence connectors and market series</summary>
              <h3>Connectors</h3><div class="choice-grid">${sources}</div>
              <h3>Market series</h3><div class="choice-grid">${instruments}</div>
              <p class="form-hint">Portfolio series required by selected institutions are added automatically and recorded in the run.</p>
            </details>
          </section>

          <section class="panel numbered">
            <span class="section-number">2</span>
            <div class="section-copy"><span class="eyebrow">SYSTEMS UNDER TEST</span>
              <h2>Select institutions and assign their agents</h2>
              <p>The profile values come from the COLFI input material and the portfolios
              are user-editable scenario defaults. Neither is a claim about a real firm.</p></div>
            <div class="selection-actions"><button type="button" id="select-all">Select all</button>
              <button type="button" id="clear-all">Clear all</button></div>
            <div class="institution-grid">${institutions}</div>
          </section>

          <section class="panel numbered">
            <span class="section-number">3</span>
            <div class="section-copy"><span class="eyebrow">SUPERVISION & RUN CONTROL</span>
              <h2>Set the independent supervisor and run budget</h2>
              <p>The supervisor classifies and designs the test. In the fast demo,
              each assigned AI flags stress only; approved code determines every action and size.</p></div>
            <label>Execution contract<select name="execution_mode">
              <option value="deterministic_stress_rules" selected>Deterministic safeguard assessment (recommended)</option>
              <option value="llm_decision">Existing LLM decision assessment</option>
            </select><small>The recommended demo keeps AI stress detection, then compares a fixed 20% sale with a fixed 10% safeguarded sale.</small></label>
            <div class="grid three">
              <label>Supervisor model<select name="supervisor_model">${modelOptions(c.default_supervisor_model || c.models[0])}</select></label>
              <label>Repetitions per agent<input type="number" name="samples_per_agent" min="1" max="5" value="1" required></label>
              <label>Agent sampling<select name="temperature">
                <option value="" selected>Provider default (confirmatory)</option>
                <option value="0.2">Temperature 0.2 (sensitivity)</option>
              </select></label>
            </div>
            <label class="switch-row"><input id="include-safeguard" type="checkbox" name="include_safeguard" checked>
              <span><strong>Include paired safeguarded execution</strong><small>The same stress flag is reused; code replaces the 20% sale with 10% and cancels the blocked 10%.</small></span>
            </label>
            <label class="switch-row cache-switch"><input id="use-cached" type="checkbox" name="use_cached">
              <span><strong>Use cached demo replay when available (<code>use_cached</code>)</strong><small>An exactly matching released assessment replays its verified 14-step log in seconds. If none exists, this runs normally and becomes available after release.</small></span>
            </label>
            <label>Optional safeguard objective
              <textarea name="safeguard_goal" rows="2" maxlength="1000" placeholder="Example: avoid rapid forced selling while allowing proportionate hedging."></textarea>
            </label>
          </section>
        </div>

        <aside class="run-summary">
          <span class="eyebrow">RUN PREVIEW</span><h2>What will happen</h2>
          <ol>
            <li>Collect and approve evidence</li>
            <li>Classify the market event</li>
            <li>Generate and approve test suite</li>
            <li>Run isolated institution agents</li>
            <li>Score, compare and release</li>
          </ol>
          <div class="estimate">
            <span><small>Institutions</small><strong id="estimate-institutions">7</strong></span>
            <span><small>Agent runs</small><strong id="estimate-runs">21</strong></span>
            <span><small>LLM calls</small><strong id="estimate-calls">16</strong></span>
          </div>
          <p class="estimate-note">Planned calls before any provider validation retries.</p>
          <button class="primary launch" type="submit">Start evidence collection <span>→</span></button>
          <p class="fine-print">No trades are executed. Failed calls remain visible and are never replaced with fixture responses.</p>
        </aside>
      </div>
    </form>`;

  document.querySelector("#create-form").addEventListener("submit", createAssessment);
  document.querySelector("#event-select").addEventListener("change", applyPreset);
  document.querySelector("#include-safeguard").addEventListener("change", updateEstimate);
  document.querySelector("#use-cached").addEventListener("change", updateEstimate);
  document.querySelectorAll('[name="institution_ids"], [name="samples_per_agent"]').forEach(
    input => input.addEventListener("change", updateEstimate)
  );
  document.querySelector("#select-all").addEventListener("click", () => toggleInstitutions(true));
  document.querySelector("#clear-all").addEventListener("click", () => toggleInstitutions(false));
  document.querySelectorAll("[data-institution-card] input[type=checkbox]").forEach(input => {
    input.addEventListener("change", event => {
      event.currentTarget.closest(".institution-card").classList.toggle("selected", event.currentTarget.checked);
    });
  });
  document.querySelectorAll("[data-holding-institution]").forEach(input => {
    input.addEventListener("input", event => updateHoldingTotal(event.currentTarget.dataset.holdingInstitution));
  });
  updateEstimate();
}

function updateHoldingTotal(institutionId) {
  const values = [...document.querySelectorAll(`[data-holding-institution="${institutionId}"]`)]
    .map(input => Number(input.value || 0));
  const total = values.reduce((sum, value) => sum + value, 0);
  const target = document.querySelector(`[data-holding-total="${institutionId}"]`);
  if (!target) return;
  target.textContent = `${total.toFixed(1)}%`;
  target.classList.toggle("invalid", Math.abs(total - 100) > 0.001);
}

function toggleInstitutions(checked) {
  document.querySelectorAll('[name="institution_ids"]').forEach(input => {
    input.checked = checked;
    input.closest(".institution-card").classList.toggle("selected", checked);
  });
  updateEstimate();
}

function updateEstimate() {
  const selected = document.querySelectorAll('[name="institution_ids"]:checked').length;
  const samples = Number(document.querySelector('[name="samples_per_agent"]')?.value || 1);
  const cases = document.querySelector("#include-safeguard")?.checked ? 3 : 2;
  const replayRequested = document.querySelector("#use-cached")?.checked;
  document.querySelector("#estimate-institutions").textContent = selected;
  document.querySelector("#estimate-runs").textContent = selected * samples * cases;
  document.querySelector("#estimate-calls").textContent = replayRequested
    ? "0*"
    : 2 + selected * samples * 2;
  const note = document.querySelector(".estimate-note");
  if (note) {
    note.textContent = replayRequested
      ? "0 new calls on an exact cache hit; otherwise the normal live call plan applies."
      : "Planned calls before any provider validation retries.";
  }
}

function showBuilderError(message) {
  const target = document.querySelector("#builder-error");
  if (!target) return;
  target.innerHTML = errorBox(message);
  target.scrollIntoView({behavior: "smooth", block: "center"});
}

function applyPreset(event) {
  const id = event.currentTarget.value;
  const preset = state.config.known_events[id];
  const context = document.querySelector("#event-context");
  if (!preset) {
    context.innerHTML = `<strong>Live/custom window</strong><span>No expected label; the supervisor classifies only the fetched evidence.</span>`;
    document.querySelectorAll('[name="instruments"]').forEach(input => { input.checked = true; });
    applyPortfolioPreset(null);
    return;
  }
  document.querySelector("#start-date").value = preset.start_date;
  document.querySelector("#end-date").value = preset.end_date;
  document.querySelector("#news-query").value = preset.news_query;
  context.innerHTML = `<strong>${esc(preset.description)}</strong>
    <span>Expected taxonomy for classification check: <b>${esc(preset.expected_taxonomy)}</b></span>
    <a href="${safeExternalUrl(preset.reference.url)}" target="_blank" rel="noopener">${esc(preset.reference.publisher)} reference ↗</a>`;
  const selected = new Set(preset.instruments);
  document.querySelectorAll('[name="instruments"]').forEach(input => {
    input.checked = selected.has(input.value);
  });
  applyPortfolioPreset(preset);
}

function applyPortfolioPreset(preset) {
  const overrides = preset?.portfolio_overrides || {};
  Object.entries(state.config.institutions).forEach(([institutionId, profile]) => {
    const holdings = overrides[institutionId] || profile.portfolio.map(item => ({
      asset_id: item.asset_id,
      weight_pct: Number(item.weight) * 100,
    }));
    const weights = new Map(holdings.map(item => [item.asset_id, item.weight_pct]));
    document.querySelectorAll(`[data-holding-institution="${institutionId}"]`).forEach(input => {
      input.value = weights.get(input.dataset.holdingAsset) || 0;
    });
    updateHoldingTotal(institutionId);
    const pill = document.querySelector(`[data-portfolio-pill="${institutionId}"]`);
    if (pill) pill.textContent = selectedPortfolioLabel(holdings);
  });
}

async function createAssessment(event) {
  event.preventDefault();
  const formElement = event.currentTarget;
  const errorTarget = formElement.querySelector("#builder-error");
  if (errorTarget) errorTarget.innerHTML = "";
  const form = new FormData(formElement);
  const institutionIds = form.getAll("institution_ids");
  if (!institutionIds.length) {
    showBuilderError("Select at least one institution to stress-test.");
    return;
  }
  if (
    form.get("execution_mode") === "deterministic_stress_rules"
    && institutionIds.length !== Object.keys(state.config.institutions).length
  ) {
    showBuilderError(
      "The deterministic 20% versus 10% assessment requires all seven institutions."
    );
    return;
  }
  if (form.get("execution_mode") === "deterministic_stress_rules") {
    if (!form.has("include_safeguard")) {
      showBuilderError(
        "The deterministic result requires the paired 10% safeguarded run."
      );
      return;
    }
  }
  const startDate = String(form.get("start_date") || "");
  const endDate = String(form.get("end_date") || "");
  const windowDays = (
    Date.parse(`${endDate}T00:00:00Z`) - Date.parse(`${startDate}T00:00:00Z`)
  ) / 86_400_000;
  if (!Number.isFinite(windowDays) || windowDays < 0 || windowDays > 31) {
    showBuilderError(
      windowDays < 0
        ? "End date must be on or after the start date."
        : "The evidence window must be 31 days or fewer."
    );
    return;
  }
  if (!form.getAll("sources").length || !form.getAll("instruments").length) {
    showBuilderError("Select at least one evidence connector and one market series.");
    return;
  }
  const selectedEvent = form.get("event_id");
  const preset = state.config.known_events[selectedEvent];
  const holdingsByInstitution = {};
  for (const institutionId of institutionIds) {
    const holdings = Object.keys(state.config.portfolio_instruments).map(assetId => ({
      asset_id: assetId,
      weight_pct: Number(form.get(`holding_${institutionId}_${assetId}`) || 0),
    })).filter(item => item.weight_pct > 0);
    const total = holdings.reduce((sum, item) => sum + item.weight_pct, 0);
    if (Math.abs(total - 100) > 0.001) {
      showBuilderError(
        `${institutionId} portfolio totals ${total.toFixed(1)}%; holdings must total 100%.`
      );
      return;
    }
    holdingsByInstitution[institutionId] = holdings;
  }
  const body = {
    event_id: selectedEvent,
    expected_taxonomy: preset?.expected_taxonomy ?? null,
    start_date: form.get("start_date"),
    end_date: form.get("end_date"),
    created_by: form.get("created_by"),
    news_query: form.get("news_query"),
    sources: form.getAll("sources"),
    instruments: form.getAll("instruments"),
    supervisor_model: form.get("supervisor_model"),
    institutions: institutionIds.map(id => ({
      institution_id: id,
      model: form.get(`model_${id}`),
      holdings: holdingsByInstitution[id],
    })),
    samples_per_agent: Number(form.get("samples_per_agent")),
    temperature: form.get("temperature") === "" ? null : Number(form.get("temperature")),
    include_safeguard: form.has("include_safeguard"),
    safeguard_goal: form.get("safeguard_goal"),
    execution_mode: form.get("execution_mode"),
    use_cached: form.has("use_cached"),
  };
  const replayRequested = form.has("use_cached");
  app.innerHTML = `<div class="loading" role="status"><span class="spinner"></span>
    <strong>${replayRequested ? "Looking for an exact released assessment" : "Creating the assessment"}</strong>
    <small>${replayRequested ? "A cache miss will continue with the normal live workflow…" : "Starting runtime evidence connectors…"}</small></div>`;
  try {
    state.assessment = await api("/assessments", {method: "POST", body});
    state.assessmentView = "results";
    history.replaceState(
      null, "", `?assessment=${encodeURIComponent(state.assessment.id)}`
    );
    activeId.textContent = state.assessment.id;
    if (state.assessment.cache_replay?.playback) {
      startCacheReplay();
    } else {
      renderAssessment();
      startPolling();
    }
  } catch (error) {
    app.innerHTML = errorBox(error) + `<button id="back-to-builder">Return to builder</button>`;
    document.querySelector("#back-to-builder").addEventListener("click", renderConfigure);
  }
}

const PHASES = [
  {id: "setup", label: "Scope", steps: ["configure"]},
  {id: "evidence", label: "Evidence", steps: ["collect", "evidence_review"]},
  {id: "design", label: "Test design", steps: ["classify", "event_review", "suite", "suite_review"]},
  {id: "execution", label: "Agent runs", steps: ["package_cases", "run_control", "run_stress", "run_safeguarded"]},
  {id: "findings", label: "Findings", steps: ["score", "synthesise", "release_review"]},
];

function phaseRail(a) {
  const stepMap = new Map(a.workflow.map(item => [item.id, item.status]));
  const phases = PHASES.map(phase => {
    const statuses = phase.steps.map(step => stepMap.get(step) || "pending");
    let status = "pending";
    if (statuses.every(value => ["complete", "skipped"].includes(value))) status = "complete";
    else if (statuses.some(value => ["running", "waiting", "failed", "rejected"].includes(value))) {
      status = statuses.find(value => ["failed", "rejected"].includes(value)) || "active";
    }
    return {...phase, status};
  });
  return `<nav class="phase-rail" aria-label="Assessment phases"><ol>${phases.map((phase, index) => `
    <li class="${esc(phase.status)}" ${phase.status === "active" ? 'aria-current="step"' : ""}>
      <span>${phase.status === "complete" ? "✓" : index + 1}</span><b>${esc(phase.label)}</b>
    </li>`).join("")}</ol></nav>`;
}

function workflow(a) {
  const disclosureKey = `${a.id}:workflow`;
  const open = disclosureIsOpen(disclosureKey) ? " open" : "";
  return `<details class="panel workflow-panel" data-disclosure-key="${esc(disclosureKey)}"${open}><summary>
    <span><b>Detailed execution log</b><small>All ${a.workflow.length} workflow steps</small></span>
    <span class="summary-actions"><span class="status ${esc(a.status)}">${esc(a.status.replaceAll("_", " "))}</span>
      <span class="disclosure-chevron" aria-hidden="true">›</span></span>
  </summary><div class="workflow-list">${a.workflow.map((step, index) => `
    <div class="workflow-step ${esc(step.status)}">
      <span class="node">${step.status === "complete" ? "✓" : String(index + 1).padStart(2, "0")}</span>
      <div><strong>${esc(step.label)}</strong><small>${esc(step.message || step.status)}</small></div>
      <span class="state">${esc(step.status)}</span>
    </div>`).join("")}</div></details>`;
}

function cacheReplayNotice(a, active = false) {
  if (!a.cache_replay?.hit) return "";
  const sourceId = a.cache_replay.source_assessment_id || a.id;
  return `<section class="cache-replay-notice" role="status">
    <div><span class="eyebrow">CACHED REPLAY</span>
      <strong>${active ? "Replaying the verified execution log" : "Verified assessment replay"}</strong>
      <small>No evidence or model provider calls are being made. The saved result and approvals remain attributed to assessment ${esc(sourceId)}.</small>
    </div>${active ? '<button id="skip-cache-replay" type="button">Skip to results →</button>' : ""}
  </section>`;
}

const CACHE_REPLAY_APPROVAL_STEPS = new Set([
  "evidence_review", "event_review", "suite_review", "release_review",
]);

function cacheReplayApproval(a, gateId) {
  if (!CACHE_REPLAY_APPROVAL_STEPS.has(gateId)) return "";
  const approval = a.approvals.slice().reverse().find(item =>
    item.gate === gateId && item.approved
  );
  const approvedAt = approval
    ? new Date(Number(approval.at) * 1000).toLocaleString()
    : "Original approval timestamp unavailable";
  const approver = approval?.actor || "Original approver unavailable";
  const note = approval?.note || "No audit note was recorded.";
  const evidenceCount = a.evidence?.items?.length || 0;
  const caseCount = a.plan?.cases?.length || 0;
  const cap = a.plan?.safeguard?.max_daily_portfolio_sell_pct
    ?? a.plan?.safeguard?.max_single_asset_sell_pct;
  const content = {
    evidence_review: {
      title: "Evidence is ready for replay review",
      description: `${evidenceCount} saved evidence items passed the original correctness checks.`,
      fields: `<div class="grid two">
        <label>Evidence items<input value="${esc(evidenceCount)} captured items" readonly></label>
        <label>Verification<input value="${a.evidence?.verification?.passed ? "Passed" : "Unavailable"}" readonly></label>
      </div>`,
    },
    event_review: {
      title: "Replay the supervisor’s event approval",
      description: "Review the saved supervisor classification before continuing the replay.",
      fields: `<div class="grid two">
        <label>Approved taxonomy<input value="${esc(a.classification?.event_type || "Unavailable")}" readonly></label>
        <label>Approved event label<input value="${esc(a.classification?.event_label || "Unavailable")}" readonly></label>
      </div><label>Supervisor rationale<textarea rows="3" readonly>${esc(a.classification?.rationale || "Unavailable")}</textarea></label>`,
    },
    suite_review: {
      title: "Replay the supervisor’s test-suite approval",
      description: `Review the saved ${caseCount}-case suite and safeguard input before continuing.`,
      fields: `<label>Assessment objective<textarea rows="3" readonly>${esc(a.plan?.objective || "Unavailable")}</textarea></label>
        <div class="grid two">
          <label>Assessment cases<input value="${esc(caseCount)} saved cases" readonly></label>
          <label>Safeguarded sale cap<input value="${cap == null ? "Unavailable" : `${esc(cap)}%`}" readonly></label>
        </div><label>Safeguard instruction<textarea rows="3" readonly>${esc(a.plan?.safeguard?.instruction || "Unavailable")}</textarea></label>`,
    },
    release_review: {
      title: "Replay the final release approval",
      description: "Review the saved verified report before opening the released result.",
      fields: `<label>Report title<input value="${esc(a.report?.title || "Verified assessment report")}" readonly></label>
        <label>Executive summary<textarea rows="3" readonly>${esc(a.report?.executive_summary || "Verified result retained from the original assessment.")}</textarea></label>`,
    },
  }[gateId];
  return `<section class="checkpoint cache-replay-gate" aria-labelledby="cache-replay-gate-title">
    <div class="checkpoint-icon">!</div><div class="checkpoint-body">
      <span class="eyebrow">REPLAYED HUMAN CHECKPOINT</span>
      <h2 id="cache-replay-gate-title">${esc(content.title)}</h2>
      <p>${esc(content.description)}</p>${content.fields}
      <div class="grid two replay-signoff">
        <label>Original approver<input value="${esc(approver)}" readonly></label>
        <label>Original approval time<input value="${esc(approvedAt)}" readonly></label>
      </div>
      <label>Original audit note<input value="${esc(note)}" readonly></label>
      <div class="button-row"><small>This button advances playback only; it does not create or alter an approval.</small>
        <button id="replay-approval" class="primary" type="button">Replay approval and continue →</button></div>
    </div></section>`;
}

function stopCacheReplay() {
  if (state.cacheReplayTimer !== null) {
    clearTimeout(state.cacheReplayTimer);
    state.cacheReplayTimer = null;
  }
}

function finishCacheReplay() {
  stopCacheReplay();
  if (state.assessment?.cache_replay) {
    state.assessment.cache_replay.playback = false;
  }
  state.assessmentView = "results";
  renderAssessment();
}

function startCacheReplay() {
  stopCacheReplay();
  const assessment = state.assessment;
  const savedWorkflow = assessment.workflow.map(step => ({...step}));
  const intervalMs = 550;
  let completed = 0;
  state.disclosures.set(`${assessment.id}:workflow`, true);

  const renderFrame = () => {
    if (state.assessment !== assessment) return;
    state.cacheReplayTimer = null;
    const currentStep = savedWorkflow[completed];
    const awaitingReplayApproval = CACHE_REPLAY_APPROVAL_STEPS.has(
      currentStep?.id
    );
    const replayWorkflow = savedWorkflow.map((step, index) => {
      if (index < completed) return {
        ...step,
        status: step.status === "skipped" ? "skipped" : "complete",
      };
      if (index === completed && completed < savedWorkflow.length) return {
        ...step,
        status: step.status === "skipped"
          ? "skipped"
          : awaitingReplayApproval ? "waiting" : "running",
        message: step.status === "skipped"
          ? step.message
          : awaitingReplayApproval
            ? "Cached replay · waiting for presenter approval"
            : `Cached replay · ${step.message || "restoring verified output"}`,
      };
      return {...step, status: "pending", message: "Waiting for cached replay"};
    });
    const replayView = {
      ...assessment,
      status: awaitingReplayApproval ? "waiting" : "running",
      workflow: replayWorkflow,
    };
    activeId.textContent = assessment.id;
    app.innerHTML = `${cacheReplayNotice(assessment, true)}
      <section class="assessment-head"><div><span class="eyebrow">ASSESSMENT ${esc(assessment.id)}</span>
        <h2>${esc(assessment.classification?.event_label || "Cached assessment replay")}</h2>
        <p role="status" aria-live="polite">${awaitingReplayApproval ? "Presenter approval required" : `Replaying step ${Math.min(completed + 1, savedWorkflow.length)} of ${savedWorkflow.length}`}</p></div>
        <span class="status ${awaitingReplayApproval ? "waiting" : "running"}">${awaitingReplayApproval ? "awaiting replay approval" : "cached replay"}</span></section>
      ${phaseRail(replayView)}
      ${cacheReplayApproval(assessment, currentStep?.id)}
      ${workflow(replayView)}`;
    document.querySelector("#skip-cache-replay")?.addEventListener("click", finishCacheReplay);

    if (completed < savedWorkflow.length) {
      if (awaitingReplayApproval) {
        document.querySelector("#replay-approval")?.addEventListener("click", () => {
          completed += 1;
          renderFrame();
        });
      } else {
        completed += 1;
        state.cacheReplayTimer = setTimeout(renderFrame, intervalMs);
      }
    } else {
      state.cacheReplayTimer = setTimeout(finishCacheReplay, 700);
    }
  };
  renderFrame();
}

function retryPanel(a) {
  if (a.status !== "failed" || !a.error) return "";
  return `<section class="retry-panel" role="alert">
    <div><span class="eyebrow">FAILED STEP</span><strong>${esc(a.error)}</strong>
      <small>Completed results stay saved. Only unresolved work is rerun.</small></div>
    <button id="retry-step" class="primary" type="button">Retry failed step</button>
  </section>`;
}

function gate(a) {
  const gates = {
    awaiting_evidence_approval: ["evidence_review", "Evidence is ready for review",
      "Check provenance and coverage. Approval allows the supervisor to see this evidence."],
    awaiting_event_approval: ["event_review", "Approve the supervisor’s event classification",
      "For a known event, compare the proposal with the expected taxonomy label before continuing."],
    awaiting_suite_approval: ["suite_review", "Approve the generated stress-test suite",
      "Review the objective, conditions, rubric and safeguard before any institution agent is called."],
    awaiting_release_approval: ["release_review", "Release the completed assessment",
      "The report and scores remain a draft until a named human approves them."],
  };
  const item = gates[a.status];
  if (!item) return "";
  const [gateId, title, note] = item;
  let fields = "";
  if (gateId === "event_review") {
    fields = `<div class="classification-compare">${classificationCheck(a, true)}</div>
      <div class="grid two">
        <label>Approved taxonomy<select name="event_type">${state.config.taxonomy.map(value =>
          `<option ${value === a.classification.event_type ? "selected" : ""}>${esc(value)}</option>`
        ).join("")}</select></label>
        <label>Approved event label<input name="event_label" value="${esc(a.classification.event_label)}"></label>
      </div>`;
  }
  if (gateId === "suite_review") {
    const deterministic = a.plan.execution_mode === "deterministic_stress_rules";
    const ruleFields = deterministic ? `<div class="rule-approval-grid">${(a.plan.deterministic_rules || []).map(rule => `
      <label>${esc(rule.institution_name)} sell target (% of starting portfolio)
        <input name="sell_target_${esc(rule.institution_id)}" type="number" value="${esc(rule.target_sell_portfolio_pct)}" readonly>
        <small>Fixed contract: ${esc(rule.description)}</small>
      </label>`).join("")}</div>` : "";
    fields = `<label>Assessment objective
        <textarea name="suite_objective" rows="3">${esc(a.plan.objective)}</textarea></label>
      ${a.request.include_safeguard ? `<div class="grid two">
        <label>Safeguard instruction<textarea name="safeguard_instruction" rows="3" ${deterministic ? "readonly" : ""}>${esc(a.plan.safeguard.instruction)}</textarea></label>
        <label>${deterministic ? "Safeguarded sale (% of starting portfolio)" : "Maximum sale per held asset per execution round (%)"}
          <input name="${deterministic ? "max_daily_portfolio_sell_pct" : "max_single_asset_sell_pct"}" type="number" min="0" max="100" step="0.1" value="${esc(deterministic ? a.plan.safeguard.max_daily_portfolio_sell_pct : a.plan.safeguard.max_single_asset_sell_pct)}" ${deterministic ? "readonly" : ""}>
          <small>${deterministic ? "Fixed at 10%. The blocked half of the 20% sale is cancelled, not delayed." : "Each tranche must obey this cap; total intent is conserved when it fits within the model horizon."}</small>
        </label></div>` : ""}`;
    fields += ruleFields;
  }
  return `<section class="checkpoint" aria-labelledby="checkpoint-title">
    <div class="checkpoint-icon">!</div><div class="checkpoint-body">
      <span class="eyebrow">HUMAN CHECKPOINT</span><h2 id="checkpoint-title">${esc(title)}</h2>
      <p>${esc(note)}</p>
      <form id="approval-form" data-gate="${esc(gateId)}">${fields}<div class="grid two">
        <label>Approver<input name="actor" value="${esc(a.request.created_by)}" required></label>
        <label>Audit note<input name="note" placeholder="What did you check or change?"></label>
      </div><div class="button-row"><button type="button" class="danger" id="reject">Reject assessment</button>
        <button class="primary" type="submit">Approve and continue →</button></div>
      </form>
    </div></section>`;
}

function scopeSection(a) {
  const assignments = a.request.institutions || [];
  return `<section class="panel"><div class="section-head"><div><span class="eyebrow">ASSESSMENT SCOPE</span>
    <h2>${assignments.length} institution agent${assignments.length === 1 ? "" : "s"} under test</h2></div>
    <span class="event-badge">${esc(a.request.event_id === "custom" ? "Custom event" : state.config.known_events[a.request.event_id]?.label || a.request.event_id)}</span></div>
    <div class="scope-grid">${assignments.map(assignment => {
      const item = state.config.institutions[assignment.institution_id];
      const holdings = assignment.holdings?.length
        ? assignment.holdings
        : item.portfolio;
      return `<article><span class="avatar">${esc(item.name.split(" ").at(-1))}</span>
        <div><strong>${esc(item.name)}</strong><small>${esc(selectedPortfolioLabel(holdings))}</small>
        <span class="model-chip">${esc(assignment.model)}</span></div></article>`;
    }).join("")}</div>
    <div class="scope-footer"><span>Supervisor <b class="mono">${esc(a.request.supervisor_model)}</b></span>
      <span>${esc(a.request.start_date)} → ${esc(a.request.end_date)}</span>
      <span>${esc(a.request.samples_per_agent)} repetition(s)</span></div></section>`;
}

function evidenceSection(a) {
  if (!a.evidence) return "";
  const legacyWarning = a.schema_version < 4
    ? `<div class="error"><strong>Legacy evidence contract</strong><div>This snapshot may contain only the final in-window observation. It is retained for audit but must not be used for a new conclusion; rerun under schema v4.</div></div>`
    : "";
  const market = a.evidence.items.filter(item => item.kind === "market");
  const references = a.evidence.items.filter(item => item.kind !== "market");
  const marketCards = market.map(item => {
    const presentation = marketPresentation(item);
    const observations = item.observations || [];
    const pathRows = observations.map((point, index) => `<tr>
      <td>${esc(point.session_date)}</td><td>${fmt(point.value, 4)}</td>
      <td>${index === 0 ? "reference" : `${Number(point.daily_change_pct) >= 0 ? "+" : ""}${fmt(point.daily_change_pct, 3)}%`}</td>
      <td>${point.volume == null ? "—" : fmt(point.volume, 0)}</td></tr>`).join("");
    return `<article class="market-card">
    <div><span class="symbol">${esc(presentation.label)}</span><small>${esc(presentation.symbol)} · ${esc(item.source)}</small></div>
    <strong>${fmt(item.value, 4)}</strong>
    <span class="change ${Number(item.change_pct) < 0 ? "down" : "up"}">${Number(item.change_pct) >= 0 ? "+" : ""}${fmt(item.change_pct)}%</span>
    <small>${a.schema_version >= 4 ? `window return · ${esc(item.reference_date)} → ${esc(item.window_end_date)}` : "legacy final-session move"}</small>
    <div class="source-links"><a href="/api/assessments/${encodeURIComponent(a.id)}/evidence/${encodeURIComponent(item.id)}" target="_blank" rel="noopener">Captured JSON ↗</a>
      ${a.schema_version >= 4 ? `<a href="${safeExternalUrl(item.source_url)}" target="_blank" rel="noopener">Dated source ↗</a>` : ""}</div>
    <details class="path-details"><summary>Inspect ${observations.length} dated observations</summary>
      <div class="table-wrap"><table><thead><tr><th>Session</th><th>Value</th><th>Daily move</th><th>Volume</th></tr></thead>
      <tbody>${pathRows}</tbody></table></div><small>${esc(item.summary)}</small></details>
  </article>`}).join("");
  const referenceRows = references.map(item => `<tr>
    <td><span class="tag">${esc(item.kind.replace("_", " "))}</span></td>
    <td><strong>${esc(item.title)}</strong><small>${esc(item.source)}</small></td>
    <td>${esc(item.observed_at)}</td>
    <td><a href="${safeExternalUrl(item.source_url)}" target="_blank" rel="noopener">Open ↗</a>
      <small class="mono">SHA ${esc(item.raw_sha256.slice(0, 12))}</small></td></tr>`).join("");
  return `<section class="panel"><div class="section-head"><div><span class="eyebrow">RUNTIME EVIDENCE</span>
    <h2>${a.evidence.items.length} captured items</h2></div><span class="status ${a.evidence.verification?.passed ? "complete" : "failed"}">${a.evidence.verification?.passed ? "dated paths verified" : "unverified"}</span></div>
    ${legacyWarning}
    <p>Returns cover the complete selected path from the displayed pre-window reference. Open Captured JSON to inspect exactly what the agents received.</p>
    <div class="market-grid">${marketCards || '<p class="empty">No market observations captured.</p>'}</div>
    ${references.length ? `<details class="data-details" ${a.status === "awaiting_evidence_approval" ? "open" : ""}>
      <summary>News and official references (${references.length})</summary><div class="table-wrap"><table>
      <thead><tr><th>Type</th><th>Source item</th><th>Observed</th><th>Provenance</th></tr></thead>
      <tbody>${referenceRows}</tbody></table></div></details>` : ""}
    ${a.evidence.failures?.length ? `<details class="diagnostics"><summary>${a.evidence.failures.length} non-blocking connector diagnostic(s)</summary>
      ${a.evidence.failures.map(item => `<p><b>${esc(item.source)}</b> — ${esc(item.error)}</p>`).join("")}</details>` : ""}
  </section>`;
}

function classificationCheck(a, compact = false) {
  const expected = a.request.expected_taxonomy;
  const observed = a.classification?.human_override?.original?.event_type || a.classification?.event_type;
  if (!expected) return `<div class="comparison neutral"><span>Custom event</span><strong>No reference label</strong></div>`;
  const matched = observed === expected;
  return `<div class="comparison ${matched ? "match" : "mismatch"}">
    <span>${matched ? "✓ Classification matched" : "! Classification mismatch"}</span>
    <div><small>Expected</small><strong>${esc(expected)}</strong></div>
    <div><small>Supervisor proposed</small><strong>${esc(observed)}</strong></div>
  </div>`;
}

function classificationSection(a) {
  if (!a.classification) return "";
  const item = a.classification;
  return `<section class="panel classification"><div class="section-head"><div>
      <span class="eyebrow">SUPERVISOR CLASSIFICATION</span><h2>${esc(item.event_type)}</h2>
      <h3>${esc(item.event_label)}</h3></div><div class="confidence"><strong>${percent(item.confidence)}</strong><small>confidence</small></div></div>
    ${classificationCheck(a)}
    <p>${esc(item.rationale)}</p>
    <div class="citation-row"><span>Grounded in</span>${item.evidence_ids.map(id => `<b class="tag mono">${esc(id)}</b>`).join("")}</div>
    ${item.evidence_gaps?.length ? `<details class="diagnostics"><summary>Evidence gaps</summary>
      <ul>${item.evidence_gaps.map(value => `<li>${esc(value)}</li>`).join("")}</ul></details>` : ""}
    ${item.human_override ? `<div class="human-change">Human override by ${esc(item.human_override.actor)} — ${esc(item.human_override.note || "No note")}</div>` : ""}
  </section>`;
}

function suiteSection(a) {
  if (!a.plan) return "";
  const suite = a.plan;
  const cases = suite.cases?.map(item => `<article class="case-card ${esc(item.condition)}">
    <span class="case-type">${esc(item.condition)}</span><h3>${esc(item.title)}</h3>
    <p>${esc(item.objective)}</p><small>${esc(item.agent_instruction)}</small>
    <div class="citation-row">${item.evidence_ids.map(id => `<b class="tag mono">${esc(id)}</b>`).join("")}</div>
  </article>`).join("") || "";
  const weights = Object.entries(suite.rubric_weights || {}).map(([key, value]) =>
    `<span><small>${esc(key.replaceAll("_", " "))}</small><b>${percent(value)}</b></span>`
  ).join("");
  const deterministic = suite.execution_mode === "deterministic_stress_rules";
  const rules = deterministic ? `<h3>Rules a human must approve before the run</h3>
    <div class="table-wrap"><table><thead><tr><th>Institution</th><th>AI’s only job</th><th>Code-owned action</th></tr></thead><tbody>
    ${(suite.deterministic_rules || []).map(rule => `<tr><td><strong>${esc(rule.institution_name)}</strong></td>
      <td>Flag stress: yes/no</td><td>If yes, sell ${fmt(rule.target_sell_portfolio_pct, 1)}% of starting portfolio pro rata</td></tr>`).join("")}
    </tbody></table></div>` : "";
  return `<section class="panel suite"><div class="section-head"><div><span class="eyebrow">SUPERVISOR-GENERATED SUITE</span>
    <h2>${esc(suite.title)}</h2></div>
    <span class="status complete">fresh · cached=${esc(suite.runtime?.cached)}</span></div>
    <p class="lead">${esc(suite.objective)}</p><p><strong>Hypothesis:</strong> ${esc(suite.hypothesis)}</p>
    <div class="case-grid">${cases}</div>
    <div class="suite-lower"><div><h3>Deterministic scoring rubric</h3>
      <div class="weight-grid">${weights}</div><p>Pass threshold <b>${percent(suite.pass_threshold)}</b></p></div>
      <div><h3>Approved deterministic execution policy</h3><p>${esc(suite.safeguard?.instruction)}</p>
      <span class="tag">${deterministic ? `safeguarded sale ${fmt(suite.safeguard?.max_daily_portfolio_sell_pct, 1)}% on session 1` : `per-round asset cap ${fmt(suite.safeguard?.max_single_asset_sell_pct, 1)}%`}</span>
      <span class="tag">${deterministic ? "the blocked 10% is not carried forward" : suite.safeguard?.require_staged_execution ? `at least ${suite.safeguard?.minimum_stages || 2} stages, ${suite.safeguard?.minimum_spacing_sessions || 1} session apart` : "timing unrestricted"}</span></div></div>
    ${rules}
    ${suite.human_override ? `<div class="human-change">Suite amended by ${esc(suite.human_override.actor)}</div>` : ""}
  </section>`;
}

function primaryAction(output) {
  if (!output) return "—";
  const active = output.actions.filter(item => item.action !== "hold");
  if (!active.length) return "Hold";
  const action = [...active].sort((a, b) => b.size_pct - a.size_pct)[0];
  return `${action.action} ${action.asset_id} ${fmt(action.size_pct, 1)}%`;
}

function executionSummary(record) {
  const sells = (record.execution_schedule || []).filter(item => item.action === "sell");
  if (!sells.length) return "No scheduled sale";
  const total = sells.reduce((sum, item) => sum + Number(item.portfolio_weight || 0) * Number(item.size_pct), 0);
  const rounds = new Set(sells.map(item => item.round)).size;
  return `${fmt(total, 1)}% portfolio across ${rounds} round${rounds === 1 ? "" : "s"}`;
}

function agentMatrix(a) {
  if (!a.responses?.length && !a.plan?.case_packs) return "";
  const assignments = a.request.institutions;
  const conditions = (a.plan?.cases || []).map(item => item.condition);
  const scoreMap = new Map((a.metrics?.run_scores || []).map(item => [item.run_id, item]));
  const rows = assignments.map(assignment => {
    const profile = state.config.institutions[assignment.institution_id];
    const cells = conditions.map(condition => {
      const records = a.responses.filter(item =>
        item.institution_id === assignment.institution_id && item.condition === condition
      );
      if (!records.length) return `<td><span class="run-state pending">Pending</span></td>`;
      const record = records[0];
      const score = scoreMap.get(record.run_id);
      if (record.status === "running") return `<td><span class="run-state running"><i></i> Running</span></td>`;
      if (record.status === "failed") return `<td><span class="run-state failed">Failed</span><small>${esc(record.error)}</small></td>`;
      return `<td><span class="stance ${esc(record.output.stance)}">${esc(record.output.stance.replace("_", " "))}</span>
        <strong class="decision-line">Intent: ${esc(primaryAction(record.output))}</strong>
        <small>Execution: ${esc(executionSummary(record))}</small>
        <small>urgency ${record.output.urgency} · confidence ${percent(record.output.confidence)}</small>
        ${score?.score != null ? `<span class="score ${score.passed ? "pass" : "fail"}">${percent(score.score)} ${score.passed ? "pass" : "review"}</span>` : ""}</td>`;
    }).join("");
    return `<tr><th><strong>${esc(profile.name)}</strong><small>${esc(portfolioLabel(profile.portfolio))}</small>
      <span class="model-chip">${esc(assignment.model)}</span></th>${cells}</tr>`;
  }).join("");
  return `<section class="panel"><div class="section-head"><div><span class="eyebrow">LIVE INSTITUTION-AGENT EXECUTION</span>
    <h2>Decision matrix</h2></div><span>${a.responses.filter(item => item.status === "complete").length} validated runs</span></div>
    <div class="table-wrap matrix"><table><thead><tr><th>Institution agent</th>
      ${conditions.map(value => `<th>${esc(value)}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>
    ${agentDetails(a)}</section>`;
}

function agentDetails(a) {
  const records = a.responses.filter(item => item.status === "complete");
  if (!records.length) return "";
  return `<details class="data-details"><summary>Inspect agent decisions, tools and traces (${records.length})</summary>
    <div class="agent-detail-grid">${records.map(record => `<details class="agent-detail">
      <summary><span><b>${esc(record.institution_name)}</b><small>${esc(record.condition)} · sample ${record.sample}</small></span>
        <span class="stance ${esc(record.output.stance)}">${esc(record.output.stance.replace("_", " "))}</span></summary>
      <p class="lead-small">${esc(record.output.executive_decision)}</p>
      <h4>Actions</h4><div class="action-list">${record.output.actions.map(action => `<div>
        <b>${esc(action.action)} ${esc(action.asset_id)}</b><span>${fmt(action.size_pct, 1)}% intended exposure</span>
        <small>${esc(action.rationale)}</small></div>`).join("")}</div>
      <h4>Deterministic execution schedule</h4><div class="action-list">${(record.execution_schedule || []).map(item => `<div>
        <b>Round ${item.round} · ${esc(item.action)} ${esc(item.asset_id)}</b><span>${fmt(item.size_pct, 2)}% on ${esc(item.scheduled_for)}</span>
      </div>`).join("") || '<small>No active execution.</small>'}</div>
      <h4>Agent trace</h4><ol class="trace">${record.trace.map(item => `<li><b>${esc(item.node.replaceAll("_", " "))}</b>
        <span>${esc(item.detail)}</span></li>`).join("")}</ol>
      <div class="mini mono">run ${esc(record.run_id)} · origin ${esc(record.decision_origin)} · model ${esc(record.model)} · cached=${esc(record.cached)}</div>
    </details>`).join("")}</div></details>`;
}

function scoreSection(a) {
  if (!a.metrics) return "";
  const m = a.metrics;
  const control = m.conditions.control || {};
  const stress = m.conditions.stress || {};
  const safe = m.conditions.safeguarded || {};
  const rows = Object.values(m.institution_scores).map(item => `<tr>
    <td><strong>${esc(item.name)}</strong></td><td>${percent(item.mean_quality_score)}</td>
    <td>${item.passed_runs}/${item.scored_runs}</td></tr>`).join("");
  const conditionRows = Object.values(m.conditions).map(item => `<tr>
    <td><strong>${esc(item.condition)}</strong></td><td>${item.valid}/${item.attempted}</td>
    <td>${percent(item.active_action_rate)}</td><td>${percent(item.seller_rate)}</td>
    <td>${percent(item.risk_off_rate)}</td><td>${fmt(item.mean_desired_sell_pct, 1)}%</td>
    <td>${fmt(item.mean_action_class_jaccard, 3)}</td><td>${fmt(item.urgency_mean, 2)}</td></tr>`).join("");
  if (!m.execution_simulation) {
    return `<section class="panel score-panel"><div class="section-head"><div><span class="eyebrow">ARCHIVED V3 METRICS</span>
      <h2>Legacy recommendation-only results</h2></div><span class="status failed">not impact evidence</span></div>
      <p>This assessment predates the paired execution contract. Its former “Safeguard Effect” must not be interpreted as contagion or market-impact reduction.</p>
      <div class="table-wrap"><table><thead><tr><th>Condition</th><th>Valid</th><th>Active</th><th>Sellers</th><th>Risk-off</th><th>Desired sell (intent)</th><th>Action class</th><th>Urgency</th></tr></thead>
      <tbody>${conditionRows}</tbody></table></div></section>`;
  }
  const sim = m.execution_simulation;
  const stressExec = sim.conditions.stress;
  const safeExec = sim.conditions.safeguarded;
  const effects = sim.effects;
  const effectRows = Object.values(effects).map(item => `<tr>
    <td><strong>${esc(item.metric)}</strong><small>${esc(item.unit)}</small></td>
    <td>${fmt(item.unmitigated, 4)}</td><td>${fmt(item.safeguarded, 4)}</td>
    <td>${fmt(item.absolute_reduction, 4)}</td><td>${item.relative_reduction == null ? "undefined" : percent(item.relative_reduction)}</td>
    <td><span class="status ${item.status === "improved" ? "complete" : item.status === "worsened" ? "failed" : ""}">${esc(item.status)}</span><small>${esc(item.interpretation)}</small></td></tr>`).join("");
  const roundRows = stressExec.rounds.map((row, index) => {
    const other = safeExec.rounds[index] || {};
    return `<tr><td>${row.round}<small>${esc((other.session_dates || row.session_dates || []).join(" / ") || "no scheduled order")}</small></td><td>${fmt(row.net_executed_sell_pct, 4)}%</td>
      <td>${fmt(other.net_executed_sell_pct, 4)}%</td><td>${fmt(row.system_price_impact_pct, 4)}%</td>
      <td>${fmt(other.system_price_impact_pct, 4)}%</td></tr>`;
  }).join("");
  const verificationRows = m.verification.checks.map(item => `<li class="${item.passed ? "verified" : "invalid"}">
    <b>${item.passed ? "PASS" : "FAIL"} · ${esc(item.name.replaceAll("_", " "))}</b><span>${esc(item.detail)}</span></li>`).join("");
  const primary = sim.primary_effect;
  return `<section class="panel score-panel"><div class="section-head"><div><span class="eyebrow">REPLAYABLE SCORERS</span>
    <h2>Verified intent, execution and system-pressure results</h2></div><span class="status ${m.verification.release_eligible ? "complete" : "failed"}">${m.verification.release_eligible ? "release checks passed" : "release blocked"}</span></div>
    <p><strong>Intent and execution are separate measures.</strong> The AI desired ${fmt(stressExec.desired_sell_pct, 2)}% in both paired paths. The approved policy changes executable scheduled selling from ${fmt(stressExec.scheduled_sell_pct, 2)}% to ${fmt(safeExec.scheduled_sell_pct, 2)}%; impact is calculated from executed selling.</p>
    <p><strong>Historical exogenous portfolio return:</strong> ${fmt(stressExec.historical_exogenous_system_return_pct, 3)}% across the complete approved evidence path; held fixed in both execution conditions.</p>
    <div class="kpi-grid">
      <article><small>AI desired selling (before policy)</small><strong>${fmt(stressExec.desired_sell_pct, 2)}% → ${fmt(safeExec.desired_sell_pct, 2)}%</strong><span>Held fixed in the paired comparison</span></article>
      <article><small>Executable scheduled selling</small><strong>${fmt(stressExec.scheduled_sell_pct, 2)}% → ${fmt(safeExec.scheduled_sell_pct, 2)}%</strong><span>Approved safeguard applied before execution</span></article>
      <article><small>Peak pressure reduction</small><strong>${percent(effects.peak_executed_pressure.relative_reduction)}</strong><span>${esc(effects.peak_executed_pressure.status)}</span></article>
      <article><small>Peak modelled impact reduction</small><strong>${percent(primary.relative_reduction)}</strong><span>${esc(primary.status)} · primary effect</span></article>
    </div>
    <h3>Agent intent before execution policy</h3>
    <div class="table-wrap"><table><thead><tr><th>Condition</th><th>Valid</th><th>Active</th>
      <th>Sellers</th><th>Risk-off</th><th>Desired sell (intent)</th><th>Action-class Jaccard</th><th>Urgency</th>
      </tr></thead><tbody>${conditionRows}</tbody></table></div>
    <h3>Safeguard effects — raw values and direction</h3>
    <div class="table-wrap"><table><thead><tr><th>Metric</th><th>Unmitigated</th><th>Safeguarded</th><th>Absolute reduction</th><th>Relative reduction</th><th>Interpretation</th></tr></thead>
      <tbody>${effectRows}</tbody></table></div>
    <p class="formula mono">${esc(primary.formula)} · negative means worse · zero baseline means undefined</p>
    <h3>Round-by-round execution and impact</h3>
    <div class="table-wrap"><table><thead><tr><th>Round</th><th>Stress executed</th><th>Safe executed</th><th>Stress system impact</th><th>Safe system impact</th></tr></thead>
      <tbody>${roundRows}</tbody></table></div>
    <details class="data-details"><summary>Model assumptions and verification</summary>
      <p class="mono">model=${esc(sim.settings.model_version)} · rounds=${sim.settings.rounds} · depth=${fmt(sim.settings.market_depth_multiple, 2)}× test system · impact persistence=${fmt(sim.settings.impact_persistence, 2)} · feedback sensitivity=${fmt(sim.settings.feedback_sale_sensitivity, 2)}</p>
      <ul class="verification-list">${verificationRows}</ul></details>
    <div class="score-lower"><div><h3>Quality score by institution</h3><table><thead><tr><th>Agent</th><th>Mean</th><th>Passed</th></tr></thead><tbody>${rows}</tbody></table></div>
      <div><h3>Known-event classification check</h3>${classificationCheck(a)}
      <p class="fine-print">Expected labels evaluate the classifier only. They are never supplied as institution-agent answers.</p></div></div>
  </section>`;
}

function reportSection(a) {
  if (!a.report) return "";
  if (!a.metrics?.execution_simulation) {
    return `<section class="panel report"><div class="section-head"><div><span class="eyebrow">ARCHIVED REPORT WITHDRAWN</span>
      <h2>Recommendation-only report is not valid impact evidence</h2></div><span class="status failed">do not use</span></div>
      <p class="report-lead">This report predates the paired execution and dated-path contracts. Its safeguard wording and arithmetic are retained in the audit record but deliberately not displayed as an assessment conclusion. Rerun the event under schema v4.</p></section>`;
  }
  const report = a.report;
  return `<section class="panel report"><div class="section-head"><div><span class="eyebrow">VERIFIED DETERMINISTIC REPORT · HUMAN RELEASE REQUIRED</span>
    <h2>${esc(report.title)}</h2></div><span class="status ${a.status === "complete" ? "complete" : "waiting"}">${a.status === "complete" ? "released" : "draft"}</span></div>
    <p class="report-lead">${esc(report.executive_summary)}</p>
    <div class="grid two"><div><h3>System findings</h3><ul>${report.findings.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div>
      <div><h3>Institution findings</h3><ul>${report.institution_findings.map(item => `<li>${esc(item)}</li>`).join("")}</ul></div></div>
    <h3>Limitations</h3><ul>${report.limitations.map(item => `<li>${esc(item)}</li>`).join("")}</ul>
    <div class="mini mono">generator ${esc(report.model)} · input ${esc(a.metrics?.execution_simulation?.input_sha256?.slice(0, 16))} · verified=${esc(report.verified)}</div>
  </section>`;
}

function plainValidationIssue(value) {
  const error = String(value || "").toLowerCase();
  if (error.includes("unknown evidence id") || error.includes("cite at least one item")) {
    return "cited evidence outside the approved case";
  }
  if (error.includes("not an approved hedge")) {
    return "selected a hedge instrument that is not approved for this portfolio";
  }
  if (error.includes("do not mix hold") || error.includes("hold stance cannot")) {
    return "mixed a hold decision with active trade actions";
  }
  if (error.includes("action asset") && error.includes("not held")) {
    return "selected an asset that this institution does not hold";
  }
  return "did not satisfy one or more required answer fields or decision rules";
}

function activityKind(item) {
  if (item.kind === "schema_repair") return "AI answer correction";
  if (item.kind === "cache_replay") return "Cached demo replay";
  return item.kind;
}

function activityMessage(a, item) {
  if (item.kind !== "schema_repair") return item.message;
  const payload = item.payload || {};
  const response = (a.responses || []).find(value => value.run_id === payload.run_id);
  const model = payload.model || response?.model || a.request?.supervisor_model || "selected AI";
  const institution = payload.institution_name || response?.institution_name;
  let subject = institution ? `${institution} using ${model}` : `the supervisor using ${model}`;
  if (!institution && item.step === "classify") {
    subject = `the event-classification supervisor using ${model}`;
  } else if (!institution && item.step === "suite") {
    subject = `the test-design supervisor using ${model}`;
  }
  const attempt = Number(payload.attempt || 1);
  const allowed = Number(payload.attempts_allowed || 3);
  const reason = payload.plain_reason || plainValidationIssue(payload.validation_error);
  if (attempt >= allowed) {
    return `AI answer check ${attempt} of ${allowed} failed — ${subject} ${reason}. ` +
      "Nothing was accepted, so this step stopped.";
  }
  return `AI answer check ${attempt} of ${allowed} — ${subject} ${reason}. ` +
    "The same AI is being asked to correct its answer; nothing has been accepted yet.";
}

function activitySection(a) {
  const rows = a.activity.slice().reverse().map(item => `<div class="activity-row">
    <time>${clock(item.at)}</time><span class="tag">${esc(item.step)}</span>
    <strong>${esc(activityKind(item))}</strong><span>${esc(activityMessage(a, item))}</span></div>`).join("");
  return `<details class="panel audit"><summary><span><b>Immutable run activity</b>
    <small>${a.activity.length} events · prompts identified by SHA-256</small></span></summary>
    <div class="activity-list">${rows}</div></details>`;
}

function resultValue(value, unit = "%", precision = 2) {
  return value == null ? "—" : `${Number(value) < 0 ? "−" : ""}${Math.abs(Number(value)).toFixed(precision)}${unit}`;
}

function assessmentTabs(active) {
  return `<nav class="assessment-tabs" aria-label="Assessment views">
    <button id="view-results" class="${active === "results" ? "active" : ""}" type="button">Results</button>
    <button id="view-audit" class="${active === "audit" ? "active" : ""}" type="button">Run &amp; audit</button>
  </nav>`;
}

function wireAssessmentTabs() {
  document.querySelector("#view-results")?.addEventListener("click", () => {
    state.assessmentView = "results";
    renderAssessment();
  });
  document.querySelector("#view-audit")?.addEventListener("click", () => {
    state.assessmentView = "audit";
    renderAssessment();
  });
}

function renderDemoResult(a) {
  const result = a.demo_result;
  const peakImpact = result.cards.find(card => card.id === "peak_impact");
  const peakImpactReduction = peakImpact?.effect?.relative_reduction;
  const impactStatus = peakImpact?.effect?.status || "undefined";
  let resultSummary = "The modelled peak market impact comparison is unavailable.";
  if (result.deterministic_rules && result.stress_flag_count === 0) {
    resultSummary = "No institution stress flag activated the selling rule, so neither path sold.";
  } else if (result.deterministic_rules) {
    const outcome = impactStatus === "improved"
      ? "lowered"
      : impactStatus === "worsened" ? "increased" : "did not change";
    resultSummary = `The safeguard reduced the stress sale from 20% to ${fmt(result.daily_cap_pct, 0)}% and ${outcome} modelled peak market impact.`;
  } else if (impactStatus !== "undefined") {
    resultSummary = "The approved execution policy changed modelled peak market impact.";
  }
  const relativeLabel = impactStatus === "worsened"
    ? "Relative increase"
    : impactStatus === "unchanged" ? "Relative change" : "Relative reduction";
  const peakImpactDetail = peakImpact
    ? `<p class="result-headline-values">
        <span>Unmitigated peak <strong>${resultValue(peakImpact.unmitigated, peakImpact.unit, 4)}</strong></span>
        <span>Safeguarded peak <strong>${resultValue(peakImpact.safeguarded, peakImpact.unit, 4)}</strong></span>
        <span>${relativeLabel} <strong>${peakImpactReduction == null ? "—" : `${fmt(Math.abs(Number(peakImpactReduction)) * 100, 3)}%`}</strong></span>
      </p>`
    : "";
  const maxSell = Math.max(1, ...result.rounds.flatMap(row => [
    Number(row.unmitigated_sell_pct || 0), Number(row.safeguarded_sell_pct || 0),
  ]));
  const cards = result.cards.map(card => `<article class="result-card">
    <small>${esc(card.label)}</small>
    <strong>${resultValue(card.unmitigated, card.unit, card.precision)} <i>→</i> ${resultValue(card.safeguarded, card.unit, card.precision)}</strong>
    <span class="result-direction ${esc(card.effect?.status || "undefined")}">${esc(card.effect?.interpretation || "Displayed as the average per institution.")}</span>
  </article>`).join("");
  const roundRows = result.rounds.map(row => {
    const left = Number(row.unmitigated_sell_pct || 0);
    const right = Number(row.safeguarded_sell_pct || 0);
    return `<tr><th scope="row"><span class="session-label">Session ${row.round}</span><small class="session-dates">${esc((row.session_dates || []).join(" / ") || "Model session")}</small></th>
      <td data-label="20% unmitigated"><div class="result-bar"><i class="unmitigated" style="width:${Math.max(1, left / maxSell * 100)}%"></i><b>${fmt(left, 2)}%</b></div>
        ${Number(row.unmitigated_feedback_pct || 0) > 0 ? `<small>${fmt(row.unmitigated_feedback_pct, 2)}% forced by modelled limit breach</small>` : ""}</td>
      <td data-label="Safeguarded"><div class="result-bar"><i class="safeguarded" style="width:${Math.max(1, right / maxSell * 100)}%"></i><b>${fmt(right, 2)}%</b></div>
        ${Number(row.safeguarded_feedback_pct || 0) > 0 ? `<small>${fmt(row.safeguarded_feedback_pct, 2)}% forced by modelled limit breach</small>` : ""}</td></tr>`;
  }).join("");
  const signalRows = result.stress_signals.map(item => `<tr>
    <td data-label="Institution"><strong>${esc(item.institution_name)}</strong><small>${esc(item.institution_id)}</small></td>
    <td data-label="Assigned AI"><span class="model-chip">${esc(item.model)}</span></td>
    <td data-label="Stress">${result.deterministic_rules
      ? `<span class="signal ${item.stress_detected ? "yes" : "no"}">${item.stress_detected ? "Yes" : "No"}</span>`
      : esc(item.decision)}</td>
    <td data-label="Severity">${result.deterministic_rules ? (item.severity == null ? "—" : item.severity) : "Not separately recorded"}</td>
    <td data-label="Cited evidence">${esc(item.cited.join(", ") || "—")}</td></tr>`).join("");
  const ruleRows = result.rules.map(rule => `<tr>
    <td data-label="Institution and holdings"><strong>${esc(rule.institution_name)}</strong><small>${esc(rule.portfolio_label)}</small><small>${esc(rule.model)}</small></td>
    <td data-label="Rule when stress is flagged">${esc(rule.description)}</td>
    <td data-label="20% unmitigated rule">${esc(rule.unmitigated_text)}</td>
    <td data-label="Safeguarded rule">${esc(rule.safeguarded_text)}</td></tr>`).join("");
  const signoff = [
    ["Evidence", result.signoff.evidence],
    ["Rules", result.signoff.rules],
    ["Release", result.signoff.release],
  ].map(([label, item]) => `<div><small>${label}</small><strong>${item ? esc(item.actor) : "Pending"}</strong>
    <span>${item ? esc(new Date(item.at).toLocaleString()) : "Human approval required"}</span></div>`).join("");
  activeId.textContent = a.id;
  const warnings = (result.warnings || []).length
    ? `<div class="result-warnings">${result.warnings.map(item => `<p>${esc(item)}</p>`).join("")}</div>`
    : "";
  app.innerHTML = `${assessmentTabs("results")}${cacheReplayNotice(a)}<section class="result-hero">
      <div class="result-meta">Assessment · ${esc(result.start_date)} → ${esc(result.end_date)} · ${result.institution_count} scenario institutions · ${esc(result.status)}</div>
      <h2>${esc(resultSummary)}</h2>
      ${peakImpactDetail}
      <div class="result-fixed-market">Historical market return held fixed in both paths:
        <strong>${resultValue(result.historical_exogenous_return_pct, "%", 3)}</strong>. It is not included in the safeguard-improvement denominator.</div>
      <div class="result-cards">${cards}</div>
    </section>
    ${warnings}
    ${gate(a)}
    <section class="panel result-panel"><div class="section-head"><div><span class="eyebrow">PAIRED EXECUTION</span>
      <h2>Session by session · selling as % of all selected portfolios combined</h2></div>
      <span class="tag">same evidence and stress flags · safeguard changes 20% to 10%</span></div>
      <p>Session 1 is the first market session after the approved evidence window. Later dates are engine rounds; no future market evidence is supplied to the AIs.</p>
      <div class="table-wrap"><table class="result-rounds"><thead><tr><th>Model session</th><th>20% unmitigated sale</th><th>${result.deterministic_rules ? `${fmt(result.daily_cap_pct, 1)}% safeguarded sale` : "Approved execution policy"}</th></tr></thead>
      <tbody>${roundRows}</tbody></table></div></section>
    <section class="panel result-panel"><div class="section-head"><div><span class="eyebrow">AI ROLE</span>
      <h2>${esc(result.ai_section_title)}</h2></div><span class="tag">${result.deterministic_rules ? `${result.stress_flag_count}/${result.stress_signal_count} flagged across ${result.stress_model_count} model${result.stress_model_count === 1 ? "" : "s"}` : "older AI-decision contract"}</span></div>
      <div class="table-wrap"><table class="result-signals"><thead><tr><th>Institution</th><th>Assigned AI</th><th>${result.deterministic_rules ? "Stress" : "Decision"}</th><th>${result.deterministic_rules ? "Severity" : "Stress flag"}</th><th>Cited evidence</th></tr></thead>
      <tbody>${signalRows}</tbody></table></div></section>
    <section class="panel result-panel"><div class="section-head"><div><span class="eyebrow">FROZEN BEFORE EXECUTION</span>
      <h2>${esc(result.rules_section_title)}</h2></div><span class="tag">${result.deterministic_rules ? "deterministic code" : "retained for audit"}</span></div>
      <div class="table-wrap"><table class="result-rules"><thead><tr><th>Institution and holdings</th><th>${result.deterministic_rules ? "Rule when stress is flagged" : "Recorded intent"}</th><th>20% unmitigated rule</th><th>${result.deterministic_rules ? `${fmt(result.daily_cap_pct, 1)}% safeguarded rule` : "Approved policy"}</th></tr></thead>
      <tbody>${ruleRows}</tbody></table></div></section>
    <section class="result-footer-grid">
      <article class="panel"><span class="eyebrow">WHAT WAS ASSUMED</span><h2>Transparent model boundary</h2>
        <p>${esc(result.assumptions.system_weighting)}. ${esc(result.assumptions.portfolio_loss_formula)}</p>
        <p class="mini mono">model=${esc(result.assumptions.simulation.model_version)} · depth=${fmt(result.assumptions.simulation.market_depth_multiple, 1)}× · limit-breach threshold=${fmt(result.assumptions.simulation.limit_breach_impact_threshold_pct, 2)}% · rounds=${esc(result.assumptions.simulation.rounds)} · input SHA ${esc(result.assumptions.evidence_sha256?.slice(0, 16))}</p>
      </article>
      <article class="panel"><span class="eyebrow">HUMAN SIGN-OFF</span><h2>Evidence, rules and release</h2>
        <div class="result-signoff">${signoff}</div>
        <button id="open-audit" type="button">Open full audit trail →</button>
        <small>${result.audit_event_count} immutable activity events are retained.</small>
      </article>
    </section>`;
  const form = document.querySelector("#approval-form");
  if (form) {
    form.addEventListener("submit", event => submitApproval(event, true));
    document.querySelector("#reject").addEventListener("click", event => submitApproval(event, false));
  }
  wireAssessmentTabs();
  document.querySelector("#open-audit").addEventListener("click", () => {
    state.assessmentView = "audit";
    renderAssessment();
    window.scrollTo({top: 0, behavior: "smooth"});
  });
}

function renderAssessment() {
  const a = state.assessment;
  if (
    a.demo_result
    && ["awaiting_release_approval", "complete", "rejected"].includes(a.status)
    && state.assessmentView !== "audit"
  ) {
    renderDemoResult(a);
    return;
  }
  activeId.textContent = a.id;
  const statusText = a.status.replaceAll("_", " ");
  app.innerHTML = `${a.demo_result ? assessmentTabs("audit") : ""}
    ${cacheReplayNotice(a)}
    <section class="assessment-head"><div><span class="eyebrow">ASSESSMENT ${esc(a.id)}</span>
      <h2>${esc(a.classification?.event_label || "Building assessment from runtime evidence")}</h2>
      <p id="live-status" role="status" aria-live="polite">${esc(statusText)}</p></div>
      <span class="status ${esc(a.status)}">${esc(statusText)}</span></section>
    ${phaseRail(a)}
    ${gate(a)}
    ${scopeSection(a)}
    ${workflow(a)}
    ${retryPanel(a)}
    ${evidenceSection(a)}
    ${classificationSection(a)}
    ${suiteSection(a)}
    ${agentMatrix(a)}
    ${scoreSection(a)}
    ${reportSection(a)}
    ${a.error && a.status !== "failed" ? errorBox(a.error) : ""}
    ${activitySection(a)}`;
  const form = document.querySelector("#approval-form");
  if (form) {
    form.addEventListener("submit", event => submitApproval(event, true));
    document.querySelector("#reject").addEventListener("click", event => submitApproval(event, false));
  }
  document.querySelector("#retry-step")?.addEventListener("click", retryStep);
  wireAssessmentTabs();
  document.querySelectorAll("[data-disclosure-key]").forEach(details => {
    details.addEventListener("toggle", () => {
      rememberDisclosure(details.dataset.disclosureKey, details.open);
    });
  });
}

async function submitApproval(event, approved) {
  event.preventDefault();
  const form = document.querySelector("#approval-form");
  if (!form || form.dataset.submitting === "true") return;
  form.dataset.submitting = "true";
  const values = new FormData(form);
  const gateId = form.dataset.gate;
  const body = {
    actor: values.get("actor"),
    note: values.get("note"),
    approved,
  };
  if (gateId === "event_review") {
    body.event_type = values.get("event_type");
    body.event_label = values.get("event_label");
  }
  if (gateId === "suite_review") {
    body.suite_objective = values.get("suite_objective");
    if (values.has("safeguard_instruction")) {
      body.safeguard_instruction = values.get("safeguard_instruction");
      if (values.has("max_daily_portfolio_sell_pct")) {
        body.max_daily_portfolio_sell_pct = Number(values.get("max_daily_portfolio_sell_pct"));
        body.institution_sell_targets_pct = {};
        (state.assessment.plan.deterministic_rules || []).forEach(rule => {
          body.institution_sell_targets_pct[rule.institution_id] = Number(
            values.get(`sell_target_${rule.institution_id}`)
          );
        });
      } else {
        body.max_single_asset_sell_pct = Number(values.get("max_single_asset_sell_pct"));
      }
    }
  }
  form.querySelectorAll("button,input,select,textarea").forEach(element => { element.disabled = true; });
  try {
    state.assessment = await api(
      `/assessments/${state.assessment.id}/approvals/${gateId}`,
      {method: "POST", body},
    );
    renderAssessment();
    startPolling();
  } catch (error) {
    if (error.status === 409) {
      state.assessment = await api(`/assessments/${state.assessment.id}`);
      renderAssessment();
      startPolling();
      return;
    }
    form.parentElement.querySelectorAll(":scope > .error").forEach(item => item.remove());
    form.insertAdjacentHTML("beforebegin", errorBox(error));
    form.dataset.submitting = "false";
    form.querySelectorAll("button,input,select,textarea").forEach(element => { element.disabled = false; });
  }
}

async function retryStep(event) {
  event.currentTarget.disabled = true;
  try {
    state.assessment = await api(`/assessments/${state.assessment.id}/retry`, {method: "POST"});
    renderAssessment();
    startPolling();
  } catch (error) {
    event.currentTarget.insertAdjacentHTML("beforebegin", errorBox(error));
  }
}

const HISTORY_PAGE_SIZES = [10, 25, 50];
const HISTORY_STATUSES = [
  ["", "All statuses"],
  ["running", "Running"],
  ["awaiting_approval", "Awaiting approval"],
  ["complete", "Complete"],
  ["failed", "Failed"],
  ["rejected", "Rejected"],
];

function historyQueryFromLocation() {
  const params = new URLSearchParams(window.location.search);
  const page = Number(params.get("page"));
  const pageSize = Number(params.get("page_size"));
  const status = params.get("status") || "";
  return {
    page: Number.isInteger(page) && page > 0 ? page : 1,
    pageSize: HISTORY_PAGE_SIZES.includes(pageSize) ? pageSize : 10,
    eventId: params.get("event") || "",
    status: HISTORY_STATUSES.some(([value]) => value === status) ? status : "",
  };
}

function historyApiPath() {
  const query = state.historyQuery;
  const params = new URLSearchParams({
    page: String(query.page),
    page_size: String(query.pageSize),
  });
  if (query.eventId) params.set("event_id", query.eventId);
  if (query.status) params.set("status", query.status);
  return `/assessments?${params.toString()}`;
}

function syncHistoryUrl() {
  const query = state.historyQuery;
  const params = new URLSearchParams({view: "history"});
  if (query.page > 1) params.set("page", String(query.page));
  if (query.pageSize !== 10) params.set("page_size", String(query.pageSize));
  if (query.eventId) params.set("event", query.eventId);
  if (query.status) params.set("status", query.status);
  history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
}

function historyPageItems(current, total) {
  if (total <= 7) return Array.from({length: total}, (_, index) => index + 1);
  const visible = [...new Set([
    1, total, current - 1, current, current + 1,
  ].filter(page => page >= 1 && page <= total))].sort((left, right) => left - right);
  const items = [];
  visible.forEach((page, index) => {
    if (index && page - visible[index - 1] > 1) items.push("…");
    items.push(page);
  });
  return items;
}

function historyEventOptions(selected) {
  const options = [
    ["", "All events"],
    ["custom", "Custom event"],
    ...Object.entries(state.config.known_events).map(([id, item]) => [id, item.label]),
  ];
  if (selected && !options.some(([value]) => value === selected)) {
    options.push([selected, selected]);
  }
  return options.map(([value, label]) =>
    `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(label)}</option>`
  ).join("");
}

function historyStatusOptions(selected) {
  return HISTORY_STATUSES.map(([value, label]) =>
    `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(label)}</option>`
  ).join("");
}

function startPolling() {
  if (state.poll) clearInterval(state.poll);
  if (state.assessment?.status !== "running") {
    state.poll = null;
    return;
  }
  state.poll = setInterval(async () => {
    try {
      state.assessment = await api(`/assessments/${state.assessment.id}/progress`);
      renderAssessment();
      if (state.assessment.status !== "running") {
        clearInterval(state.poll);
        state.poll = null;
      }
    } catch (error) {
      console.error(error);
    }
  }, 1200);
}

async function renderHistory() {
  if (state.poll) {
    clearInterval(state.poll);
    state.poll = null;
  }
  const requestId = ++state.historyRequest;
  if (!state.history) {
    app.innerHTML = '<div class="loading"><span class="spinner"></span><strong>Loading assessment history…</strong></div>';
  }
  let response;
  try { response = await api(historyApiPath()); }
  catch (error) { app.innerHTML = errorBox(error); return; }
  if (requestId !== state.historyRequest) return;
  state.history = response;
  const pagination = response.pagination;
  state.historyQuery.page = pagination.page;
  syncHistoryUrl();
  const start = pagination.total ? ((pagination.page - 1) * pagination.page_size) + 1 : 0;
  const end = Math.min(pagination.page * pagination.page_size, pagination.total);
  const filtered = Boolean(state.historyQuery.eventId || state.historyQuery.status);
  const rows = state.history.assessments.map(item => `<tr>
    <td><strong class="mono">${esc(item.id)}</strong></td>
    <td>${esc(item.window)}<small>${esc(item.event_id === "custom" ? "Custom event" : state.config.known_events[item.event_id]?.label || item.event_id)}</small></td>
    <td>${esc(item.event_type || "Pending classification")}</td>
    <td>${item.institution_count || "—"}</td><td>${item.agent_run_count}</td>
    <td><span class="status ${item.result_validity === "withdrawn_legacy_metrics" ? "failed" : esc(item.status)}">${item.result_validity === "withdrawn_legacy_metrics" ? "withdrawn legacy result" : esc(item.status)}</span></td>
    <td><button data-open="${esc(item.id)}">Open</button></td></tr>`).join("");
  const pageButtons = historyPageItems(
    pagination.page, pagination.total_pages
  ).map(item => item === "…"
    ? '<span class="pagination-ellipsis" aria-hidden="true">…</span>'
    : `<button type="button" data-history-page="${item}" class="${item === pagination.page ? "active" : ""}" ${item === pagination.page ? 'aria-current="page"' : `aria-label="Page ${item}"`}>${item}</button>`
  ).join("");
  const paginationControls = pagination.total_pages > 1 ? `
    <nav class="history-pagination" aria-label="Assessment history pagination">
      <button type="button" data-history-page="${pagination.page - 1}" ${pagination.has_previous ? "" : "disabled"} aria-label="Previous page">← Previous</button>
      <span class="pagination-pages">${pageButtons}</span>
      <button type="button" data-history-page="${pagination.page + 1}" ${pagination.has_next ? "" : "disabled"} aria-label="Next page">Next →</button>
    </nav>` : "";
  app.innerHTML = `<section class="panel history"><div class="section-head"><div><span class="eyebrow">RUN REGISTRY</span>
    <h2>Assessment history</h2></div><span>${pagination.total} ${filtered ? "matching " : ""}record${pagination.total === 1 ? "" : "s"}</span></div>
    <div class="history-filters">
      <label>Event<select id="history-event">${historyEventOptions(state.historyQuery.eventId)}</select></label>
      <label>Status<select id="history-status">${historyStatusOptions(state.historyQuery.status)}</select></label>
      <button type="button" id="history-clear" ${filtered ? "" : "disabled"}>Clear filters</button>
    </div>
    <div class="table-wrap"><table><thead><tr><th>Assessment</th><th>Evidence window</th>
      <th>Event</th><th>Institutions</th><th>Agent runs</th><th>Status</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="empty">${filtered ? "No assessments match these filters." : "No assessments yet."}</td></tr>`}</tbody></table></div>
    <div class="history-footer"><span>Showing ${start}–${end} of ${pagination.total} assessments</span>
      <label>Rows per page<select id="history-page-size">${HISTORY_PAGE_SIZES.map(size => `<option value="${size}" ${size === pagination.page_size ? "selected" : ""}>${size}</option>`).join("")}</select></label></div>
    ${paginationControls}
  </section>`;
  document.querySelector("#history-event").addEventListener("change", event => {
    state.historyQuery.eventId = event.currentTarget.value;
    state.historyQuery.page = 1;
    renderHistory();
  });
  document.querySelector("#history-status").addEventListener("change", event => {
    state.historyQuery.status = event.currentTarget.value;
    state.historyQuery.page = 1;
    renderHistory();
  });
  document.querySelector("#history-page-size").addEventListener("change", event => {
    state.historyQuery.pageSize = Number(event.currentTarget.value);
    state.historyQuery.page = 1;
    renderHistory();
  });
  document.querySelector("#history-clear").addEventListener("click", () => {
    state.historyQuery = {page: 1, pageSize: state.historyQuery.pageSize, eventId: "", status: ""};
    renderHistory();
  });
  document.querySelectorAll("[data-history-page]").forEach(button => button.addEventListener("click", () => {
    if (button.disabled) return;
    state.historyQuery.page = Number(button.dataset.historyPage);
    renderHistory();
  }));
  document.querySelectorAll("[data-open]").forEach(button => button.addEventListener("click", async () => {
    state.assessment = await api(`/assessments/${button.dataset.open}`);
    state.assessmentView = "results";
    history.replaceState(
      null, "", `?assessment=${encodeURIComponent(state.assessment.id)}`
    );
    renderAssessment();
    startPolling();
  }));
}

document.querySelector("#new-assessment").addEventListener("click", () => {
  stopCacheReplay();
  history.replaceState(null, "", window.location.pathname);
  renderConfigure();
});
document.querySelector("#show-history").addEventListener("click", () => {
  stopCacheReplay();
  state.history = null;
  state.historyQuery = {page: 1, pageSize: 10, eventId: "", status: ""};
  renderHistory();
});

(async function boot() {
  try {
    state.config = await api("/config");
    const health = document.querySelector("#health");
    health.textContent = state.config.ready
      ? `${state.config.models.length} live model endpoint(s)`
      : "Runtime not ready";
    health.classList.add(state.config.ready ? "ok" : "bad");
    const requestedAssessment = new URLSearchParams(window.location.search).get(
      "assessment"
    );
    if (requestedAssessment) {
      state.assessment = await api(
        `/assessments/${encodeURIComponent(requestedAssessment)}`
      );
      state.assessmentView = "results";
      renderAssessment();
      startPolling();
    } else if (new URLSearchParams(window.location.search).get("view") === "history") {
      state.historyQuery = historyQueryFromLocation();
      await renderHistory();
    } else {
      renderConfigure();
    }
  } catch (error) {
    app.innerHTML = errorBox(error);
  }
})();
