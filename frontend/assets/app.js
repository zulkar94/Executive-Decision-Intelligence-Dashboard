/**
 * Executive Decision Intelligence - zero-build SPA.
 *
 * Security posture:
 *  - Tokens live in module scope only. Nothing is written to localStorage or
 *    sessionStorage, so an injected script cannot read them back after reload.
 *  - All DOM writes go through textContent or explicit element creation; the
 *    app never assigns innerHTML from server data.
 *  - The access token is refreshed once on 401 and the request replayed.
 */

const API = "/api/v1";
const state = {
  access: null,
  refresh: null,
  user: null,
  view: "summary",
  decisionId: null,
  method: "weighted_sum",
};

/* ---------------------------------------------------------------- helpers */
const $ = (selector) => document.querySelector(selector);
const el = (tag, props = {}, children = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of [].concat(children)) {
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
};
const fmt = (value, digits = 3) =>
  value === null || value === undefined || Number.isNaN(value) ? "--" : Number(value).toFixed(digits);
const pct = (value, digits = 1) =>
  value === null || value === undefined ? "--" : `${(Number(value) * 100).toFixed(digits)}%`;

let toastTimer;
function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("is-visible"), 3200);
}

async function api(path, { method = "GET", body, retry = true } = {}) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.access) headers.Authorization = `Bearer ${state.access}`;

  const response = await fetch(`${API}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401 && retry && state.refresh) {
    if (await refreshSession()) return api(path, { method, body, retry: false });
    signOut();
    throw new Error("Session expired. Sign in again.");
  }
  if (response.status === 204) return null;

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(payload.errors) && payload.errors.length
      ? `${payload.detail}: ${payload.errors[0].loc.slice(-1)[0]} ${payload.errors[0].msg}`
      : payload.detail || `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return payload;
}

async function refreshSession() {
  try {
    const response = await fetch(`${API}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: state.refresh }),
    });
    if (!response.ok) return false;
    const pair = await response.json();
    state.access = pair.access_token;
    state.refresh = pair.refresh_token;
    return true;
  } catch {
    return false;
  }
}

/* ------------------------------------------------------------------ chart */
const palette = () => {
  const styles = getComputedStyle(document.documentElement);
  return {
    ink: styles.getPropertyValue("--ink").trim(),
    muted: styles.getPropertyValue("--muted").trim(),
    rule: styles.getPropertyValue("--rule").trim(),
    signal: styles.getPropertyValue("--signal").trim(),
    caution: styles.getPropertyValue("--caution").trim(),
    adverse: styles.getPropertyValue("--adverse").trim(),
  };
};

function canvas(width, height) {
  const node = el("canvas");
  const ratio = window.devicePixelRatio || 1;
  node.width = width * ratio;
  node.height = height * ratio;
  node.style.aspectRatio = `${width} / ${height}`;
  const ctx = node.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.font = "12px system-ui, sans-serif";
  return { node, ctx, width, height };
}

/** Horizontal bars: [{label, value, highlight}] with values already in [0, 1]. */
function barChart(rows, { width = 620, height = 0, labelWidth = 168 } = {}) {
  const h = height || Math.max(90, rows.length * 34 + 24);
  const { node, ctx, width: w } = canvas(width, h);
  const colors = palette();
  const max = Math.max(...rows.map((row) => row.value), 1e-9);
  rows.forEach((row, index) => {
    const y = 14 + index * 34;
    ctx.fillStyle = colors.ink;
    ctx.textAlign = "right";
    ctx.fillText(row.label.slice(0, 26), labelWidth - 12, y + 14);
    const span = (w - labelWidth - 60) * (row.value / max);
    ctx.fillStyle = row.highlight ? colors.signal : colors.rule;
    ctx.fillRect(labelWidth, y, Math.max(span, 1), 18);
    ctx.fillStyle = colors.muted;
    ctx.textAlign = "left";
    ctx.fillText(row.display ?? fmt(row.value), labelWidth + Math.max(span, 1) + 8, y + 14);
  });
  return node;
}

/** Tornado: symmetric bars around a centre axis, sorted by swing. */
function tornadoChart(rows, { width = 620 } = {}) {
  const h = Math.max(100, rows.length * 30 + 30);
  const { node, ctx, width: w } = canvas(width, h);
  const colors = palette();
  const axis = 210;
  const max = Math.max(...rows.map((row) => row.swing), 1e-9);
  ctx.strokeStyle = colors.rule;
  ctx.beginPath();
  ctx.moveTo(axis, 10);
  ctx.lineTo(axis, h - 10);
  ctx.stroke();
  rows.forEach((row, index) => {
    const y = 16 + index * 30;
    const span = ((w - axis - 90) * row.swing) / max;
    ctx.fillStyle = colors.ink;
    ctx.textAlign = "right";
    ctx.fillText(row.criterion.slice(0, 24), axis - 10, y + 13);
    ctx.fillStyle = row.robust ? colors.rule : colors.caution;
    ctx.fillRect(axis + 1, y, Math.max(span, 1), 17);
    ctx.fillStyle = colors.muted;
    ctx.textAlign = "left";
    ctx.fillText(
      row.flip_weight === null ? `${fmt(row.swing, 2)} · stable` : `${fmt(row.swing, 2)} · flips at w=${fmt(row.flip_weight, 2)}`,
      axis + Math.max(span, 1) + 8,
      y + 13,
    );
  });
  return node;
}

/** Line chart with an optional forecast band. */
function lineChart(history, forecastBand, { width = 620, height = 220 } = {}) {
  const { node, ctx, width: w, height: h } = canvas(width, height);
  const colors = palette();
  const pad = { left: 46, right: 14, top: 14, bottom: 24 };
  const forecast = forecastBand?.forecast ?? [];
  const lower = forecastBand?.lower ?? [];
  const upper = forecastBand?.upper ?? [];
  const all = history.concat(forecast, lower, upper).filter(Number.isFinite);
  if (!all.length) return node;
  const min = Math.min(...all);
  const max = Math.max(...all);
  const span = max - min || 1;
  const total = history.length + forecast.length;
  const x = (i) => pad.left + ((w - pad.left - pad.right) * i) / Math.max(total - 1, 1);
  const y = (v) => pad.top + (h - pad.top - pad.bottom) * (1 - (v - min) / span);

  ctx.strokeStyle = colors.rule;
  ctx.fillStyle = colors.muted;
  ctx.textAlign = "right";
  for (let step = 0; step <= 3; step += 1) {
    const value = min + (span * step) / 3;
    ctx.beginPath();
    ctx.moveTo(pad.left, y(value));
    ctx.lineTo(w - pad.right, y(value));
    ctx.stroke();
    ctx.fillText(value.toFixed(1), pad.left - 8, y(value) + 4);
  }

  if (forecast.length) {
    ctx.fillStyle = "rgba(31, 79, 163, 0.14)";
    ctx.beginPath();
    upper.forEach((value, i) => {
      const px = x(history.length + i);
      i === 0 ? ctx.moveTo(px, y(value)) : ctx.lineTo(px, y(value));
    });
    for (let i = lower.length - 1; i >= 0; i -= 1) {
      ctx.lineTo(x(history.length + i), y(lower[i]));
    }
    ctx.closePath();
    ctx.fill();
  }

  ctx.strokeStyle = colors.ink;
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  history.forEach((value, i) => (i === 0 ? ctx.moveTo(x(i), y(value)) : ctx.lineTo(x(i), y(value))));
  ctx.stroke();

  if (forecast.length) {
    ctx.strokeStyle = colors.signal;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(x(history.length - 1), y(history[history.length - 1]));
    forecast.forEach((value, i) => ctx.lineTo(x(history.length + i), y(value)));
    ctx.stroke();
    ctx.setLineDash([]);
  }
  return node;
}

/* ------------------------------------------------------------------ views */
async function renderSummary(main) {
  main.replaceChildren(
    el("div", { className: "head" }, [
      el("div", {}, [el("h2", {}, "Portfolio"), el("p", {}, "Indicator health and decisions whose winner is not safe.")]),
    ]),
    el("p", { className: "empty" }, "Loading."),
  );
  const summary = await api("/analytics/executive-summary");

  const health = el("table", {}, [
    el("thead", {}, el("tr", {}, [
      el("th", {}, "Indicator"),
      el("th", { className: "num" }, "Latest"),
      el("th", { className: "num" }, "Change"),
      el("th", { className: "num" }, "Vs target"),
      el("th", { className: "num" }, "Anomalies"),
      el("th", {}, "Status"),
    ])),
    el("tbody", {}, summary.kpi_health.map((row) =>
      el("tr", {}, [
        el("td", {}, row.name),
        el("td", { className: "num" }, fmt(row.latest, 2)),
        el("td", { className: "num" }, row.change_pct === null ? "--" : `${fmt(row.change_pct, 1)}%`),
        el("td", { className: "num" }, row.to_target_pct === null ? "--" : `${fmt(row.to_target_pct, 1)}%`),
        el("td", { className: "num" }, row.anomalies),
        el("td", {}, el("span", { className: `pill ${row.status}` }, row.status.replace("_", " "))),
      ]),
    )),
  ]);

  const fragile = summary.fragile_decisions.length
    ? summary.fragile_decisions.map((item) =>
        el("div", { className: "list-row" }, [
          el("button", { type: "button", onclick: () => openDecision(item.decision_id) }, item.title),
          el("span", {}, `${item.winner} leads by ${fmt(item.margin, 3)}`),
        ]),
      )
    : [el("p", { className: "empty" }, "No decision is within a hair of reversing.")];

  main.replaceChildren(
    el("div", { className: "head" }, [
      el("div", {}, [el("h2", {}, "Portfolio"), el("p", {}, "Indicator health and decisions whose winner is not safe.")]),
    ]),
    el("section", { className: "panel" }, [
      el("div", { className: "stat-row" }, [
        el("div", { className: "stat" }, [el("b", {}, `${fmt(summary.portfolio_score, 0)}`), el("span", {}, "Portfolio score")]),
        el("div", { className: "stat" }, [el("b", {}, summary.open_decisions), el("span", {}, "Open decisions")]),
        el("div", { className: "stat" }, [el("b", {}, summary.fragile_decisions.length), el("span", {}, "Fragile winners")]),
      ]),
    ]),
    el("section", { className: "panel" }, [el("h3", {}, "Indicators"), health]),
    el("section", { className: "panel" }, [el("h3", {}, "Fragile decisions"), ...fragile]),
  );
}

async function renderDecisions(main) {
  const decisions = await api("/decisions");
  const rows = decisions.length
    ? decisions.map((decision) =>
        el("div", { className: "list-row" }, [
          el("button", { type: "button", onclick: () => openDecision(decision.id) }, decision.title),
          el("span", {}, `${decision.options.length} options · ${decision.criteria.length} criteria · ${decision.status}`),
        ]),
      )
    : [el("p", { className: "empty" }, "No decisions yet.")];

  const title = el("input", { id: "new-title", type: "text", placeholder: "Decision under consideration" });
  main.replaceChildren(
    el("div", { className: "head" }, [
      el("div", {}, [el("h2", {}, "Decisions"), el("p", {}, "Every decision carries its criteria, options, and scored matrix.")]),
    ]),
    el("section", { className: "panel" }, rows),
    el("section", { className: "panel" }, [
      el("h3", {}, "Open a new decision"),
      title,
      el("div", { className: "actions" }, [
        el("button", {
          className: "primary",
          type: "button",
          onclick: async () => {
            try {
              const created = await api("/decisions", { method: "POST", body: { title: title.value.trim() } });
              toast("Decision created");
              openDecision(created.id);
            } catch (error) {
              toast(error.message);
            }
          },
        }, "Create decision"),
      ]),
    ]),
  );
}

function openDecision(decisionId) {
  state.decisionId = decisionId;
  state.view = "decision";
  render();
}

async function renderDecision(main) {
  const decision = await api(`/decisions/${state.decisionId}`);
  main.replaceChildren(
    el("div", { className: "head" }, [
      el("div", {}, [el("h2", {}, decision.title), el("p", {}, decision.description || "No brief recorded.")]),
      el("button", { className: "ghost", type: "button", onclick: () => { state.view = "decisions"; render(); } }, "All decisions"),
    ]),
    el("p", { className: "empty" }, "Scoring."),
  );

  const sections = [];
  const canAnalyse = decision.options.length >= 2 && decision.criteria.length >= 1;

  if (!canAnalyse) {
    sections.push(el("section", { className: "panel" }, [
      el("h3", {}, "Set up the matrix"),
      el("p", {}, "Add at least two options and one criterion, then score each cell."),
    ]));
  }

  sections.push(buildMatrixEditor(decision));

  if (canAnalyse) {
    try {
      const ranking = await api(`/decisions/${decision.id}/ranking?method=${state.method}`);
      const winner = ranking.ranked[0];
      const runnerUp = ranking.ranked[1];
      sections.unshift(el("section", { className: "panel verdict" }, [
        el("div", {}, [
          el("div", { className: "verdict-name" }, winner.name),
          el("div", { className: "verdict-meta" }, `leads ${runnerUp ? runnerUp.name : "the field"} by ${fmt(ranking.winner_margin)} on ${state.method.replace("_", " ")}`),
        ]),
        barChart(ranking.ranked.map((row) => ({ label: row.name, value: row.score, highlight: row.rank === 1 }))),
      ]));

      const sensitivity = await api(`/decisions/${decision.id}/sensitivity?method=${state.method}`);
      sections.push(el("section", { className: "panel" }, [
        el("h3", {}, "Which weight decides this"),
        el("p", {}, `Sweeping one weight at a time while the others hold their proportions. Amber bars can reverse ${sensitivity.incumbent_option}.`),
        tornadoChart(sensitivity.rows),
      ]));

      sections.push(buildSimulationPanel(decision.id));
    } catch (error) {
      sections.push(el("section", { className: "panel" }, [el("p", { className: "error" }, error.message)]));
    }
  }

  main.replaceChildren(
    el("div", { className: "head" }, [
      el("div", {}, [el("h2", {}, decision.title), el("p", {}, decision.description || "No brief recorded.")]),
      el("div", { className: "actions" }, [
        el("select", {
          onchange: (event) => { state.method = event.target.value; render(); },
        }, [
          el("option", { value: "weighted_sum", selected: state.method === "weighted_sum" }, "Weighted sum"),
          el("option", { value: "topsis", selected: state.method === "topsis" }, "TOPSIS"),
        ]),
        el("button", { className: "ghost", type: "button", onclick: () => { state.view = "decisions"; render(); } }, "All decisions"),
      ]),
    ]),
    ...sections,
  );
}

function buildMatrixEditor(decision) {
  const scoreCache = new Map();
  const head = el("tr", {}, [el("th", {}, "Option")].concat(
    decision.criteria.map((criterion) =>
      el("th", { className: "num" }, `${criterion.name} (w ${fmt(criterion.weight, 2)}, ${criterion.direction})`),
    ),
  ));
  const body = el("tbody", {}, decision.options.map((option) =>
    el("tr", {}, [el("td", {}, option.name)].concat(
      decision.criteria.map((criterion) => {
        const value = el("input", { type: "number", step: "0.1", value: "", "aria-label": `${option.name} on ${criterion.name}` });
        const spread = el("input", { type: "number", step: "0.1", min: "0", value: "", "aria-label": `Uncertainty for ${option.name} on ${criterion.name}` });
        scoreCache.set(`${option.id}:${criterion.id}`, { value, spread });
        return el("td", { className: "num" }, [value, spread]);
      }),
    )),
  ));

  api(`/decisions/${decision.id}/scores`).then((scores) => {
    for (const score of scores) {
      const cell = scoreCache.get(`${score.option_id}:${score.criterion_id}`);
      if (cell) {
        cell.value.value = score.value;
        cell.spread.value = score.uncertainty;
      }
    }
  }).catch(() => toast("Could not load existing scores"));

  const save = el("button", { className: "primary", type: "button", onclick: async () => {
    const payload = [];
    for (const [key, cell] of scoreCache.entries()) {
      if (cell.value.value === "") continue;
      const [optionId, criterionId] = key.split(":");
      payload.push({
        option_id: optionId,
        criterion_id: criterionId,
        value: Number(cell.value.value),
        uncertainty: Number(cell.spread.value || 0),
      });
    }
    if (!payload.length) return toast("Nothing to save");
    try {
      await api(`/decisions/${decision.id}/scores`, { method: "PUT", body: { scores: payload } });
      toast(`Saved ${payload.length} cells`);
      render();
    } catch (error) {
      toast(error.message);
    }
  } }, "Save matrix");

  const criterionName = el("input", { type: "text", placeholder: "Criterion" });
  const criterionWeight = el("input", { type: "number", step: "0.05", value: "1", min: "0" });
  const criterionDirection = el("select", {}, [
    el("option", { value: "benefit" }, "Higher is better"),
    el("option", { value: "cost" }, "Lower is better"),
  ]);
  const optionName = el("input", { type: "text", placeholder: "Option" });

  return el("section", { className: "panel" }, [
    el("h3", {}, "Decision matrix"),
    el("p", {}, "Second field in each cell is the uncertainty used by the simulation."),
    decision.options.length && decision.criteria.length
      ? el("table", {}, [el("thead", {}, head), body])
      : el("p", { className: "empty" }, "Add options and criteria to build the matrix."),
    el("div", { className: "actions" }, [save]),
    el("div", { className: "actions" }, [
      criterionName, criterionWeight, criterionDirection,
      el("button", { className: "ghost", type: "button", onclick: async () => {
        try {
          await api(`/decisions/${decision.id}/criteria`, { method: "POST", body: {
            name: criterionName.value.trim(),
            weight: Number(criterionWeight.value),
            direction: criterionDirection.value,
          } });
          toast("Criterion added");
          render();
        } catch (error) { toast(error.message); }
      } }, "Add criterion"),
    ]),
    el("div", { className: "actions" }, [
      optionName,
      el("button", { className: "ghost", type: "button", onclick: async () => {
        try {
          await api(`/decisions/${decision.id}/options`, { method: "POST", body: { name: optionName.value.trim() } });
          toast("Option added");
          render();
        } catch (error) { toast(error.message); }
      } }, "Add option"),
    ]),
  ]);
}

function buildSimulationPanel(decisionId) {
  const runs = el("input", { type: "number", value: "5000", min: "100", step: "500" });
  const jitter = el("input", { type: "number", value: "0.15", min: "0", max: "1", step: "0.05" });
  const fallback = el("input", { type: "number", value: "0", min: "0", step: "0.1" });
  const output = el("div", {}, el("p", { className: "empty" }, "Not simulated yet."));

  const run = el("button", { className: "primary", type: "button", onclick: async () => {
    run.disabled = true;
    try {
      const result = await api(`/decisions/${decisionId}/simulate`, { method: "POST", body: {
        runs: Number(runs.value),
        weight_jitter: Number(jitter.value),
        default_uncertainty: Number(fallback.value),
      } });
      output.replaceChildren(
        el("div", { className: "stat-row" }, [
          el("div", { className: "stat" }, [el("b", {}, pct(result.decision_confidence)), el("span", {}, "Confidence in the ranked winner")]),
          el("div", { className: "stat" }, [el("b", {}, result.runs.toLocaleString()), el("span", {}, "Runs")]),
          el("div", { className: "stat" }, [el("b", {}, result.seed), el("span", {}, "Seed (reproducible)")]),
        ]),
        barChart(result.options.map((row, index) => ({
          label: row.name,
          value: row.win_probability,
          highlight: index === 0,
          display: `${pct(row.win_probability)} · p05 ${fmt(row.p05, 2)} / p95 ${fmt(row.p95, 2)}`,
        })), { labelWidth: 150 }),
      );
    } catch (error) {
      output.replaceChildren(el("p", { className: "error" }, error.message));
    } finally {
      run.disabled = false;
    }
  } }, "Run simulation");

  return el("section", { className: "panel" }, [
    el("h3", {}, "Outcome under uncertainty"),
    el("p", {}, "Scores are resampled from their stated spread and weights are perturbed; the winner is recounted every draw."),
    el("div", { className: "actions" }, [
      el("label", {}, "Runs"), runs,
      el("label", {}, "Weight jitter"), jitter,
      el("label", {}, "Default spread"), fallback,
      run,
    ]),
    output,
  ]);
}

async function renderKpis(main) {
  const kpis = await api("/kpis");
  const sections = [
    el("div", { className: "head" }, [
      el("div", {}, [el("h2", {}, "Indicators"), el("p", {}, "Holt trend forecast with a 95% band, and robust anomaly flags.")]),
    ]),
  ];

  if (!kpis.length) {
    sections.push(el("section", { className: "panel" }, el("p", { className: "empty" }, "No indicators registered.")));
  }

  for (const kpi of kpis) {
    const points = await api(`/kpis/${kpi.id}/points?limit=180`);
    const history = points.map((point) => point.value);
    const panel = el("section", { className: "panel" }, [
      el("h3", {}, `${kpi.name}${kpi.unit ? ` (${kpi.unit})` : ""}`),
      el("p", {}, kpi.target === null ? "No target set." : `Target ${fmt(kpi.target, 2)} · ${kpi.direction === "benefit" ? "higher is better" : "lower is better"}`),
    ]);
    if (history.length >= 4) {
      try {
        const [forecast, anomalies] = await Promise.all([
          api(`/kpis/${kpi.id}/forecast?horizon=14`),
          api(`/kpis/${kpi.id}/anomalies`),
        ]);
        panel.append(
          lineChart(history, forecast),
          el("div", { className: "legend" }, [
            el("span", {}, `Trend ${fmt(forecast.trend_per_period, 3)} per period`),
            el("span", {}, `In-sample MAPE ${fmt(forecast.mape, 2)}%`),
            el("span", {}, forecast.backtest ? `Holdout MAPE ${fmt(forecast.backtest.mape, 2)}%` : "No holdout"),
            el("span", {}, `${anomalies.length} anomalies`),
          ]),
        );
      } catch (error) {
        panel.append(el("p", { className: "error" }, error.message));
      }
    } else {
      panel.append(el("p", { className: "empty" }, "Needs at least four observations."));
    }
    sections.push(panel);
  }
  main.replaceChildren(...sections);
}

async function renderAudit(main) {
  const entries = await api("/admin/audit?limit=100");
  main.replaceChildren(
    el("div", { className: "head" }, [
      el("div", {}, [el("h2", {}, "Audit"), el("p", {}, "Append-only trail. Client addresses are stored as salted digests.")]),
    ]),
    el("section", { className: "panel" }, el("table", {}, [
      el("thead", {}, el("tr", {}, [el("th", {}, "When"), el("th", {}, "Action"), el("th", {}, "Entity"), el("th", {}, "Outcome")])),
      el("tbody", {}, entries.map((entry) =>
        el("tr", {}, [
          el("td", {}, new Date(entry.ts).toLocaleString()),
          el("td", {}, entry.action),
          el("td", {}, `${entry.entity}${entry.entity_id ? ` ${entry.entity_id.slice(0, 8)}` : ""}`),
          el("td", {}, entry.outcome),
        ]),
      )),
    ])),
  );
}

/* ------------------------------------------------------------------ shell */
async function render() {
  const main = $("#main");
  for (const button of document.querySelectorAll(".nav-item")) {
    button.classList.toggle("is-active", button.dataset.view === state.view);
  }
  try {
    if (state.view === "summary") await renderSummary(main);
    else if (state.view === "decisions") await renderDecisions(main);
    else if (state.view === "decision") await renderDecision(main);
    else if (state.view === "kpis") await renderKpis(main);
    else if (state.view === "audit") await renderAudit(main);
  } catch (error) {
    main.replaceChildren(el("section", { className: "panel" }, el("p", { className: "error" }, error.message)));
  }
}

function signOut() {
  if (state.refresh) {
    fetch(`${API}/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: state.refresh }),
    }).catch(() => {});
  }
  state.access = null;
  state.refresh = null;
  state.user = null;
  $("#app").hidden = true;
  $("#gate").hidden = false;
}

async function enterApp() {
  state.user = await api("/auth/me");
  $("#who").textContent = `${state.user.email} · ${state.user.role}`;
  $("#nav-audit").hidden = state.user.role !== "admin";
  $("#gate").hidden = true;
  $("#app").hidden = false;
  state.view = "summary";
  await render();
}

function wireGate() {
  let mode = "login";
  const submit = $("#gate-submit");
  const error = $("#gate-error");

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      mode = tab.dataset.mode;
      document.querySelectorAll(".tab").forEach((other) => other.classList.toggle("is-active", other === tab));
      submit.textContent = mode === "login" ? "Sign in" : "Create account";
      $("#password").setAttribute("autocomplete", mode === "login" ? "current-password" : "new-password");
      error.textContent = "";
    });
  }

  submit.addEventListener("click", async () => {
    const email = $("#email").value.trim();
    const password = $("#password").value;
    error.textContent = "";
    submit.disabled = true;
    try {
      if (mode === "register") {
        await api("/auth/register", { method: "POST", body: { email, password } });
        toast("Account created. Signing in.");
      }
      const pair = await api("/auth/login", { method: "POST", body: { email, password } });
      state.access = pair.access_token;
      state.refresh = pair.refresh_token;
      await enterApp();
    } catch (exception) {
      error.textContent = exception.message;
    } finally {
      submit.disabled = false;
    }
  });

  $("#password").addEventListener("keydown", (event) => {
    if (event.key === "Enter") submit.click();
  });
  $("#signout").addEventListener("click", signOut);
  for (const button of document.querySelectorAll(".nav-item")) {
    button.addEventListener("click", () => {
      state.view = button.dataset.view;
      render();
    });
  }
}

wireGate();
