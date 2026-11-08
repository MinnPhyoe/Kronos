// FX Signals: a phone-first view of the chart, the forecast and the signal in one glance.
// Talks only to our own API (the provider key stays on the server). Never places a trade.
import { FxChart } from "./chart.js";

const TF_SEC = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };
const TF_LABEL = { "1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H" };
const ACTIVE = new Set(["waiting", "triggered"]);

const store = {
  get(k, d) { try { const v = localStorage.getItem("fx." + k); return v === null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("fx." + k, v); } catch (e) {} },
};

const S = {
  pairs: [], meta: {}, pair: store.get("pair", "EURUSD"), tf: store.get("tf", "5m"), settings: null,
  health: null, signals: new Map(), sessions: [], forecast: null, ws: null, wsDelay: 1000, marketTime: null,
  view: store.get("view", "chart"),
};

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---------------------------------------------------------------- API
async function api(path, opts = {}) {
  const token = store.get("token", "");
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (token) headers.Authorization = "Bearer " + token;
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(`${r.status}: ${msg}`);
  }
  return r.json();
}

// ---------------------------------------------------------------- formatting
function digits(pair) { return (S.meta[pair] || {}).digits ?? 5; }
function px(pair, x) { return x == null ? "–" : Number(x).toFixed(digits(pair)); }
function pct(x) { return x == null ? "–" : Math.round(100 * x) + "%"; }
function rr(x) { return x == null ? "–" : (x > 0 ? "+" : "") + Number(x).toFixed(2) + "R"; }
function timeOf(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function dayTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
}
function inMinutes(tSec) {
  const now = S.marketTime || Date.now() / 1000;
  const m = Math.round((tSec - now) / 60);
  if (m <= 0) return "now";
  if (m < 60) return `in ${m} min`;
  return `in ${Math.floor(m / 60)} h ${m % 60} min`;
}
const ICON_OK = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M4.8 8.2l2.2 2.2 4.2-4.6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_NO = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
const ICON_WARN = '<svg class="ic" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5l7 13H1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M8 6v4M8 12v.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

function verdict(v) {
  if (v === "PASS") return `<span class="verdict pass">${ICON_OK}PASS</span>`;
  return `<span class="verdict block">${ICON_NO}BLOCK</span>`;
}

// ---------------------------------------------------------------- chart
const chart = new FxChart($("#chart"), $("#legend"), $("#chart-key"));
const media = window.matchMedia("(prefers-color-scheme: dark)");
media.addEventListener("change", () => chart.applyTheme());

async function loadChart() {
  const pair = S.pair, tf = S.tf;
  chart.setInstrument(digits(pair), TF_SEC[tf]);
  try {
    const [c, f, s] = await Promise.all([
      api(`/candles?pair=${pair}&tf=${tf}&limit=500`),
      api(`/forecast?pair=${pair}&tf=${tf}`),
      api(`/sessions?pair=${pair}`),
    ]);
    if (pair !== S.pair || tf !== S.tf) return;
    chart.setCandles(c.candles);
    S.forecast = f.available ? f : null;
    chart.setForecast(S.forecast);
    S.sessions = s.sessions;
    chart.setRanges(tf === "1h" ? [] : recentSessions(s.sessions));
    chart.fit(tf === "1m" ? 150 : 110);
  } catch (e) {
    toast("Could not load the chart", e.message);
  }
  renderSignalSlot();
  renderBanners();
}

// only today's boxes: older ranges scroll off and would only clutter the key
function recentSessions(list) {
  const now = S.marketTime || Date.now() / 1000;
  return (list || []).filter((x) => x.range && x.expiry > now - 10 * 3600);
}

// ---------------------------------------------------------------- signal card
function currentSignal() {
  const mine = [...S.signals.values()].filter((s) => s.pair === S.pair);
  const active = mine.filter((s) => ACTIVE.has(s.status) && s.shown);
  if (active.length) return active.sort((a, b) => (b.created_ts || 0) - (a.created_ts || 0))[0];
  return mine.sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""))[0] || null;
}

function levelBox(name, color, value, sub) {
  return `<div class="level"><div class="k"><i style="border-color:${color}"></i>${name}</div><div class="v">${value}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
}

function filterTable(sig) {
  const rows = (sig.filters || []).map((f) =>
    `<tr><td>${esc(f.name)}</td><td><span class="${f.ok ? "ok-yes" : "ok-no"}">${f.ok ? "pass" : "fail"}</span> · ${esc(f.detail)}</td></tr>`).join("");
  const tm = sig.timemachine || {};
  const k = sig.kronos || {};
  const extra = [
    k.paths != null ? `<tr><td>Forecast paths</td><td>${k.tp_first} TP1 first · ${k.sl_first} SL first · ${k.neither} neither (of ${k.entered} entered, ${k.paths} paths)</td></tr>` : "",
    tm.replays ? `<tr><td>Time Machine</td><td>${esc(tm.verdict)} ${tm.replays} replays, seed ${tm.seed}.</td></tr>` : "",
    sig.sizing && sig.sizing.limits ? `<tr><td>Size</td><td>${sig.lots} lots, bound by ${esc(sig.sizing.bound_by)} · ${sig.sizing.max_loss} at the stop</td></tr>` : "",
    sig.model && sig.model.name ? `<tr><td>Model</td><td>${esc(sig.model.name)}</td></tr>` : "",
    sig.plan && sig.plan.notes && sig.plan.notes.length ? `<tr><td>Stop</td><td>${esc(sig.plan.stop_rule)}; ${esc(sig.plan.notes.join("; "))}</td></tr>` : "",
  ].join("");
  return `<details class="more"><summary>Why this signal</summary><table>${rows}${extra}</table></details>`;
}

function signalCard(sig, { compact = false } = {}) {
  const p = sig.pair;
  const side = sig.side ? `<span class="chip ${sig.side}">${sig.side}</span>` : "";
  const status = `<span class="chip status-${sig.status}">${sig.status}</span>`;
  const head = `<div class="card-head"><span class="pair">${esc(p)}</span>${side}<span class="setup">${esc(sig.setup || "")}</span><span class="spacer"></span>${status}</div>`;
  if (sig.status === "expired") {
    return `<div class="card">${head}<p class="reason">No 5m close beyond the range before ${timeOf(sig.expires_at || sig.updated_at)}. The setup expired.</p></div>`;
  }
  if (!sig.passed && sig.status === "filtered" && sig.sl == null) {
    return `<div class="card">${head}<p class="reason">Filtered: not shown as a trade.</p><ul class="reasons">${(sig.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>${compact ? "" : filterTable(sig)}</div>`;
  }
  const pip = sig.pip || 0.0001;
  const tpSub = (x) => x == null ? "" : `${(Math.abs(x - sig.entry) / pip).toFixed(1)} pips`;
  const when = sig.status === "triggered" ? `Triggered ${timeOf(sig.trigger && sig.trigger.time)}`
    : sig.status === "waiting" ? `Waiting for a 5m close ${sig.side === "buy" ? "above" : "below"} ${px(p, sig.side === "buy" ? sig.range.high : sig.range.low)} · expires ${timeOf(sig.expires_at)}`
    : sig.outcome ? `Closed: ${sig.outcome.toUpperCase()} ${rr(sig.outcome_r)}` : "";
  const levels = `<div class="levels">
      ${levelBox("Entry", "var(--entry)", px(p, sig.entry), sig.status === "waiting" ? "planned" : "")}
      ${levelBox("SL", "var(--stop)", px(p, sig.sl), sig.sl_pips != null ? sig.sl_pips + " pips" : "")}
      ${levelBox("TP1", "var(--target)", px(p, sig.tp1), tpSub(sig.tp1))}
      ${levelBox("TP2", "var(--target)", px(p, sig.tp2), tpSub(sig.tp2))}
    </div>`;
  const tiles = `<div class="tiles">
      <div class="tile"><div class="k">Kronos confidence</div><div class="v">${pct(sig.kronos_confidence)}</div></div>
      <div class="tile"><div class="k">Time Machine odds</div><div class="v">${pct(sig.timemachine_odds)}</div></div>
      <div class="tile"><div class="k">Expectancy</div><div class="v">${rr(sig.expectancy_r)}</div></div>
      <div class="tile"><div class="k">Risk check</div><div class="v">${verdict(sig.risk_check)}</div></div>
      <div class="tile"><div class="k">Lots</div><div class="v">${sig.lots ?? "–"} <small>${sig.sizing && sig.sizing.max_loss != null ? "risk " + sig.sizing.max_loss : ""}</small></div></div>
      <div class="tile"><div class="k">Spread</div><div class="v">${sig.spread_pips ?? "–"} <small>pips</small></div></div>
    </div>`;
  const reason = sig.explanation ? `<p class="reason">${esc(sig.explanation)}</p>`
    : !sig.passed ? `<ul class="reasons">${(sig.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "";
  const canAct = sig.passed && ACTIVE.has(sig.status);
  const acted = sig.user_action;
  const actions = compact || !(canAct || acted) ? "" : `<div class="actions">
      <button class="btn primary" data-act="took" data-id="${esc(sig.id)}" aria-pressed="${acted === "took"}" ${acted ? "disabled" : ""}>I took this trade</button>
      <button class="btn" data-act="skipped" data-id="${esc(sig.id)}" aria-pressed="${acted === "skipped"}" ${acted ? "disabled" : ""}>Skip</button></div>`;
  return `<div class="card">${head}<div class="when">${esc(when)}${sig.late_minutes ? ` · ${sig.late_minutes} min late` : ""}</div>${levels}${tiles}${reason}${actions}
    ${compact ? "" : filterTable(sig)}<p class="disclaimer">${esc(sig.disclaimer || "Analysis and education, not financial advice.")}</p></div>`;
}

function nextSessionText() {
  const now = S.marketTime || Date.now() / 1000;
  const upcoming = (S.sessions || []).filter((s) => s.range_start > now).sort((a, b) => a.range_start - b.range_start)[0];
  const forming = (S.sessions || []).find((s) => s.range_start <= now && now < s.range_end);
  if (forming) return `The ${esc(forming.label.replace(" ORB", ""))} opening range is forming until ${timeOf(forming.range_end_iso)}.`;
  if (upcoming) return `Next: ${esc(upcoming.label.replace(" ORB", ""))} range ${timeOf(upcoming.range_start_iso)}–${timeOf(upcoming.range_end_iso)} (${inMinutes(upcoming.range_start)}).`;
  return "No session range left today.";
}

function renderSignalSlot() {
  const sig = currentSignal();
  chart.setSignal(sig && ACTIVE.has(sig.status) && sig.shown ? sig : (sig && sig.status === "closed" ? sig : null));
  const slot = $("#signal-slot");
  if (!sig) {
    slot.innerHTML = `<div class="card empty"><b>No setup on ${esc(S.pair)} yet</b>${nextSessionText()}</div>`;
    return;
  }
  slot.innerHTML = signalCard(sig) + (ACTIVE.has(sig.status) ? "" : `<p class="muted small" style="margin:10px 2px 0">${nextSessionText()}</p>`);
}

// ---------------------------------------------------------------- lists
function renderSignalsList() {
  const active = [...S.signals.values()].filter((s) => ACTIVE.has(s.status) && s.shown)
    .sort((a, b) => (b.created_ts || 0) - (a.created_ts || 0));
  const badge = $("#badge");
  badge.hidden = !active.length;
  badge.textContent = active.length;
  $("#signals-list").innerHTML = active.length ? active.map((s) => `<button class="row" data-open="${esc(s.pair)}">
      <div class="main"><div class="t">${esc(s.pair)} · ${esc(s.side)} · ${esc(s.status)}</div>
      <div class="d">${esc(s.setup)} · entry ${px(s.pair, s.entry)} · SL ${px(s.pair, s.sl)} · TP1 ${px(s.pair, s.tp1)}</div></div>
      <div class="r">${pct(s.kronos_confidence)}</div></button>`).join("")
    : `<div class="card empty"><b>No active signals</b>Signals appear after a session's opening range, when every filter passes.</div>`;
}

async function renderScan() {
  try {
    const r = await api("/scan");
    $("#scan-list").innerHTML = r.results.length ? r.results.map((x) => {
      if (!x.usable) return `<div class="row"><div class="main"><div class="t">${esc(x.pair)} · ${esc(x.label)}</div><div class="d">${esc(x.reason)}</div></div></div>`;
      const e = x.entry;
      const d = e.status === "triggered" ? `closed ${e.side === "buy" ? "above" : "below"} at ${px(x.pair, e.price)} (${timeOf(e.trigger_time_iso)})`
        : e.status === "waiting" ? `${e.distance_pips ?? "–"} pips from the range` : e.status;
      return `<button class="row" data-open="${esc(x.pair)}"><div class="main"><div class="t">${esc(x.pair)} · ${esc(x.label)} · ${esc(e.status)}</div>
        <div class="d">range ${px(x.pair, x.range.low)}–${px(x.pair, x.range.high)} (${x.range.height_pips} pips) · ${esc(d)}</div></div></button>`;
    }).join("") : `<div class="muted small">No session range has formed yet today.</div>`;
  } catch (e) { $("#scan-list").innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
  try {
    const c = await api("/calendar?hours=48");
    $("#calendar-list").innerHTML = c.events.length ? c.events.filter((e) => e.impact === "High").slice(0, 30).map((e) =>
      `<div class="row"><div class="main"><div class="t">${esc(e.currency)} · ${esc(e.title)}</div><div class="d">${dayTime(e.time_iso)} · ${esc(e.impact)} impact${e.forecast ? " · forecast " + esc(e.forecast) : ""}</div></div></div>`).join("")
      : `<div class="muted small">${c.error ? esc(c.error) : "No high-impact events in the next 48 hours (or no calendar source configured)."}</div>`;
  } catch (e) { $("#calendar-list").innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
}

async function renderHistory() {
  const inc = $("#hist-filtered").checked;
  try {
    const r = await api(`/signals/history?limit=200&include_filtered=${inc}`);
    const st = r.stats || {};
    const keys = Object.keys(st).filter((k) => !k.startsWith("setup:")).sort((a, b) => (a === "all" ? -1 : b === "all" ? 1 : a.localeCompare(b)));
    $("#stats").innerHTML = keys.length ? `<table><thead><tr><th>Setup</th><th>Trades</th><th>Win rate</th><th>Expectancy</th><th>Total</th></tr></thead><tbody>${
      keys.map((k) => `<tr><td>${k === "all" ? "<b>All</b>" : esc(k)}</td><td>${st[k].trades}</td><td>${pct(st[k].win_rate)}</td><td class="${st[k].expectancy_r >= 0 ? "pos" : "neg"}">${rr(st[k].expectancy_r)}</td><td>${rr(st[k].total_r)}</td></tr>`).join("")
    }</tbody></table><p class="muted small">Tracked outcomes of ${inc ? "every signal, shown or filtered" : "shown signals"}: TP1, SL, or the time exit. Past results are not a promise.</p>`
      : `<div class="card empty"><b>No closed signals yet</b>Outcomes appear here once signals hit TP1, the stop, or the time exit.</div>`;
    $("#history-list").innerHTML = r.signals.map((s) => `<button class="row" data-open="${esc(s.pair)}">
        <div class="main"><div class="t">${esc(s.pair)} · ${esc(s.side || "–")} · ${esc(s.setup)}</div>
        <div class="d">${dayTime(s.created_at)} · ${esc(s.status)}${s.outcome ? " · " + esc(s.outcome.toUpperCase()) : ""}${s.user_action ? " · you " + esc(s.user_action) : ""}${!s.shown ? " · filtered" : ""}</div></div>
        <div class="r ${s.outcome_r > 0 ? "pos" : s.outcome_r < 0 ? "neg" : ""}">${s.outcome_r != null ? rr(s.outcome_r) : "–"}</div></button>`).join("");
  } catch (e) { $("#stats").innerHTML = `<div class="muted small">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- settings
const SESSION_NAMES = { london: "London", newyork: "New York", tokyo: "Tokyo (JPY pairs)" };
const ALL_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY"];

function renderSettings() {
  const s = S.settings;
  if (!s) return;
  const num = (k, label, step = "any") => `<label>${label}<input type="number" name="${k}" step="${step}" value="${esc(s[k])}" inputmode="decimal"></label>`;
  const paused = (s.paused_setups || []).map((p) => `<span class="check">${esc(p)} <button class="btn" type="button" data-resume="${esc(p)}" style="flex:none;min-height:30px;padding:2px 10px">Resume</button></span>`).join("");
  $("#settings-form").innerHTML = `
    ${num("account_balance", "Account size")}
    <label>Account currency<input name="account_currency" value="${esc(s.account_currency)}" maxlength="3"></label>
    ${num("max_loss_per_trade", "Max loss per trade")}
    ${num("max_daily_loss", "Daily loss limit")}
    ${num("max_trades_per_day", "Max trades per day", "1")}
    ${num("min_reward_risk", "Min reward:risk", "0.1")}
    ${num("max_lots", "Max lots", "0.01")}
    ${num("max_leverage", "Max leverage", "1")}
    <fieldset><legend>Pairs</legend><div class="checks">${ALL_PAIRS.map((p) => `<label class="check"><input type="checkbox" name="pairs" value="${p}" ${s.pairs.includes(p) ? "checked" : ""}>${p}</label>`).join("")}</div></fieldset>
    <fieldset><legend>Sessions</legend><div class="checks">${Object.entries(SESSION_NAMES).map(([k, v]) => `<label class="check"><input type="checkbox" name="sessions" value="${k}" ${s.sessions.includes(k) ? "checked" : ""}>${v}</label>`).join("")}</div></fieldset>
    ${num("news_blackout_before_min", "News blackout before (min)", "1")}
    ${num("news_blackout_after_min", "News blackout after (min)", "1")}
    <label class="check full"><input type="checkbox" name="alerts_enabled" ${s.alerts_enabled ? "checked" : ""}> Alerts (setup, trigger, TP/SL, news)</label>
    ${paused ? `<fieldset><legend>Paused by the monitor</legend><div class="checks">${paused}</div></fieldset>` : ""}
    <button class="btn primary full" type="submit">Save settings</button>
    <p class="muted small full">Limits feed the risk check. The app sizes and checks; it never places a trade.</p>`;
}

async function saveSettings(ev) {
  ev.preventDefault();
  const fd = new FormData($("#settings-form"));
  const body = {};
  for (const k of ["account_balance", "max_loss_per_trade", "max_daily_loss", "min_reward_risk", "max_lots", "max_leverage"]) body[k] = parseFloat(fd.get(k));
  for (const k of ["max_trades_per_day", "news_blackout_before_min", "news_blackout_after_min"]) body[k] = parseInt(fd.get(k), 10);
  body.account_currency = String(fd.get("account_currency") || "USD").toUpperCase();
  body.pairs = fd.getAll("pairs");
  body.sessions = fd.getAll("sessions");
  body.alerts_enabled = fd.get("alerts_enabled") === "on";
  try {
    S.settings = await api("/settings", { method: "PUT", body: JSON.stringify(body) });
    toast("Saved", "Your limits apply to the next signal.");
    await loadPairs();
  } catch (e) { toast("Not saved", e.message); }
}

// ---------------------------------------------------------------- health and banners
function renderBanners() {
  const h = S.health;
  const out = [];
  if (h) {
    if (!h.model_up) out.push(["crit", "<b>Signals unavailable.</b> The forecast model is offline; candles keep streaming."]);
    if (h.data_delayed && h.data_delayed[S.pair]) out.push(["warn", `<b>Data delayed.</b> No new ${esc(S.pair)} candle for over 3 minutes.`]);
    const demoModel = h.config && h.config.model_backend === "bootstrap";
    const sim = h.config && h.config.provider === "simulated";
    if (sim || demoModel) out.push(["warn", `<b>Demo.</b> ${sim ? "Simulated prices, not real quotes. " : ""}${demoModel ? "Forecasts from a statistical stand-in, not Kronos." : ""}`]);
  }
  if (S.forecast && S.forecast.stale) out.push(["warn", "The forecast is older than the last candle."]);
  $("#banners").innerHTML = out.map(([k, m]) => `<div class="banner ${k}">${ICON_WARN}<span>${m}</span></div>`).join("");
}

function setStatus(kind, text) {
  const el = $("#status");
  el.className = "status " + kind;
  el.querySelector(".status-text").textContent = text;
}

function renderHealth() {
  const h = S.health;
  if (!h) return;
  const svc = h.services || {};
  const row = (k, v) => `<tr><td>${k}</td><td>${v}</td></tr>`;
  $("#health-box").innerHTML = `<table>
    ${row("Data feed", h.feed_up ? `up (${esc(h.config.provider)})` : "down")}
    ${row("Forecast model", h.model_up ? esc((svc.model || {}).backend || h.config.model_backend) : "offline")}
    ${row("Last model run", svc.model && svc.model.last_run_ms ? Math.round(svc.model.last_run_ms) + " ms" : "–")}
    ${row("Market time", h.market_time ? dayTime(h.market_time) : "–")}
    ${row("Forecast", `${h.config.paths} paths, ${h.config.horizon} candles ahead`)}
    ${row("Min confidence", pct(h.config.min_confidence))}
  </table><p class="muted small">Analysis and education, not financial advice. Signals are decision support: you choose every trade.</p>`;
}

async function refreshHealth() {
  try {
    S.health = await api("/health");
    if (S.health.market_time) S.marketTime = Date.parse(S.health.market_time) / 1000;
  } catch (e) { S.health = null; }
  renderBanners();
  renderHealth();
}

// ---------------------------------------------------------------- live stream
function connect() {
  const token = store.get("token", "");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/stream${token ? "?token=" + encodeURIComponent(token) : ""}`);
  S.ws = ws;
  ws.onopen = () => { S.wsDelay = 1000; setStatus("live", "Live"); };
  ws.onclose = () => {
    setStatus("down", "Reconnecting");
    setTimeout(connect, S.wsDelay);
    S.wsDelay = Math.min(30000, S.wsDelay * 2);
  };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    onMessage(m);
  };
}

let lastSlot = 0;
function onMessage(m) {
  switch (m.type) {
    case "tick":
      if (m.time) S.marketTime = Math.max(S.marketTime || 0, m.time);
      if (m.pair === S.pair) chart.tick(m.time, m.bid);
      break;
    case "candle":
      if (m.pair === S.pair && m.tf === S.tf && m.closed) chart.closedCandle(m.candle);
      if (m.pair === S.pair && m.tf === "5m" && m.closed) {
        // session boxes and the "next session" line move on with each 5m close
        if (Date.now() - lastSlot > 3000) { lastSlot = Date.now(); api(`/sessions?pair=${S.pair}`).then((s) => { S.sessions = s.sessions; if (S.tf !== "1h") chart.setRanges(recentSessions(s.sessions)); renderSignalSlot(); }).catch(() => {}); }
      }
      break;
    case "forecast":
      if (m.forecast.pair === S.pair && m.forecast.tf === S.tf) {
        S.forecast = m.forecast;
        chart.setForecast(m.forecast);
        renderBanners();
      }
      break;
    case "signal":
      S.signals.set(m.signal.id, m.signal);
      if (m.signal.pair === S.pair) renderSignalSlot();
      renderSignalsList();
      if (S.view === "history" && !ACTIVE.has(m.signal.status)) renderHistory();
      break;
    case "alert":
      onAlert(m);
      break;
    case "status":
      if (m.service === "model" && S.health && S.health.model_up !== m.ok) { S.health.model_up = m.ok; renderBanners(); }
      if (m.service === "feed" && m.market_time) S.marketTime = m.market_time;
      break;
  }
}

function toast(title, body) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<b>${esc(title)}</b>${esc(body || "")}`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

function onAlert(m) {
  if (S.settings && !S.settings.alerts_enabled) return;
  toast(m.title, m.body);
  if ("Notification" in window && Notification.permission === "granted" && document.visibilityState !== "visible") {
    try { new Notification(m.title, { body: m.body, tag: m.key, icon: "/static/icon.svg" }); } catch (e) {}
  }
}

// ---------------------------------------------------------------- navigation and wiring
function show(view) {
  S.view = view;
  store.set("view", view);
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "signals") { renderSignalsList(); renderScan(); }
  if (view === "history") renderHistory();
  if (view === "settings") { renderSettings(); renderHealth(); }
  if (view === "chart") requestAnimationFrame(() => chart.chart.timeScale().scrollToRealTime());
}

function openPair(pair) {
  S.pair = pair;
  store.set("pair", pair);
  $("#pair").value = pair;
  show("chart");
  loadChart();
}

function renderTf() {
  $("#tf").innerHTML = Object.keys(TF_SEC).map((t) => `<button type="button" role="tab" data-tf="${t}" class="${t === S.tf ? "active" : ""}" aria-selected="${t === S.tf}">${TF_LABEL[t]}</button>`).join("");
}

async function loadPairs() {
  const r = await api("/pairs");
  S.pairs = r.pairs.map((p) => p.pair);
  S.meta = Object.fromEntries(r.pairs.map((p) => [p.pair, p]));
  if (!S.pairs.includes(S.pair)) S.pair = S.pairs[0] || "EURUSD";
  $("#pair").innerHTML = S.pairs.map((p) => `<option value="${p}" ${p === S.pair ? "selected" : ""}>${p.slice(0, 3)}/${p.slice(3)}</option>`).join("");
}

function wire() {
  $("#pair").addEventListener("change", (e) => openPair(e.target.value));
  $("#tf").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tf]");
    if (!b) return;
    S.tf = b.dataset.tf;
    store.set("tf", S.tf);
    renderTf();
    loadChart();
  });
  document.querySelector(".tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (b) show(b.dataset.view);
  });
  document.body.addEventListener("click", async (e) => {
    const open = e.target.closest("[data-open]");
    if (open) { openPair(open.dataset.open); return; }
    const act = e.target.closest("button[data-act]");
    if (act) {
      act.disabled = true;
      try {
        const s = await api(`/signals/${encodeURIComponent(act.dataset.id)}/action`, { method: "POST", body: JSON.stringify({ action: act.dataset.act }) });
        S.signals.set(s.id, s);
        renderSignalSlot();
        toast(act.dataset.act === "took" ? "Logged: you took it" : "Logged: skipped", "It counts in your results log.");
      } catch (err) { act.disabled = false; toast("Not logged", err.message); }
      return;
    }
    const res = e.target.closest("button[data-resume]");
    if (res && S.settings) {
      try {
        S.settings = await api("/settings", { method: "PUT", body: JSON.stringify({ paused_setups: S.settings.paused_setups.filter((x) => x !== res.dataset.resume) }) });
        renderSettings();
      } catch (err) { toast("Not saved", err.message); }
    }
  });
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#hist-filtered").addEventListener("change", renderHistory);
  $("#status").addEventListener("click", () => show("settings"));
  const theme = $("#theme");
  theme.value = store.get("theme", "system");
  theme.addEventListener("change", () => {
    store.set("theme", theme.value);
    if (theme.value === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme.value;
    chart.applyTheme();
  });
  const tok = $("#token");
  tok.value = store.get("token", "");
  tok.addEventListener("change", () => { store.set("token", tok.value.trim()); location.reload(); });
  const nb = $("#notify-btn");
  const ns = () => { $("#notify-state").textContent = !("Notification" in window) ? "This browser has no notifications; alerts show inside the app." : `Notifications: ${Notification.permission}.`; };
  ns();
  nb.addEventListener("click", async () => { if ("Notification" in window) { await Notification.requestPermission(); ns(); } });
}

async function init() {
  wire();
  renderTf();
  try {
    await loadPairs();
    S.settings = await api("/settings");
  } catch (e) {
    toast("Cannot reach the server", e.message);
    if (String(e.message).startsWith("401")) show("settings");
  }
  await refreshHealth();
  try {
    const [a, h] = await Promise.all([api("/signals?status=active&limit=200"), api("/signals?status=all&include_filtered=true&limit=200")]);
    for (const s of h.signals) S.signals.set(s.id, s);
    for (const s of a.signals) S.signals.set(s.id, s);
  } catch (e) {}
  show(S.view);
  await loadChart();
  renderSignalsList();
  connect();
  setInterval(refreshHealth, 15000);
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
}

init();
