// ADR-70: the read-only demo viewer. Plain ES module, no build step, no dependencies.
//
// Same origin as the API through the demo-viewer proxy (demo/nginx.conf):
//   POST /token                                   FAKE token issuer: a persona's bearer token
//   POST /v1/incidents                            start the reference incident (the only write)
//   GET  /v1/incidents/{id}                       header, status, severity, spend (budget)
//   GET  /v1/incidents/{id}/evidence              evidence list (paged by cursor)
//   GET  /v1/incidents/{id}/diagnoses/current     diagnosis, hypotheses, citations (404: none yet)
//   GET  /v1/incidents/{id}/timeline/stream       live timeline, SSE, read with fetch so the
//                                                 Authorization header can be sent
//   GET  /v1/incidents/{id}/actions               the incident's fixes, open or ended (a Release A
//                                                 fix ends `withdrawn`, so open_action_ids drops it)
//   GET  /v1/artifacts/{id}/content               a fix's patch, its canonical diff (patch_diff only)
//   GET  /readyz                                  the system-health box (no token)
//
// SECURITY: every value from the API (evidence, model output, titles, IDs) is untrusted. It is
// put on the page only through textContent and createElement (see el()), never parsed as HTML,
// and never used as a link target. The one link this page builds (Temporal) is made from a
// UUID that matched UUID_RE and an integer run number.

import { confidenceRing, icon } from "./icons.js";

const TOKEN_TTL_S = 300; // the fake issuer's default; the API re-checks streams every 5 minutes
const REFRESH_EARLY_S = 60; // mint a new token this long before the old one expires
const RETRY_DEFAULT_MS = 3000; // the stream's own `retry:` replaces it
const RETRY_MAX_MS = 30000;
const HEALTH_EVERY_MS = 15000;
const AGE_EVERY_MS = 30000;
const FINISHED = new Set(["closed", "cancelled"]);
const SETTLED = new Set(["handed_over", "closed", "cancelled"]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// The local Temporal UI (compose.yaml: temporal-ui on 127.0.0.1:${AISRE_TEMPORAL_UI_PORT:-8233})
// and namespace (.env.example: AISRE_TEMPORAL_NAMESPACE=default). Workflow IDs follow
// aisre.record.incidents.workflow_id: incident-<id>, or incident-<id>-r<run> for a re-run.
const TEMPORAL_UI = "http://127.0.0.1:8233";
const TEMPORAL_NAMESPACE = "default";
const RECENT_KEY = "icp-demo.recent";
const THEME_KEY = "icp-demo.theme";
const RECENT_MAX = 8;

const PERSONAS = [
  {
    id: "responder",
    label: "Demo responder · orders team (sees everything)",
    initials: "DR",
    name: "Demo responder",
    team: "orders team",
    sub: "demo-responder",
    groups: ["aisre:responder", "orders-team"],
  },
  {
    id: "responder-outside",
    label: "Demo responder · outside the orders team (runbooks restricted)",
    initials: "DO",
    name: "Demo responder",
    team: "outside orders team",
    sub: "demo-responder-outside",
    groups: ["aisre:responder"],
  },
  {
    id: "viewer",
    label: "Demo viewer · orders team (read only, cannot start)",
    initials: "DV",
    name: "Demo viewer",
    team: "orders team · read only",
    sub: "demo-viewer",
    groups: ["aisre:viewer", "orders-team"],
  },
];

// ADR-53's tiers: what the observation is, and its class.
const TIERS = {
  1: ["Reproduction", "mech"],
  2: ["Version split", "mech"],
  3: ["Config match", "mech"],
  4: ["Code match", "supp"],
  5: ["New error signature", "supp"],
  6: ["Scale match with rollout", "supp"],
  7: ["Rollback clears it", "supp"],
  8: ["Timing alone", "weak"],
};

// Evidence kinds: the chip they filter under, a readable name, and an icon.
const KINDS = {
  metric: { chip: "Metrics", name: "Metric", icon: "metric" },
  log: { chip: "Logs", name: "Logs", icon: "log" },
  trace: { chip: "Traces", name: "Trace", icon: "trace" },
  change: { chip: "Changes", name: "Change", icon: "change" },
  alert: { chip: "Alerts", name: "Alert", icon: "alert" },
  error_group: { chip: "Errors", name: "Error group", icon: "error_group" },
  dependency: { chip: "Dependencies", name: "Dependency", icon: "dependency" },
  runbook: { chip: "Runbooks", name: "Runbook", icon: "runbook" },
  past_incident: { chip: "Runbooks", name: "Past incident", icon: "past_incident" },
  gap: { chip: "Gaps", name: "Gap", icon: "gap" },
};
const CHIP_ORDER = ["Metrics", "Logs", "Traces", "Changes", "Alerts", "Errors", "Dependencies", "Runbooks", "Gaps"];

const STATUS_LABEL = {
  open: "Open",
  investigating: "Investigating",
  diagnosed: "Diagnosed",
  inconclusive: "Inconclusive",
  testing: "Testing a fix",
  awaiting_approval: "Awaiting approval",
  executing: "Executing",
  reconciling: "Reconciling",
  verifying: "Verifying",
  recovered: "Recovered",
  not_recovered: "Not recovered",
  handed_over: "Handed over",
  cancelled: "Cancelled",
  closed: "Closed",
};

const STATUS_TONE = {
  open: "accent",
  investigating: "accent",
  diagnosed: "ok",
  inconclusive: "warn",
  handed_over: "ok",
  recovered: "ok",
  not_recovered: "bad",
  cancelled: "bad",
  closed: "",
};

const SEVERITY = {
  sev1: ["Sev 1", "bad"],
  sev2: ["Sev 2", "bad"],
  sev3: ["Sev 3", "warn"],
  sev4: ["Sev 4", "neutral"],
};

const CONF_LEVEL = { low: 1, medium: 2, high: 3 };

// Readable names for the timeline (Artifact 7.5's event types).
const EVENT_LABEL = {
  "aisre.incident.created.v1": "Incident opened",
  "aisre.incident.alert_attached.v1": "Alert attached",
  "aisre.incident.severity_proposed.v1": "Severity proposed",
  "aisre.incident.severity_confirmed.v1": "Severity confirmed",
  "aisre.incident.status_changed.v1": "Status changed",
  "aisre.incident.handed_over.v1": "Handed over to people",
  "aisre.incident.cancellation_requested.v1": "Cancellation requested",
  "aisre.incident.cancelled.v1": "Incident cancelled",
  "aisre.incident.closed.v1": "Incident closed",
  "aisre.incident.reinvestigation_requested.v1": "Re-investigation requested",
  "aisre.incident.note_added.v1": "Note added",
  "aisre.evidence.collected.v1": "Evidence collected",
  "aisre.evidence.gap_recorded.v1": "Evidence gap recorded",
  "aisre.evidence.injection_flagged.v1": "Possible instruction in evidence flagged",
  "aisre.evidence.content_removed.v1": "Evidence content removed",
  "aisre.hypothesis.created.v1": "Hypothesis proposed",
  "aisre.hypothesis.status_changed.v1": "Hypothesis updated",
  "aisre.diagnosis.produced.v1": "Diagnosis produced",
  "aisre.diagnosis.inconclusive.v1": "Diagnosis inconclusive",
  "aisre.diagnosis.reviewed.v1": "Diagnosis reviewed",
  "aisre.action.proposed.v1": "Fix drafted",
  "aisre.action.test_started.v1": "Fix test started",
  "aisre.action.tested.v1": "Fix tested",
  "aisre.action.review_opened.v1": "Draft pull request opened",
  "aisre.action.withdrawn.v1": "Fix withdrawn",
  "aisre.action.undone.v1": "Fix undone",
  "aisre.approval.requested.v1": "Approval requested",
  "aisre.approval.granted.v1": "Approval granted",
  "aisre.approval.rejected.v1": "Approval rejected",
  "aisre.approval.revoked.v1": "Approval revoked",
  "aisre.approval.refused.v1": "Approval refused",
  "aisre.approval.escalated.v1": "Approval escalated",
  "aisre.approval.expired.v1": "Approval expired",
  "aisre.approval.voided.v1": "Approval voided",
  "aisre.execution.started.v1": "Execution started",
  "aisre.execution.completed.v1": "Execution completed",
  "aisre.execution.refused.v1": "Execution refused",
  "aisre.execution.outcome_resolved.v1": "Execution outcome resolved",
  "aisre.verification.completed.v1": "Verification completed",
  "aisre.verification.accepted.v1": "Verification accepted",
  "aisre.limit.reached.v1": "Limit reached",
  "aisre.model.attempt_failed.v1": "Model attempt failed",
  "aisre.notification.failed.v1": "Notification failed",
};

// ------------------------------------------------------------------ DOM helpers (text only)

/** createElement with text and attributes. Children are nodes or strings (as text nodes). */
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.className) node.className = opts.className;
  if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
  for (const [k, v] of Object.entries(opts.attrs || {})) {
    if (v !== undefined && v !== null && v !== false) node.setAttribute(k, String(v));
  }
  for (const [k, v] of Object.entries(opts.data || {})) {
    if (v !== undefined && v !== null) node.dataset[k] = String(v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    node.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

const $ = (id) => document.getElementById(id);

function clear(node) {
  node.replaceChildren();
}

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function validDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "14:07" in UTC. */
function hhmm(iso) {
  const d = validDate(iso);
  return d ? d.toISOString().slice(11, 16) : "";
}

/** "14:07:03" in UTC. */
function hhmmss(iso) {
  const d = validDate(iso);
  return d ? d.toISOString().slice(11, 19) : "";
}

/** "Oct 20" in UTC. */
function monthDay(iso) {
  const d = validDate(iso);
  return d ? `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}` : "";
}

/** "14:07, Oct 20, 2026" in UTC. */
function longTime(iso) {
  const d = validDate(iso);
  return d ? `${hhmm(iso)}, ${monthDay(iso)}, ${d.getUTCFullYear()}` : String(iso || "");
}

/** HH:MM, with the date in front when it isn't the incident's opening day (replayed sources). */
function shortTime(iso) {
  const d = validDate(iso);
  if (!d) return "";
  const opened = view.incident && validDate(view.incident.created_at);
  if (opened && opened.toISOString().slice(0, 10) !== d.toISOString().slice(0, 10)) {
    return `${monthDay(iso)} ${hhmm(iso)}`;
  }
  return hhmm(iso);
}

function fmtFull(iso) {
  const d = validDate(iso);
  return d ? d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z") : "";
}

function ago(iso) {
  const d = validDate(iso);
  if (!d) return "";
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function fmtUsd(micros) {
  if (typeof micros !== "number") return "–";
  return "$" + (micros / 1e6).toFixed(micros < 1e6 ? 4 : 2);
}

function humanize(s) {
  return String(s || "").replace(/_/g, " ");
}

function capital(s) {
  const t = String(s || "");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

function firstLine(text) {
  const line = String(text || "")
    .split(/\r?\n/)
    .map((l) => l.trim())
    .find((l) => l.length);
  return line || "";
}

function showMessage(text, kind = "info") {
  const m = $("message");
  m.textContent = text;
  m.dataset.kind = kind;
  m.hidden = !text;
}

// ------------------------------------------------------------------ browser storage (best effort)

function storeGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storeSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* private window or blocked storage: the page works without it */
  }
}

// ------------------------------------------------------------------ theme

const THEMES = ["system", "light", "dark"];
let theme = "system";

function applyTheme(next) {
  theme = THEMES.includes(next) ? next : "system";
  if (theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  const button = $("theme");
  const label = { system: "system", light: "light", dark: "dark" }[theme];
  button.setAttribute("aria-label", `Theme: ${label}. Switch theme`);
  button.title = `Theme: ${label}`;
  $("theme-icon").replaceChildren(icon({ system: "monitor", light: "sun", dark: "moon" }[theme]));
}

function initTheme() {
  applyTheme(storeGet(THEME_KEY) || "system");
  $("theme").addEventListener("click", () => {
    const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    applyTheme(next);
    storeSet(THEME_KEY, next);
  });
}

// ------------------------------------------------------------------ system health (/readyz)

async function checkHealth() {
  const box = $("health");
  let state = "bad";
  let title = "API unreachable";
  let detail = "The proxy could not reach the API.";
  try {
    const res = await fetch("/readyz", { cache: "no-store", headers: { Accept: "application/json" } });
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    if (res.ok && body && body.status === "ready") {
      state = "ok";
      title = "System healthy";
      detail = "API ready: the Incident Record answers";
    } else if (res.status === 503) {
      state = "warn";
      title = "Not ready";
      detail = (body && (body.detail || body.title)) || "The API reports it is not ready.";
    } else {
      detail = `GET /readyz answered HTTP ${res.status}.`;
    }
  } catch (e) {
    detail = e.message || detail;
  }
  box.dataset.state = state;
  $("health-title").textContent = title;
  $("health-detail").textContent = detail;
}

// ------------------------------------------------------------------ recent incidents (this browser)

function loadRecent() {
  try {
    const parsed = JSON.parse(storeGet(RECENT_KEY) || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((r) => r && typeof r.id === "string" && UUID_RE.test(r.id))
      .map((r) => ({
        id: r.id.toLowerCase(),
        ref: String(r.ref || ""),
        title: String(r.title || ""),
        status: String(r.status || ""),
      }))
      .slice(0, RECENT_MAX);
  } catch {
    return [];
  }
}

let recent = loadRecent();

function remember(inc) {
  const entry = { id: inc.incident_id, ref: inc.display_ref, title: inc.title, status: inc.status };
  const existing = recent.find((r) => r.id === entry.id);
  if (existing && existing.ref === entry.ref && existing.title === entry.title && existing.status === entry.status) {
    return;
  }
  recent = [entry, ...recent.filter((r) => r.id !== entry.id)].slice(0, RECENT_MAX);
  storeSet(RECENT_KEY, JSON.stringify(recent));
  renderRecent();
}

function recentItem(r, wide) {
  const button = el(
    "button",
    {
      className: "recent-btn",
      attrs: { type: "button", "aria-current": r.id === view.id ? "true" : null, title: r.title },
    },
    [
      el("span", { className: "recent-ref", text: r.ref || r.id.slice(0, 8) }),
      wide ? el("span", { className: "recent-title", text: r.title }) : null,
      el("span", { className: "pill sm", data: { tone: STATUS_TONE[r.status] || "" }, text: STATUS_LABEL[r.status] || r.status }),
    ],
  );
  button.addEventListener("click", () => {
    closeSidebar();
    openIncident(r.id);
  });
  return el("li", {}, [button]);
}

function renderRecent() {
  $("recent-count").textContent = String(recent.length);
  const list = $("recent");
  clear(list);
  if (!recent.length) list.append(el("li", { className: "recent-empty", text: "None yet. Start or open one." }));
  for (const r of recent) list.append(recentItem(r, false));
  const wide = $("empty-recent");
  clear(wide);
  for (const r of recent) wide.append(recentItem(r, true));
  $("empty-recent-wrap").hidden = !recent.length;
}

// ------------------------------------------------------------------ sign-in

const auth = {
  persona: PERSONAS[0],
  token: null,
  expiresAt: 0,
  timer: null,
  minting: null,
};

async function mintToken() {
  if (auth.minting) return auth.minting;
  auth.minting = (async () => {
    const p = auth.persona;
    const res = await fetch("/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sub: p.sub, groups: p.groups, ttl_seconds: TOKEN_TTL_S }),
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`token issuer answered ${res.status}`);
    const body = await res.json();
    if (typeof body.access_token !== "string") throw new Error("no access_token in the answer");
    const ttl = Number(body.expires_in) || TOKEN_TTL_S;
    auth.token = body.access_token;
    auth.expiresAt = Date.now() + ttl * 1000;
    clearTimeout(auth.timer);
    auth.timer = setTimeout(onTokenRefresh, Math.max(5, ttl - REFRESH_EARLY_S) * 1000);
    renderTokenState(true);
    return auth.token;
  })();
  try {
    return await auth.minting;
  } catch (e) {
    renderTokenState(false, e.message);
    throw e;
  } finally {
    auth.minting = null;
  }
}

async function onTokenRefresh() {
  try {
    await mintToken();
    // The open stream was authorised with the old token, and the API re-checks it every five
    // minutes: start a new one with the new token, resuming after the last event seen.
    if (view.id) stream.restart("token refreshed");
  } catch {
    auth.timer = setTimeout(onTokenRefresh, 5000);
  }
}

function renderTokenState(ok, why) {
  const s = $("token-state");
  if (!ok) {
    s.textContent = `Sign-in failed: ${why || "unknown error"}`;
    s.dataset.ok = "false";
    return;
  }
  const left = Math.max(0, Math.round((auth.expiresAt - Date.now()) / 1000));
  s.textContent = `Token valid ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} · ${auth.persona.groups.join(", ")}`;
  s.dataset.ok = "true";
}

async function token() {
  if (!auth.token || Date.now() > auth.expiresAt - 10_000) await mintToken();
  return auth.token;
}

class ApiProblem extends Error {
  constructor(status, problem) {
    const title = (problem && (problem.title || problem.error)) || `HTTP ${status}`;
    const detail = problem && problem.detail ? `: ${problem.detail}` : "";
    super(`${title}${detail}`);
    this.status = status;
    this.problem = problem;
  }
}

/** A JSON call to the API with the persona's token. One retry with a fresh token on 401. */
async function api(path, { method = "GET", body, headers = {} } = {}, retried = false) {
  const res = await fetch(path, {
    method,
    headers: { Authorization: `Bearer ${await token()}`, Accept: "application/json", ...headers },
    body,
    cache: "no-store",
  });
  if (res.status === 401 && !retried) {
    auth.token = null;
    return api(path, { method, body, headers }, true);
  }
  let parsed = null;
  try {
    parsed = await res.json();
  } catch {
    parsed = null;
  }
  if (!res.ok) throw new ApiProblem(res.status, parsed);
  return { status: res.status, body: parsed };
}

// ------------------------------------------------------------------ incident state

const view = {
  id: null,
  incident: null,
  events: new Map(), // sequence -> CloudEvent (deduplicated)
  lastSeq: 0,
  historySeq: 0, // events up to here were already there when the incident was opened
  evidence: [], // EvidenceRecord, in the API's order
  evidenceLabel: new Map(), // evidence_id -> "E3"
  diagnosis: null,
  diagnosisLoaded: false,
  actions: new Map(), // action_id -> ActionView
  actionsKey: "",
  actionEvents: 0, // aisre.action.* events seen: the actions are read again when it moves
  diffs: new Map(), // diff_artifact_id -> {files} (Patch.canonical()) or {error}
  hypothesesFromEvents: new Map(), // hypothesis_id -> data, until a diagnosis lists them
  statusTimes: new Map(), // status -> time it was entered, from the timeline
  run: 1, // the investigation run, from re-investigation events
  liveState: "idle",
};

// UI state that survives re-renders.
const ui = {
  tab: "diagnosis",
  kindChip: "All",
  query: "",
  openEvidence: new Set(),
  openHyps: new Set(),
  openRunbooks: new Set(),
};

function resetView(id) {
  view.id = id;
  view.incident = null;
  view.events = new Map();
  view.lastSeq = 0;
  view.historySeq = 0;
  view.evidence = [];
  view.evidenceLabel = new Map();
  view.diagnosis = null;
  view.diagnosisLoaded = false;
  view.actions = new Map();
  view.actionsKey = "";
  view.actionEvents = 0;
  view.diffs = new Map();
  view.hypothesesFromEvents = new Map();
  view.statusTimes = new Map();
  view.run = 1;
  ui.kindChip = "All";
  ui.query = "";
  ui.openEvidence = new Set();
  ui.openHyps = new Set();
  ui.openRunbooks = new Set();
  $("ex-q").value = "";
  clear($("timeline"));
  renderAll();
}

function renderAll() {
  renderIncident();
  renderEvidence();
  renderDiagnosis();
  renderHypotheses();
  renderFix();
  renderLiveTimeline();
}

function showIncidentPane(show) {
  $("incident").hidden = !show;
  $("empty").hidden = show;
}

async function openIncident(id) {
  if (!UUID_RE.test(id)) {
    showMessage("That is not an incident ID (expected a UUID).", "error");
    return;
  }
  id = id.toLowerCase();
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
  stream.stop();
  resetView(id);
  showIncidentPane(true);
  renderRecent();
  try {
    await refreshIncident();
  } catch (e) {
    showIncidentPane(false);
    view.id = null;
    renderRecent();
    showMessage(
      e.status === 404
        ? "No such incident, or this persona may not read it (the API answers both the same way)."
        : `Could not read the incident: ${e.message}`,
      "error",
    );
    return;
  }
  view.historySeq = view.incident ? view.incident.last_event_seq : 0;
  await Promise.allSettled([refreshEvidence(), refreshDiagnosis()]);
  stream.start();
}

async function refreshIncident() {
  const { body } = await api(`/v1/incidents/${view.id}`);
  if (body.incident_id !== view.id) return;
  view.incident = body;
  remember(body);
  renderIncident();
  renderFix();
  renderLiveTimeline();
  // Every fix, open or ended: a Release A fix ends `withdrawn` and leaves open_action_ids.
  const key = `${body.status}|${body.version}|${view.actionEvents}|${(body.open_action_ids || []).join(",")}`;
  if (key !== view.actionsKey) {
    view.actionsKey = key;
    refreshActions().catch((e) => console.warn("actions read failed", e));
  }
}

async function refreshActions() {
  const id = view.id;
  const { body } = await api(`/v1/incidents/${id}/actions`);
  if (id !== view.id) return;
  const found = new Map();
  for (const a of Array.isArray(body) ? body : []) {
    if (UUID_RE.test(String(a.action_id))) found.set(String(a.action_id), a);
  }
  view.actions = found;
  renderFix();
  for (const a of found.values()) {
    const diffId = a.patch && String(a.patch.diff_artifact_id);
    if (!diffId || !UUID_RE.test(diffId) || view.diffs.has(diffId)) continue;
    view.diffs.set(diffId, { loading: true });
    try {
      const got = await api(`/v1/artifacts/${diffId}/content`);
      const files = got.body && Array.isArray(got.body.files) ? got.body.files : null;
      view.diffs.set(diffId, files ? { files } : { error: "not a patch" });
    } catch (e) {
      view.diffs.set(diffId, { error: e.message });
    }
    if (id !== view.id) return;
    renderFix();
  }
}

async function refreshEvidence() {
  const id = view.id;
  const items = [];
  let cursor = null;
  for (let page = 0; page < 20; page += 1) {
    const q = new URLSearchParams({ limit: "200" });
    if (cursor) q.set("cursor", cursor);
    const { body } = await api(`/v1/incidents/${id}/evidence?${q}`);
    items.push(...(body.items || []));
    cursor = body.next_cursor;
    if (!cursor) break;
  }
  if (id !== view.id) return;
  view.evidence = items;
  view.evidenceLabel = new Map(items.map((e, i) => [e.evidence_id, `E${i + 1}`]));
  renderEvidence();
  renderDiagnosis(); // citation labels may have changed
  renderHypotheses();
}

async function refreshDiagnosis() {
  const id = view.id;
  try {
    const { body } = await api(`/v1/incidents/${id}/diagnoses/current`);
    if (id !== view.id) return;
    view.diagnosis = body;
  } catch (e) {
    if (e.status !== 404) throw e;
    if (id !== view.id) return;
    view.diagnosis = null;
  }
  view.diagnosisLoaded = true;
  renderDiagnosis();
  renderHypotheses();
  renderIncident(); // the confidence in the meta row
}

// Refetches after events, coalesced so a burst of 16 evidence events is one read.
const pending = { incident: false, evidence: false, diagnosis: false, timer: null };
function scheduleRefresh(what) {
  pending[what] = true;
  if (pending.timer) return;
  pending.timer = setTimeout(async () => {
    pending.timer = null;
    const jobs = [];
    if (pending.incident) jobs.push(refreshIncident());
    if (pending.evidence) jobs.push(refreshEvidence());
    if (pending.diagnosis) jobs.push(refreshDiagnosis());
    pending.incident = pending.evidence = pending.diagnosis = false;
    const results = await Promise.allSettled(jobs);
    for (const r of results) if (r.status === "rejected") console.warn("refresh failed", r.reason);
  }, 350);
}

// ------------------------------------------------------------------ live timeline (SSE over fetch)

const stream = {
  controller: null,
  retryMs: RETRY_DEFAULT_MS,
  failures: 0,
  timer: null,
  ended: false,

  start() {
    this.stop();
    this.ended = false;
    this.failures = 0;
    this.connect();
  },

  stop() {
    clearTimeout(this.timer);
    this.timer = null;
    if (this.controller) this.controller.abort();
    this.controller = null;
  },

  restart(reason) {
    if (this.ended) return;
    console.info("stream restart:", reason);
    this.stop();
    this.connect();
  },

  scheduleReconnect(why) {
    if (this.ended || !view.id) return;
    this.failures += 1;
    const delay = Math.min(RETRY_MAX_MS, this.retryMs * 2 ** Math.min(this.failures - 1, 4));
    setLive("reconnecting", "Reconnecting", `Reconnecting in ${Math.round(delay / 1000)} s (${why})`);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.connect(), delay);
  },

  async connect() {
    const id = view.id;
    const controller = new AbortController();
    this.controller = controller;
    const headers = { Accept: "text/event-stream" };
    // Resume after the last event already shown; the API replays from there.
    if (view.lastSeq > 0) headers["Last-Event-ID"] = String(view.lastSeq);
    let res;
    try {
      headers.Authorization = `Bearer ${await token()}`;
      res = await fetch(`/v1/incidents/${id}/timeline/stream`, {
        headers,
        signal: controller.signal,
        cache: "no-store",
      });
    } catch (e) {
      if (controller.signal.aborted) return;
      this.scheduleReconnect(e.message || "network error");
      return;
    }
    if (!res.ok) {
      if (res.status === 401) auth.token = null;
      if (res.status === 404 || res.status === 403) {
        this.ended = true;
        setLive("error", "Error", `Stream refused (${res.status})`);
        return;
      }
      this.scheduleReconnect(`HTTP ${res.status}`);
      return;
    }
    setLive("live", "Live", "Following the incident's timeline stream");
    try {
      await this.read(res.body, controller.signal, id);
    } catch (e) {
      if (controller.signal.aborted) return;
      this.scheduleReconnect(e.message || "stream dropped");
      return;
    }
    if (controller.signal.aborted || id !== view.id) return;
    // The API ends the stream once the incident is closed or cancelled and its last event is
    // sent, and on a failed 5-minute re-check. Anything else is a drop.
    const status = view.incident && view.incident.status;
    if (FINISHED.has(status)) {
      this.ended = true;
      setLive("idle", "Ended", `Stream ended: incident ${STATUS_LABEL[status].toLowerCase()}`);
    } else {
      this.scheduleReconnect("stream closed");
    }
  },

  /** Parses text/event-stream from a ReadableStream (WHATWG SSE rules, the parts used here). */
  async read(body, signal, id) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let msg = { id: null, event: "message", data: [] };
    const dispatch = () => {
      if (msg.data.length) this.onMessage(msg, id);
      msg = { id: null, event: "message", data: [] };
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (signal.aborted) return;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.search(/\r\n|\r|\n/)) !== -1) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + (buffer[nl] === "\r" && buffer[nl + 1] === "\n" ? 2 : 1));
        if (line === "") {
          dispatch();
          continue;
        }
        if (line.startsWith(":")) continue; // heartbeat comment
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let val = colon === -1 ? "" : line.slice(colon + 1);
        if (val.startsWith(" ")) val = val.slice(1);
        if (field === "data") msg.data.push(val);
        else if (field === "event") msg.event = val;
        else if (field === "id") msg.id = val;
        else if (field === "retry" && /^\d+$/.test(val)) this.retryMs = Number(val);
      }
    }
  },

  onMessage(msg, id) {
    if (id !== view.id) return;
    let event;
    try {
      event = JSON.parse(msg.data.join("\n"));
    } catch {
      console.warn("unparseable stream message dropped");
      return;
    }
    const seq = Number(event.sequence ?? msg.id);
    if (!Number.isInteger(seq) || seq <= 0) return;
    this.failures = 0;
    if (view.events.has(seq)) return; // dedupe: a replay after reconnect
    view.events.set(seq, event);
    if (seq > view.lastSeq) view.lastSeq = seq;
    onEvent(event, seq);
  },
};

function setLive(state, label, title) {
  view.liveState = state;
  const l = $("live");
  l.dataset.state = state;
  l.textContent = label;
  l.title = title || label;
  renderStepper();
  renderLiveTimeline();
}

let liveTimer = null;
function scheduleLiveTimeline() {
  if (liveTimer) return;
  liveTimer = requestAnimationFrame(() => {
    liveTimer = null;
    renderLiveTimeline();
    renderStepper();
    renderFix(); // the hand-over reason comes from the timeline
  });
}

function onEvent(event, seq) {
  const type = String(event.type || "");
  const data = event.data || {};
  appendTimeline(event, seq > view.historySeq);
  const enter = (status) => {
    if (!view.statusTimes.has(status)) view.statusTimes.set(status, event.time);
  };
  if (type === "aisre.incident.created.v1") enter("open");
  else if (type === "aisre.incident.status_changed.v1" && data.to_status) enter(String(data.to_status));
  else if (type === "aisre.incident.handed_over.v1") enter("handed_over");
  else if (type === "aisre.incident.closed.v1") enter("closed");
  else if (type === "aisre.incident.cancelled.v1") enter("cancelled");
  else if (type === "aisre.incident.reinvestigation_requested.v1") {
    const run = Number(data.investigation_run);
    if (Number.isInteger(run) && run > view.run && run < 1000) view.run = run;
    renderTemporalLink();
  } else if (type === "aisre.hypothesis.created.v1" && data.hypothesis_id) {
    view.hypothesesFromEvents.set(String(data.hypothesis_id), data);
    if (!view.diagnosis) renderHypotheses();
  }
  if (type.startsWith("aisre.action.")) view.actionEvents += 1;
  if (type.startsWith("aisre.incident.") || type.startsWith("aisre.diagnosis.") || type.startsWith("aisre.action.")) {
    scheduleRefresh("incident");
  }
  if (type.startsWith("aisre.evidence.")) scheduleRefresh("evidence");
  if (type.startsWith("aisre.hypothesis.") || type.startsWith("aisre.diagnosis.")) {
    scheduleRefresh("diagnosis");
    scheduleRefresh("incident"); // spend moves with each model call
  }
  scheduleLiveTimeline();
}

// ------------------------------------------------------------------ header

function currentConfidence() {
  const d = view.diagnosis;
  if (!d) return null;
  // ADR-69: the reviewed final outcome and confidence, not the diagnoser's own.
  const outcome = d.final_outcome || d.outcome;
  const confidence = d.final_confidence || d.confidence;
  return { outcome, confidence, level: CONF_LEVEL[confidence] || 0, established: outcome === "established" };
}

function renderIncident() {
  const inc = view.incident;
  if (!inc) {
    for (const id of ["crumb-ref", "inc-ref", "inc-title", "inc-sub", "inc-service", "inc-env", "inc-started", "inc-age", "inc-status", "spend-amount", "spend-calls"]) {
      $(id).textContent = "";
    }
    $("inc-title").textContent = "Loading…";
    $("sev").textContent = "";
    $("sev").dataset.tone = "none";
    renderStepper();
    return;
  }
  $("crumb-ref").textContent = inc.display_ref;
  $("inc-ref").textContent = inc.display_ref;
  $("inc-title").textContent = inc.title;
  // The incident view has no alert summary, so the subtitle is the scope only.
  $("inc-sub").textContent = `${inc.service} · ${inc.environment}`;
  $("inc-service").textContent = inc.service;
  $("inc-env").textContent = inc.environment;
  $("inc-started").textContent = `Since ${longTime(inc.started_at)} UTC`;
  $("inc-started").title = "When the symptom started (the alert's time)";
  renderAge();

  const sev = $("sev");
  const severity = inc.confirmed_severity || inc.proposed_severity;
  clear(sev);
  if (severity && SEVERITY[severity]) {
    const [label, tone] = SEVERITY[severity];
    sev.dataset.tone = tone;
    sev.append(icon("incidents"), el("span", { text: `${label} · ${inc.confirmed_severity ? "confirmed" : "proposed"}` }));
  } else {
    sev.dataset.tone = "none";
    sev.append(el("span", { text: "Severity not set" }));
    sev.title = "No severity has been proposed or confirmed for this incident";
  }

  const pill = $("inc-status");
  pill.textContent = STATUS_LABEL[inc.status] || inc.status;
  pill.dataset.tone = STATUS_TONE[inc.status] || "";

  const c = currentConfidence();
  const bar = $("conf-bar");
  const label = $("conf-label");
  if (!c) {
    bar.dataset.level = "0";
    bar.dataset.tone = "none";
    label.textContent = "Not yet";
  } else {
    bar.dataset.level = String(c.level);
    bar.dataset.tone = c.established ? "ok" : "warn";
    label.textContent = capital(c.established ? humanize(c.confidence) : `${humanize(c.confidence)} · inconclusive`);
  }
  renderSpend();
  renderTemporalLink();
  renderStepper();
}

function renderAge() {
  const inc = view.incident;
  if (!inc) return;
  const age = $("inc-age");
  age.textContent = `Opened ${ago(inc.created_at)}`;
  age.title = `Opened ${fmtFull(inc.created_at)}`;
}

function renderSpend() {
  const b = view.incident && view.incident.budget;
  if (!b) {
    $("spend-amount").textContent = "Not available";
    $("spend-calls").textContent = "";
    $("spend-bar").style.setProperty("width", "0%");
    return;
  }
  const pct = b.max_cost_micros ? Math.min(100, (100 * b.spent_cost_micros) / b.max_cost_micros) : 0;
  $("spend-amount").textContent = `${fmtUsd(b.spent_cost_micros)} / ${fmtUsd(b.max_cost_micros)}`;
  // CSSOM, not a style attribute: allowed under style-src 'self'.
  $("spend-bar").style.setProperty("width", `${Math.max(pct, b.spent_cost_micros ? 1.5 : 0)}%`);
  const reserved = b.reserved_cost_micros ? ` · ${fmtUsd(b.reserved_cost_micros)} reserved` : "";
  $("spend-calls").textContent = `Model calls ${b.used_calls} of ${b.max_calls}${reserved}`;
}

function renderTemporalLink() {
  const a = $("temporal");
  if (!view.id || !UUID_RE.test(view.id)) {
    a.removeAttribute("href");
    return;
  }
  const run = Number.isInteger(view.run) && view.run > 1 ? `-r${view.run}` : "";
  const workflowId = `incident-${view.id.toLowerCase()}${run}`;
  a.href = `${TEMPORAL_UI}/namespaces/${TEMPORAL_NAMESPACE}/workflows/${workflowId}`;
  a.title = `Workflow ${workflowId} in the local Temporal UI (namespace ${TEMPORAL_NAMESPACE})`;
}

// ------------------------------------------------------------------ stepper

function renderStepper() {
  const list = $("stepper");
  const inc = view.incident;
  const status = inc ? inc.status : "open";
  const times = view.statusTimes;
  const seen = new Set(["open", ...times.keys(), status]);
  const verdict = seen.has("inconclusive") ? "inconclusive" : "diagnosed";
  const final = seen.has("cancelled") ? "cancelled" : "closed";
  const rank = (s) => {
    if (s === "open") return 0;
    if (s === "investigating") return 1;
    if (s === "handed_over") return 3;
    if (s === "closed" || s === "cancelled") return 4;
    return 2; // diagnosed, inconclusive, and the later M2+ states before a hand-over
  };
  const current = rank(status);
  const live = view.liveState === "live" && !SETTLED.has(status);
  const steps = [
    { key: "open", label: "Alert received", time: (inc && inc.created_at) || times.get("open") },
    { key: "investigating", label: "Investigating", time: times.get("investigating") },
    { key: verdict, label: STATUS_LABEL[verdict], time: times.get(verdict) },
    { key: "handed_over", label: "Handed over", time: times.get("handed_over") },
    { key: final, label: STATUS_LABEL[final], time: times.get(final) },
  ];
  clear(list);
  steps.forEach((s, i) => {
    let state = "todo";
    if (i < current) state = "done";
    if (i === current) state = SETTLED.has(status) || (i === 0 && status !== "open") ? "done" : "current";
    if (i === 2 && s.key === "inconclusive" && i <= current) state = "warn";
    if (i === 4 && s.key === "cancelled" && i <= current) state = "bad";
    if (i === current && i === 2 && !["diagnosed", "inconclusive"].includes(status)) state = "current";
    let caption = "Pending";
    if (state === "current") caption = live ? "In progress" : STATUS_LABEL[status] || "Current";
    else if (s.time && state !== "todo") caption = shortTime(s.time) + " UTC";
    else if (state !== "todo") caption = "Done";
    if (state === "todo" && s.key === "closed") caption = "By a person";
    const mark = {
      done: icon("check"),
      warn: icon("warn"),
      bad: icon("x"),
      current: el("span", { className: "ring-dot" }),
      todo: el("span", { className: "num", text: String(i + 1) }),
    }[state];
    list.append(
      el("li", { data: { state }, attrs: { "aria-current": state === "current" ? "step" : null } }, [
        el("span", { className: "step-mark", attrs: { "aria-hidden": "true" } }, [mark]),
        el("span", { className: "step-text" }, [
          el("span", { className: "step-label", text: s.label }),
          el("span", { className: "step-cap", text: caption }),
        ]),
      ]),
    );
  });
}

// ------------------------------------------------------------------ tabs

const TABS = ["diagnosis", "evidence", "runbooks", "timeline"];

function selectTab(name, focus = false) {
  ui.tab = TABS.includes(name) ? name : "diagnosis";
  for (const t of TABS) {
    const tab = $(`tab-${t}`);
    const on = t === ui.tab;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    $(`panel-${t}`).hidden = !on;
    if (on && focus) tab.focus();
  }
  if (ui.tab === "timeline") {
    const list = $("timeline");
    list.scrollTop = list.scrollHeight;
  }
}

function initTabs() {
  for (const t of TABS) $(`tab-${t}`).addEventListener("click", () => selectTab(t));
  $("tabs").addEventListener("keydown", (ev) => {
    const i = TABS.indexOf(ui.tab);
    let next = null;
    if (ev.key === "ArrowRight") next = TABS[(i + 1) % TABS.length];
    else if (ev.key === "ArrowLeft") next = TABS[(i + TABS.length - 1) % TABS.length];
    else if (ev.key === "Home") next = TABS[0];
    else if (ev.key === "End") next = TABS[TABS.length - 1];
    if (next) {
      ev.preventDefault();
      selectTab(next, true);
    }
  });
  $("full-tl").addEventListener("click", () => {
    selectTab("timeline");
    const card = $("tabs");
    card.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    $("tab-timeline").focus({ preventScroll: true });
  });
}

// ------------------------------------------------------------------ diagnosis

/** A citation chip: click opens and highlights the evidence item in the explorer. */
function citation(evidenceId, relation) {
  const label = view.evidenceLabel.get(evidenceId) || "E?";
  const contradicts = relation === "contradicts";
  const btn = el("button", {
    className: `cite${contradicts ? " contradicts" : ""}`,
    text: contradicts ? `${label} contradicts` : label,
    attrs: { type: "button", title: `Show evidence ${label} in the Evidence Explorer` },
  });
  btn.addEventListener("click", () => focusEvidence(evidenceId));
  return btn;
}

function gauge(c) {
  const level = c ? c.level : 0;
  const tone = !c ? "none" : c.established ? "ok" : "warn";
  const word = c ? capital(humanize(c.confidence)) : "Not yet";
  return el(
    "div",
    {
      className: "gauge",
      attrs: {
        role: "img",
        "aria-label": c ? `Confidence ${word}, ${level} of 3` : "Confidence not yet scored",
        title: "Categorical confidence scored by code (ADR-53): low, medium or high",
      },
    },
    [
      confidenceRing(level, tone),
      el("span", { className: "gauge-text" }, [
        el("strong", { text: word }),
        el("small", { text: "confidence" }),
      ]),
    ],
  );
}

function renderDiagnosis() {
  const box = $("diagnosis");
  const d = view.diagnosis;
  const c = currentConfidence();
  if (!d) {
    const live = view.incident && !SETTLED.has(view.incident.status);
    box.replaceChildren(
      el("div", { className: "root", data: { tone: "pending" } }, [
        el("span", { className: "root-icon" }, [icon(live ? "pulse" : "info")]),
        el("div", { className: "root-body" }, [
          el("span", { className: "root-label", text: "Root cause" }),
          el("p", {
            className: "root-text muted",
            text: view.diagnosisLoaded
              ? "No diagnosis yet. It appears once the evidence is in."
              : "Loading the diagnosis…",
          }),
        ]),
        gauge(null),
      ]),
    );
    return;
  }
  const leading = (d.hypotheses || []).find((h) => h.hypothesis_id === d.leading_hypothesis_id);
  const parts = [];
  parts.push(
    el("div", { className: "root", data: { tone: c.established ? "ok" : "warn" } }, [
      el("span", { className: "root-icon" }, [icon(c.established ? "check" : "warn")]),
      el("div", { className: "root-body" }, [
        el("span", { className: "root-label", text: c.established ? "Root cause established" : "Root cause not established" }),
        el("p", {
          className: "root-text",
          text: c.established && leading ? leading.statement : d.stop_reason || "The evidence did not settle it; see what is missing below.",
        }),
        el("div", { className: "root-tags" }, [
          el("span", { className: "tag", text: humanize(d.category) }),
          el("span", { className: "tag", data: { tone: d.reviewed ? "ok" : "warn" }, text: d.reviewed ? "Reviewed" : "Not reviewed" }),
        ]),
      ]),
      gauge(c),
    ]),
  );
  parts.push(el("p", { className: "summary", text: d.summary }));

  if (d.claims && d.claims.length) {
    parts.push(el("h3", { className: "section-h", text: "Claims and their evidence" }));
    parts.push(
      el(
        "ol",
        { className: "claims" },
        d.claims.map((cl) =>
          el("li", {}, [
            el("span", { text: cl.text }),
            el("span", { className: "cites" }, (cl.evidence_ids || []).map((id) => citation(id, "supports"))),
          ]),
        ),
      ),
    );
  }
  if (d.ruled_out && d.ruled_out.length) {
    parts.push(el("h3", { className: "section-h", text: "Ruled out" }));
    parts.push(el("ul", { className: "plain-list" }, d.ruled_out.map((r) => el("li", { text: r }))));
  }
  if (d.missing_evidence && d.missing_evidence.length) {
    parts.push(el("h3", { className: "section-h", text: "Missing evidence" }));
    parts.push(el("ul", { className: "plain-list warn-list" }, d.missing_evidence.map((r) => el("li", { text: r }))));
  }
  const review = [`Revision ${d.revision}`, `produced by ${d.produced_by}`];
  if (d.stop_reason) review.push(`stopped: ${d.stop_reason}`);
  if (d.reviewed) review.push(`review: ${d.review_notes || "accepted"}`);
  if (d.final_outcome && d.outcome !== d.final_outcome) review.push(`diagnoser said ${d.outcome}, review capped it`);
  else if (d.final_confidence && d.final_confidence !== d.confidence) review.push(`diagnoser scored ${d.confidence}`);
  parts.push(el("p", { className: "review", text: review.join(" · ") }));
  box.replaceChildren(...parts);
}

const HYP_STATUS = {
  open: ["Open", ""],
  proposed: ["Proposed", ""],
  supported: ["Supported", "ok"],
  confirmed: ["Confirmed", "ok"],
  ruled_out: ["Ruled out", "bad"],
};

function renderHypotheses() {
  const list = $("hypotheses");
  clear(list);
  const fromDiagnosis = view.diagnosis ? view.diagnosis.hypotheses || [] : [];
  const items = fromDiagnosis.length
    ? [...fromDiagnosis].sort((a, b) => a.ordinal - b.ordinal)
    : [...view.hypothesesFromEvents.values()].sort((a, b) => a.ordinal - b.ordinal);
  $("hyp-count").textContent = items.length ? `(${items.length})` : "";
  if (!items.length) {
    list.append(el("li", { className: "empty", text: "None yet. The platform proposes hypotheses once the evidence is in." }));
    return;
  }
  for (const h of items) {
    const id = String(h.hypothesis_id);
    const status = fromDiagnosis.length ? h.status || "open" : "proposed";
    const [label, tone] = HYP_STATUS[status] || [humanize(status), ""];
    const leading = view.diagnosis && view.diagnosis.leading_hypothesis_id === h.hypothesis_id;
    const open = ui.openHyps.has(id);
    const detailId = `hyp-d-${id}`;
    const toggle = el(
      "button",
      {
        className: "hyp-toggle",
        attrs: { type: "button", "aria-expanded": open ? "true" : "false", "aria-controls": detailId },
      },
      [
        el("span", { className: "hyp-num", text: String(h.ordinal) }),
        el("span", { className: "hyp-statement", text: h.statement }),
        el("span", { className: "hyp-meta" }, [
          leading ? el("span", { className: "pill sm", data: { tone: "accent" }, text: "Leading" }) : null,
          el("span", { className: "pill sm", data: { tone }, text: label }),
          el("span", { className: "hyp-conf", text: `${h.confidence} confidence` }),
          el("span", { className: "chev" }, [icon("chevronDown")]),
        ]),
      ],
    );
    const detail = el("div", { className: "hyp-detail", attrs: { id: detailId } });
    detail.hidden = !open;
    detail.append(el("p", { className: "hyp-line", text: `Category: ${humanize(h.category)}` }));
    if (h.status_reason && status !== "open" && status !== "proposed") {
      detail.append(el("p", { className: "hyp-line", text: `Status: ${h.status_reason}` }));
    }
    if (h.confirm_test) detail.append(el("p", { className: "hyp-line" }, [el("strong", { text: "Confirmed if: " }), h.confirm_test]));
    if (h.refute_test) detail.append(el("p", { className: "hyp-line" }, [el("strong", { text: "Refuted if: " }), h.refute_test]));
    if (h.evidence && h.evidence.length) {
      const supports = h.evidence.filter((l) => l.relation !== "contradicts");
      const contradicts = h.evidence.filter((l) => l.relation === "contradicts");
      if (supports.length) {
        detail.append(el("p", { className: "hyp-line cites" }, [el("strong", { text: "Supports: " }), ...supports.map((l) => citation(l.evidence_id, "supports"))]));
      }
      if (contradicts.length) {
        detail.append(el("p", { className: "hyp-line cites" }, [el("strong", { text: "Contradicts: " }), ...contradicts.map((l) => citation(l.evidence_id, "contradicts"))]));
      }
    } else if (!fromDiagnosis.length) {
      detail.append(el("p", { className: "hyp-line muted", text: "Evidence links appear with the diagnosis." }));
    }
    toggle.addEventListener("click", () => {
      const nowOpen = !ui.openHyps.has(id);
      if (nowOpen) ui.openHyps.add(id);
      else ui.openHyps.delete(id);
      toggle.setAttribute("aria-expanded", nowOpen ? "true" : "false");
      detail.hidden = !nowOpen;
    });
    list.append(el("li", { className: "hyp", data: { status } }, [toggle, detail]));
  }
}

// ------------------------------------------------------------------ proposed fix (read only)

const ISOLATION_NOTE = {
  "development-runc": "development-runc: runc on a development host. It proves the flow, never isolation (ADR-71).",
  gvisor: "gvisor: the sandbox ran under gVisor on the test host.",
};

/** A patch's canonical diff as red and green lines: text only, from Patch.canonical()'s hunks. */
function renderDiff(diff) {
  if (!diff) return null;
  if (diff.loading) return el("p", { className: "fix-text", text: "Loading the patch…" });
  if (diff.error) return el("p", { className: "fix-text", text: `Patch not shown: ${diff.error}` });
  const lines = [];
  for (const f of diff.files) {
    lines.push(el("span", { className: "diff-file", text: `--- a/${f.path}\n+++ b/${f.path}` }));
    for (const h of Array.isArray(f.hunks) ? f.hunks : []) {
      lines.push(el("span", { className: "diff-hunk", text: `@@ -${h.old_start},${h.old_lines} +${h.new_start},${h.new_lines} @@` }));
      for (const line of Array.isArray(h.lines) ? h.lines : []) {
        const tag = String(line).slice(0, 1);
        const kind = tag === "+" ? "add" : tag === "-" ? "del" : "ctx";
        lines.push(el("span", { className: `diff-line ${kind}`, text: String(line) }));
      }
    }
  }
  return el("pre", { className: "diff", attrs: { "aria-label": "Patch diff" } }, lines);
}

function renderFix() {
  const box = $("fix");
  const inc = view.incident;
  const actions = [...view.actions.values()];
  const parts = [];
  const handover = [...view.events.values()].find((e) => e.type === "aisre.incident.handed_over.v1");
  const reason = handover && handover.data && handover.data.reason ? String(handover.data.reason) : "";
  if (actions.length) {
    for (const a of actions) {
      const test = a.latest_test;
      const diff = a.patch ? view.diffs.get(String(a.patch.diff_artifact_id)) : null;
      parts.push(
        el("div", { className: "fix-item" }, [
          el("div", { className: "fix-top" }, [
            el("span", { className: "tag", text: humanize(a.kind) }),
            el("span", { className: "pill sm", text: humanize(a.status) }),
          ]),
          el("p", { className: "fix-text", text: a.description }),
          el("dl", { className: "fix-dl" }, [
            el("dt", { text: "Target" }),
            el("dd", { text: `${a.target_service} / ${a.target_environment} · ${a.target_deployment}` }),
            el("dt", { text: "Revision" }),
            el("dd", { className: "mono", text: `${a.base_revision} → ${a.expected_after_revision}` }),
            el("dt", { text: "Expected effect" }),
            el("dd", { text: a.expected_effect }),
            el("dt", { text: "Risk" }),
            el("dd", { text: a.risk }),
            el("dt", { text: "Undo" }),
            el("dd", { text: a.undo_plan }),
            el("dt", { text: "Sandbox test" }),
            el("dd", {
              text: test
                ? `${humanize(test.outcome)} · failure reproduced first: ${test.reproduced ? "yes" : "no"} · ${ISOLATION_NOTE[test.isolation] || test.isolation}`
                : a.status === "testing" ? "Running" : "Not run",
            }),
            el("dt", { text: "Pull request" }),
            el("dd", {
              text: a.patch && a.patch.review_ref
                ? `${a.patch.review_ref}, a draft in the review system. People review, merge and deploy it; the platform never merges.`
                : "None: a draft pull request is opened only for a fix whose test passed.",
            }),
          ]),
          a.patch
            ? el("div", { className: "diff-stat" }, [
                el("span", { className: "add", text: `+${a.patch.lines_added}` }),
                el("span", { className: "del", text: `−${a.patch.lines_removed}` }),
                el("span", { text: `${a.patch.files_changed} file${a.patch.files_changed === 1 ? "" : "s"} changed` }),
              ])
            : null,
          renderDiff(diff),
        ]),
      );
    }
    if (reason) parts.push(el("p", { className: "fix-reason" }, [el("strong", { text: "Hand-over reason: " }), reason]));
    parts.push(
      el("p", {
        className: "fix-note",
        text: "The fix is a recommendation, applied by people: the platform is advisory in this release (ADR-44) and this viewer is read-only (ADR-70).",
      }),
    );
  } else {
    parts.push(el("p", { className: "fix-empty-title", text: "No fix drafted." }));
    const handed = inc && ["handed_over", "closed"].includes(inc.status);
    parts.push(
      el("p", {
        className: "fix-text",
        text: handed
          ? "This incident was handed over to people with the diagnosis and no fix."
          : "A fix is drafted only from an established, reviewed diagnosis, then tested in the sandbox.",
      }),
    );
    if (reason) parts.push(el("p", { className: "fix-reason" }, [el("strong", { text: "Hand-over reason: " }), reason]));
    parts.push(el("p", { className: "fix-note", text: "The platform is advisory in this release (ADR-44) and this viewer is read-only (ADR-70)." }));
  }
  box.replaceChildren(...parts);
}

// ------------------------------------------------------------------ live timeline (condensed)

function condensedRows() {
  const events = [...view.events.entries()].sort((a, b) => a[0] - b[0]).map(([, e]) => e);
  const hasStatusHandover = events.some(
    (e) => e.type === "aisre.incident.status_changed.v1" && e.data && e.data.to_status === "handed_over",
  );
  const rows = [];
  let group = null;
  const flush = () => {
    if (group) rows.push(group.finish());
    group = null;
  };
  for (const e of events) {
    const type = String(e.type || "");
    const d = e.data || {};
    if (type === "aisre.evidence.collected.v1") {
      if (!group || group.kind !== "evidence") {
        flush();
        group = {
          kind: "evidence",
          n: 0,
          strong: 0,
          kinds: new Set(),
          time: null,
          finish() {
            return {
              title: `Evidence collected: ${this.n} record${this.n === 1 ? "" : "s"}, ${this.strong} strong`,
              detail: `${[...this.kinds].map(humanize).join(", ")} · strong = mechanism tiers 1–3`,
              time: this.time,
              tone: "done",
            };
          },
        };
      }
      group.n += 1;
      if (d.strength_tier && Number(d.strength_tier) <= 3) group.strong += 1;
      if (d.kind) group.kinds.add(String(d.kind));
      group.time = e.time;
      continue;
    }
    if (type === "aisre.hypothesis.created.v1" || type === "aisre.hypothesis.status_changed.v1") {
      const kind = type.endsWith("created.v1") ? "hyp-new" : "hyp-upd";
      if (!group || group.kind !== kind) {
        flush();
        group = {
          kind,
          n: 0,
          time: null,
          finish() {
            return {
              title: kind === "hyp-new" ? `Hypotheses proposed: ${this.n}` : `Hypotheses updated: ${this.n}`,
              detail: kind === "hyp-new" ? "Tested against the evidence by the diagnosis" : "",
              time: this.time,
              tone: "done",
            };
          },
        };
      }
      group.n += 1;
      group.time = e.time;
      continue;
    }
    flush();
    if (type === "aisre.incident.handed_over.v1" && hasStatusHandover) continue;
    let row = null;
    switch (type) {
      case "aisre.incident.created.v1":
        row = { title: "Alert received, incident opened", detail: `${d.display_ref || ""} from ${d.source || "?"}` };
        break;
      case "aisre.incident.status_changed.v1": {
        const to = String(d.to_status || "");
        const names = {
          investigating: "Investigation started",
          diagnosed: "Diagnosed",
          testing: "Testing a fix in the sandbox",
          inconclusive: "Inconclusive",
          handed_over: "Handed over to people",
          closed: "Closed",
          cancelled: "Cancelled",
        };
        row = {
          title: names[to] || `Status: ${STATUS_LABEL[to] || to}`,
          detail: d.reason || "",
          tone: to === "inconclusive" ? "warn" : to === "cancelled" ? "bad" : "done",
        };
        break;
      }
      case "aisre.evidence.gap_recorded.v1":
        row = { title: "Gap: a source could not be read", detail: `${d.source_system || "?"}: ${d.gap_reason || ""}`, tone: "warn" };
        break;
      case "aisre.evidence.injection_flagged.v1":
        row = { title: "Instruction-like text flagged", detail: `${d.source_system || "?"} (${d.tool || "?"})`, tone: "warn" };
        break;
      case "aisre.diagnosis.produced.v1":
        row = { title: "Diagnosis produced", detail: `${d.confidence || "?"} confidence · ${humanize(d.category)}` };
        break;
      case "aisre.diagnosis.inconclusive.v1":
        row = { title: "Diagnosis inconclusive", detail: d.stop_reason || "", tone: "warn" };
        break;
      case "aisre.diagnosis.reviewed.v1":
        row = { title: "Diagnosis reviewed", detail: d.review_notes || "" };
        break;
      case "aisre.incident.alert_attached.v1":
        row = { title: "Another alert joined", detail: d.fingerprint || "" };
        break;
      case "aisre.action.proposed.v1":
        row = { title: "Fix drafted", detail: humanize(d.kind) };
        break;
      case "aisre.action.test_started.v1":
        row = { title: "Sandbox test started", detail: String(d.isolation || "") };
        break;
      case "aisre.action.tested.v1":
        row = {
          title: `Sandbox test: ${humanize(d.outcome)}`,
          detail: `${d.reproduced ? "failure reproduced first" : "failure did not reproduce"} · ${d.isolation}`,
          tone: d.outcome === "passed" ? "done" : "warn",
        };
        break;
      case "aisre.action.review_opened.v1":
        row = { title: "Draft pull request opened", detail: `${d.review_ref} · for people to review and merge` };
        break;
      default:
        row = { title: EVENT_LABEL[type] || type, detail: "" };
    }
    rows.push({ tone: "done", ...row, time: e.time });
  }
  flush();
  return rows;
}

function renderLiveTimeline() {
  const list = $("live-tl");
  clear(list);
  const rows = condensedRows();
  const inc = view.incident;
  const status = inc ? inc.status : null;
  const running = status && !SETTLED.has(status);
  if (!rows.length) {
    list.append(el("li", { className: "rail-empty", text: view.id ? "Waiting for the first event…" : "" }));
    return;
  }
  // While the run is live the newest row is where it is now; a settled incident has none.
  if (running) rows[rows.length - 1].tone = rows[rows.length - 1].tone === "done" ? "current" : rows[rows.length - 1].tone;
  const next = {
    open: "Investigation",
    investigating: "Diagnosis",
    diagnosed: "Fix drafted and tested",
    testing: "Hand-over to people",
    inconclusive: "Hand-over to people",
  }[status];
  if (running && next) rows.push({ title: next, detail: "Not yet", time: null, tone: "todo" });
  for (const r of rows) {
    const mark = { done: icon("check"), warn: icon("warn"), bad: icon("x"), current: el("span", { className: "ring-dot" }), todo: el("span") }[r.tone] || icon("check");
    list.append(
      el("li", { data: { tone: r.tone } }, [
        el("span", { className: "rail-time", text: r.time ? shortTime(r.time) : "", attrs: { title: r.time ? `${fmtFull(r.time)}` : null } }),
        el("span", { className: "rail-dot", attrs: { "aria-hidden": "true" } }, [mark]),
        el("span", { className: "rail-body" }, [
          el("span", { className: "rail-title", text: r.title }),
          r.detail ? el("span", { className: "rail-detail", text: r.detail }) : null,
        ]),
      ]),
    );
  }
}

// ------------------------------------------------------------------ full timeline (Timeline tab)

function describeEvent(event) {
  const d = event.data || {};
  switch (event.type) {
    case "aisre.incident.created.v1":
      return `${d.display_ref || ""} ${d.service || ""}/${d.environment || ""} from ${d.source || "?"}`;
    case "aisre.incident.status_changed.v1":
      return `${STATUS_LABEL[d.from_status] || d.from_status} → ${STATUS_LABEL[d.to_status] || d.to_status}${d.reason ? `: ${d.reason}` : ""}`;
    case "aisre.incident.handed_over.v1":
    case "aisre.incident.cancelled.v1":
      return d.reason || "";
    case "aisre.incident.alert_attached.v1":
      return d.source_alert_id || d.fingerprint || "";
    case "aisre.incident.note_added.v1":
      return d.note || "";
    case "aisre.incident.reinvestigation_requested.v1":
      return `run ${d.investigation_run}${d.reason ? `: ${d.reason}` : ""}`;
    case "aisre.evidence.collected.v1": {
      const tier = d.strength_tier ? ` · tier ${d.strength_tier}` : "";
      const label = view.evidenceLabel.get(d.evidence_id);
      return `${label ? `${label} · ` : ""}${humanize(d.kind)} from ${d.source_system}${tier}${d.is_partial ? " · partial" : ""}`;
    }
    case "aisre.evidence.gap_recorded.v1":
      return `${d.source_system}: ${d.gap_reason}`;
    case "aisre.evidence.injection_flagged.v1":
      return `${d.source_system} (${d.tool})`;
    case "aisre.hypothesis.created.v1":
      return `H${d.ordinal}: ${d.statement} (${d.confidence})`;
    case "aisre.hypothesis.status_changed.v1":
      return `${d.from_status} → ${d.to_status} (${d.confidence})`;
    case "aisre.diagnosis.produced.v1":
      return `revision ${d.revision}, ${humanize(d.category)}, ${d.confidence} confidence`;
    case "aisre.diagnosis.inconclusive.v1":
      return `revision ${d.revision}${d.stop_reason ? `: ${d.stop_reason}` : ""}`;
    case "aisre.diagnosis.reviewed.v1":
      return `revision ${d.revision}${d.review_notes ? `: ${d.review_notes}` : ""}`;
    case "aisre.action.proposed.v1":
      return `${humanize(d.kind)} for ${d.target_service}/${d.target_environment}`;
    case "aisre.action.test_started.v1":
      return `isolation ${d.isolation}${d.isolation === "development-runc" ? " (proves the flow, not isolation)" : ""}`;
    case "aisre.action.tested.v1":
      return `${humanize(d.outcome)} · reproduced first: ${d.reproduced ? "yes" : "no"} · isolation ${d.isolation}`;
    case "aisre.action.review_opened.v1":
      return `${d.review_ref} · draft, never merged by the platform · isolation ${d.isolation}`;
    case "aisre.limit.reached.v1":
    case "aisre.model.attempt_failed.v1":
      return Object.entries(d)
        .slice(0, 3)
        .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
        .join(", ");
    default:
      return "";
  }
}

function eventGroup(type) {
  const part = String(type).split(".")[1] || "";
  return part === "hypothesis" ? "diagnosis" : part;
}

function appendTimeline(event, fresh) {
  const list = $("timeline");
  const seq = Number(event.sequence);
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const detail = describeEvent(event);
  const item = el("li", { className: `tl${fresh ? " fresh" : ""}`, data: { seq } }, [
    el("span", { className: "tl-time", text: `${monthDay(event.time)} ${hhmmss(event.time)}` }),
    el("div", { className: "tl-body" }, [
      el("div", { className: "tl-name-row" }, [
        el("span", {
          className: "tl-name",
          text: EVENT_LABEL[event.type] || event.type,
          data: { group: eventGroup(event.type) },
          attrs: { title: event.type },
        }),
        el("span", { className: "tl-seq", text: `#${seq}` }),
      ]),
      detail ? el("div", { className: "tl-detail", text: detail }) : null,
      el("div", { className: "tl-actor", text: event.actor || "" }),
    ]),
  ]);
  // Keep sequence order even if a replay delivers an older event late.
  let before = null;
  for (const child of list.children) {
    if (Number(child.dataset.seq) > seq) {
      before = child;
      break;
    }
  }
  list.insertBefore(item, before);
  if (fresh) setTimeout(() => item.classList.remove("fresh"), 1600);
  $("tab-tl-count").textContent = String(view.events.size);
  if (atBottom) list.scrollTop = list.scrollHeight;
}

// ------------------------------------------------------------------ evidence

function tierTag(tier) {
  if (!tier) return el("span", { className: "tag", text: "No tier", attrs: { title: "No strength tier assigned" } });
  const [name, cls] = TIERS[tier] || [`Tier ${tier}`, "supp"];
  const meaning = { mech: "mechanism", supp: "supporting", weak: "weak" }[cls];
  return el("span", { className: `tag tier-${cls}`, text: `T${tier} · ${name}`, attrs: { title: `Tier ${tier}: ${name} (${meaning}, ADR-53)` } });
}

function kindTag(kind) {
  const k = KINDS[kind] || { name: humanize(kind), icon: "info" };
  return el("span", { className: `tag kind${kind === "gap" ? " gap" : ""}` }, [icon(k.icon), el("span", { text: k.name })]);
}

function headline(e) {
  if (e.kind === "gap") return `Not collected: ${e.gap_reason || "unknown reason"}`;
  return firstLine(e.summary);
}

function evidenceMatches(e) {
  if (ui.kindChip !== "All" && (KINDS[e.kind] || {}).chip !== ui.kindChip) return false;
  const q = ui.query.trim().toLowerCase();
  if (!q) return true;
  const label = (view.evidenceLabel.get(e.evidence_id) || "").toLowerCase();
  const hay = [e.summary, e.source_system, e.tool, e.kind, humanize(e.kind), e.gap_reason, label].join(" ").toLowerCase();
  return hay.includes(q);
}

function renderChips() {
  const box = $("ex-chips");
  clear(box);
  const present = new Set(view.evidence.map((e) => (KINDS[e.kind] || {}).chip).filter(Boolean));
  const chips = ["All", ...CHIP_ORDER.filter((c) => present.has(c))];
  if (!chips.includes(ui.kindChip)) ui.kindChip = "All";
  for (const c of chips) {
    const n = c === "All" ? view.evidence.length : view.evidence.filter((e) => (KINDS[e.kind] || {}).chip === c).length;
    const b = el("button", { className: `chip-btn${c === "Gaps" ? " gap" : ""}`, attrs: { type: "button", "aria-pressed": c === ui.kindChip ? "true" : "false" } }, [
      el("span", { text: c }),
      el("span", { className: "chip-n", text: String(n) }),
    ]);
    b.addEventListener("click", () => {
      ui.kindChip = c;
      renderChips();
      applyEvidenceFilter();
    });
    box.append(b);
  }
}

function applyEvidenceFilter() {
  let shown = 0;
  for (const e of view.evidence) {
    const item = document.getElementById(`ev-${e.evidence_id}`);
    if (!item) continue;
    const ok = evidenceMatches(e);
    item.hidden = !ok;
    if (ok) shown += 1;
  }
  const none = $("ev-none");
  if (none) none.hidden = shown > 0 || !view.evidence.length;
}

function evidenceRow(e) {
  const isGap = e.kind === "gap";
  const attrs = e.attributes || {};
  const restricted = attrs.restricted === true;
  const label = view.evidenceLabel.get(e.evidence_id);
  const details = el("details", { className: "ev-details" });
  details.open = ui.openEvidence.has(e.evidence_id);
  details.addEventListener("toggle", () => {
    if (details.open) ui.openEvidence.add(e.evidence_id);
    else ui.openEvidence.delete(e.evidence_id);
  });
  const flags = [
    restricted ? el("span", { className: "tag", data: { tone: "warn" } }, [icon("lock"), el("span", { text: "Restricted" })]) : null,
    e.is_partial ? el("span", { className: "tag", data: { tone: "warn" }, text: "Partial" }) : null,
    e.flagged_injection ? el("span", { className: "tag", data: { tone: "bad" } }, [icon("flag"), el("span", { text: "Instruction-like text flagged" })]) : null,
    e.content_removed_at ? el("span", { className: "tag", data: { tone: "bad" }, text: "Content removed" }) : null,
  ];
  details.append(
    el("summary", { className: "ev-sum" }, [
      el("span", { className: "ev-label", text: label }),
      kindTag(e.kind),
      isGap ? null : tierTag(e.strength_tier),
      el("span", { className: "ev-headline", text: headline(e) }),
      ...flags,
      el("span", { className: "ev-source", text: e.source_system }),
      el("span", { className: "ev-time", text: shortTime(e.collected_at) }),
      el("span", { className: "chev" }, [icon("chevronDown")]),
    ]),
  );
  const meta = [];
  const add = (k, v) => v && meta.push(el("dt", { text: k }), el("dd", { text: v }));
  add("Source", `${e.source_system} · ${e.tool}`);
  if (e.window_start || e.window_end) add("Window", `${fmtFull(e.window_start)} to ${fmtFull(e.window_end)}`);
  add("Collected", fmtFull(e.collected_at));
  if (restricted) {
    add("Document", `${attrs.doc_id ?? "?"} version ${attrs.doc_version ?? "?"}`);
    add("Restricted", `Text shown only to the document's permitted groups (owner: ${attrs.owner ?? "unknown"}).`);
  } else if (attrs.doc_id) {
    add("Document", `${attrs.doc_id} version ${attrs.doc_version ?? "?"}`);
  }
  if (e.is_partial) add("Partial", "The source returned only part of the data.");
  if (e.flagged_injection) add("Flagged", "Instruction-like text was found in this record; it is treated as data only.");
  if (e.content_removed_at) add("Removed", `${fmtFull(e.content_removed_at)}${e.content_removed_reason ? `: ${e.content_removed_reason}` : ""}`);
  details.append(
    el("div", { className: "ev-body" }, [
      el("pre", { text: isGap ? `Not collected: ${e.gap_reason}` : e.summary }),
      el("dl", { className: "ev-meta" }, meta),
    ]),
  );
  return el("li", { className: `ev${isGap ? " gap" : ""}`, attrs: { id: `ev-${e.evidence_id}` }, data: { kind: e.kind } }, [details]);
}

function renderEvidence() {
  const list = $("evidence");
  clear(list);
  const n = view.evidence.length;
  const gaps = view.evidence.filter((e) => e.kind === "gap").length;
  $("ex-count").textContent = n ? `(${n} item${n === 1 ? "" : "s"}${gaps ? `, ${gaps} gap${gaps > 1 ? "s" : ""}` : ""})` : "";
  $("tab-ev-count").textContent = String(n);
  renderChips();
  if (!n) {
    list.append(el("li", { className: "empty", text: "None yet. Evidence appears as the collector reads each source." }));
  } else {
    for (const e of view.evidence) list.append(evidenceRow(e));
    list.append(el("li", { className: "empty", attrs: { id: "ev-none" }, text: "No evidence matches this filter." }));
    applyEvidenceFilter();
  }
  renderEvidenceTab();
  renderRunbooksTab();
}

function jumpRow(e) {
  const b = el("button", { className: "jump", attrs: { type: "button", title: "Show in the Evidence Explorer" } }, [
    el("span", { className: "ev-label", text: view.evidenceLabel.get(e.evidence_id) }),
    kindTag(e.kind),
    e.kind === "gap" ? null : tierTag(e.strength_tier),
    el("span", { className: "ev-headline", text: headline(e) }),
  ]);
  b.addEventListener("click", () => focusEvidence(e.evidence_id));
  return el("li", {}, [b]);
}

function renderEvidenceTab() {
  const panel = $("panel-evidence");
  clear(panel);
  if (!view.evidence.length) {
    panel.append(el("p", { className: "empty", text: "None yet." }));
    return;
  }
  const count = (pred) => view.evidence.filter(pred).length;
  const cls = (e) => (e.strength_tier ? TIERS[e.strength_tier][1] : null);
  const groups = [
    ["mech", "Mechanism", "tiers 1–3: reproduction, version split, config match", count((e) => cls(e) === "mech")],
    ["supp", "Supporting", "tiers 4–7: code match, new error, scale match, rollback", count((e) => cls(e) === "supp")],
    ["weak", "Timing alone", "tier 8", count((e) => cls(e) === "weak")],
    ["none", "No tier", "context: runbooks, past incidents, health", count((e) => e.kind !== "gap" && !e.strength_tier)],
    ["gap", "Gaps", "sources that could not be read", count((e) => e.kind === "gap")],
  ];
  panel.append(
    el("p", { className: "panel-note", text: "What the collector found, by strength. Tiers are assigned by code from what the observation is (ADR-53), not by the model." }),
    el(
      "ul",
      { className: "tier-grid" },
      groups.map(([key, name, hint, n]) =>
        el("li", { data: { tier: key } }, [el("strong", { text: String(n) }), el("span", { className: "tier-name", text: name }), el("small", { text: hint })]),
      ),
    ),
    el("ol", { className: "jump-list" }, view.evidence.map(jumpRow)),
  );
}

function renderRunbooksTab() {
  const panel = $("panel-runbooks");
  clear(panel);
  const docs = view.evidence.filter((e) => e.kind === "runbook" || e.kind === "past_incident");
  $("tab-rb-count").textContent = String(docs.length);
  panel.append(el("p", { className: "panel-note", text: "Runbook and past-incident passages retrieved for this incident and stored as evidence. A reader outside a document's groups sees only its owner and version." }));
  if (!docs.length) {
    panel.append(el("p", { className: "empty", text: "None retrieved (yet)." }));
    return;
  }
  const list = el("ol", { className: "rb-list" });
  for (const e of docs) {
    const attrs = e.attributes || {};
    const restricted = attrs.restricted === true;
    const details = el("details", { className: "rb" });
    details.open = ui.openRunbooks.has(e.evidence_id);
    details.addEventListener("toggle", () => {
      if (details.open) ui.openRunbooks.add(e.evidence_id);
      else ui.openRunbooks.delete(e.evidence_id);
    });
    details.append(
      el("summary", {}, [
        el("span", { className: "rb-icon" }, [icon(restricted ? "lock" : e.kind === "runbook" ? "runbook" : "past_incident")]),
        el("span", { className: "rb-main" }, [
          el("span", { className: "rb-title", text: restricted ? `Restricted ${KINDS[e.kind].name.toLowerCase()}` : firstLine(e.summary) }),
          el("span", { className: "rb-sub", text: `${view.evidenceLabel.get(e.evidence_id)} · ${KINDS[e.kind].name} · ${attrs.doc_id ?? "?"} version ${attrs.doc_version ?? "?"}` }),
        ]),
        el("span", { className: "chev" }, [icon("chevronDown")]),
      ]),
      restricted
        ? el("p", { className: "rb-restricted", text: `Text shown only to the document's permitted groups (owner: ${attrs.owner ?? "unknown"}).` })
        : el("pre", { text: e.summary }),
    );
    const jump = el("button", { className: "link-btn sm", attrs: { type: "button" }, text: "Show in explorer" });
    jump.addEventListener("click", () => focusEvidence(e.evidence_id));
    details.append(jump);
    list.append(el("li", {}, [details]));
  }
  panel.append(list);
}

function focusEvidence(evidenceId) {
  const record = view.evidence.find((e) => e.evidence_id === evidenceId);
  if (!record) {
    showMessage("That evidence record isn't in the list (yet).", "warn");
    return;
  }
  if (!evidenceMatches(record)) {
    ui.kindChip = "All";
    ui.query = "";
    $("ex-q").value = "";
    renderChips();
    applyEvidenceFilter();
  }
  const target = document.getElementById(`ev-${evidenceId}`);
  if (!target) return;
  const details = target.querySelector("details");
  if (details) details.open = true;
  ui.openEvidence.add(evidenceId);
  target.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
  const summary = target.querySelector("summary");
  if (summary) summary.focus({ preventScroll: true });
  target.classList.remove("flash");
  void target.offsetWidth; // restart the highlight
  target.classList.add("flash");
  setTimeout(() => target.classList.remove("flash"), 2200);
}

// ------------------------------------------------------------------ search / open box

function searchMatches(q) {
  const s = q.trim().toLowerCase();
  if (!s) return recent;
  return recent.filter((r) => [r.ref, r.title, r.id, r.status].join(" ").toLowerCase().includes(s));
}

function renderSearchResults(show) {
  const input = $("open-id");
  const box = $("search-results");
  clear(box);
  const q = input.value.trim();
  const items = UUID_RE.test(q) ? [] : searchMatches(q);
  if (UUID_RE.test(q)) {
    box.append(el("li", { className: "sr-hint", text: "Press Enter to open this incident ID." }));
  } else if (!items.length) {
    box.append(
      el("li", {
        className: "sr-hint",
        text: recent.length ? "No recent incident matches. Paste a full incident ID to open it." : "Paste an incident ID (UUID) and press Enter. Incidents you open appear here.",
      }),
    );
  }
  for (const r of items) {
    const b = el("button", { className: "sr-item", attrs: { type: "button", role: "option" } }, [
      el("span", { className: "recent-ref", text: r.ref }),
      el("span", { className: "recent-title", text: r.title }),
      el("span", { className: "pill sm", data: { tone: STATUS_TONE[r.status] || "" }, text: STATUS_LABEL[r.status] || r.status }),
    ]);
    b.addEventListener("mousedown", (ev) => ev.preventDefault()); // keep focus for the click
    b.addEventListener("click", () => {
      hideSearch();
      input.value = "";
      openIncident(r.id);
    });
    box.append(el("li", { attrs: { role: "presentation" } }, [b]));
  }
  box.hidden = !show;
  input.setAttribute("aria-expanded", show ? "true" : "false");
}

function hideSearch() {
  $("search-results").hidden = true;
  $("open-id").setAttribute("aria-expanded", "false");
}

function initSearch() {
  const input = $("open-id");
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  $("kbd-hint").textContent = isMac ? "⌘K" : "Ctrl K";
  input.addEventListener("focus", () => renderSearchResults(true));
  input.addEventListener("input", () => renderSearchResults(true));
  input.addEventListener("blur", () => setTimeout(hideSearch, 120));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") {
      hideSearch();
      input.blur();
    } else if (ev.key === "ArrowDown") {
      const first = $("search-results").querySelector("button");
      if (first) {
        ev.preventDefault();
        first.focus();
      }
    }
  });
  $("search-results").addEventListener("keydown", (ev) => {
    const buttons = [...$("search-results").querySelectorAll("button")];
    const i = buttons.indexOf(document.activeElement);
    if (ev.key === "ArrowDown" && i < buttons.length - 1) {
      ev.preventDefault();
      buttons[i + 1].focus();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      if (i > 0) buttons[i - 1].focus();
      else input.focus();
    } else if (ev.key === "Escape") {
      hideSearch();
      input.focus();
    }
  });
  $("open-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    showMessage("");
    if (UUID_RE.test(q)) {
      hideSearch();
      input.value = "";
      input.blur();
      openIncident(q);
      return;
    }
    const matches = q ? searchMatches(q) : [];
    if (matches.length) {
      hideSearch();
      input.value = "";
      input.blur();
      openIncident(matches[0].id);
    } else {
      showMessage("That is not an incident ID (expected a UUID), and no incident opened in this browser matches it.", "error");
    }
  });
  document.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      input.focus();
      input.select();
    }
  });
  $("open-focus").addEventListener("click", () => input.focus());
}

// ------------------------------------------------------------------ sidebar (small screens)

function openSidebar() {
  document.body.classList.add("side-open");
  $("scrim").hidden = false;
  $("menu").setAttribute("aria-expanded", "true");
  $("side-close").focus();
}

function closeSidebar() {
  if (!document.body.classList.contains("side-open")) return;
  document.body.classList.remove("side-open");
  $("scrim").hidden = true;
  $("menu").setAttribute("aria-expanded", "false");
}

function initSidebar() {
  $("menu").addEventListener("click", openSidebar);
  $("side-close").addEventListener("click", () => {
    closeSidebar();
    $("menu").focus();
  });
  $("scrim").addEventListener("click", closeSidebar);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeSidebar();
  });
}

// ------------------------------------------------------------------ start (the one write)

async function startReference(button) {
  const buttons = [$("start"), $("start-empty")];
  for (const b of buttons) b.disabled = true;
  showMessage("Posting the reference alert…");
  try {
    const alertRes = await fetch("reference-alert.json", { cache: "no-store" });
    if (!alertRes.ok) throw new Error(`reference alert not served (${alertRes.status})`);
    const alertText = await alertRes.text();
    JSON.parse(alertText); // fail here, not at intake, if the copy is damaged
    const { status, body } = await api("/v1/incidents", {
      method: "POST",
      body: alertText,
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": `demo-viewer-${crypto.randomUUID()}`,
      },
    });
    if (status === 200 && body.deduplicated) {
      showMessage(
        `The alert joined open incident ${body.display_ref}: an orders-api alert joins an incident until it is closed. ` +
          "Close it with the CLI (incident close) to start a fresh run; this viewer cannot close incidents.",
        "warn",
      );
    } else {
      showMessage(`Opened ${body.display_ref}. Following it live.`);
    }
    await openIncident(body.incident_id);
  } catch (e) {
    let text = `Could not start the incident: ${e.message}`;
    if (e.status === 403) text = "This persona may not create incidents (ADR-50). Pick a responder.";
    if (e.status === 404 && e.problem && e.problem.code === "unknown_service") {
      text += " Run the dev seed: docker compose exec api python -m aisre.api.dev_seed";
    } else if (e.status === 404) {
      text += " (Has the dev seed been run? docker compose exec api python -m aisre.api.dev_seed)";
    }
    showMessage(text, "error");
  } finally {
    for (const b of buttons) b.disabled = false;
    if (button && document.activeElement === document.body) button.focus();
  }
}

// ------------------------------------------------------------------ personas

function renderPersona() {
  const p = auth.persona;
  $("avatar").textContent = p.initials;
  $("user-name").textContent = p.name;
  $("user-team").textContent = p.team;
}

function initPersonas() {
  const select = $("persona");
  for (const p of PERSONAS) select.append(el("option", { text: p.label, attrs: { value: p.id } }));
  select.value = auth.persona.id;
  renderPersona();
  select.addEventListener("change", async () => {
    auth.persona = PERSONAS.find((p) => p.id === select.value) || PERSONAS[0];
    auth.token = null;
    renderPersona();
    try {
      await mintToken();
    } catch {
      return;
    }
    showMessage(`Signed in as ${auth.persona.label}.`);
    // Reload what this persona may see (restricted runbooks differ by group).
    if (view.id) openIncident(view.id);
  });
}

// ------------------------------------------------------------------ main

function hashId() {
  let h = "";
  try {
    h = decodeURIComponent(location.hash.slice(1)).trim();
  } catch {
    return null;
  }
  return UUID_RE.test(h) ? h : null;
}

function drawStaticIcons() {
  for (const node of document.querySelectorAll("[data-icon]")) {
    node.replaceChildren(icon(node.dataset.icon));
  }
}

async function main() {
  drawStaticIcons();
  initTheme();
  initSidebar();
  initTabs();
  initSearch();
  initPersonas();
  renderRecent();
  renderAll();
  checkHealth();
  setInterval(checkHealth, HEALTH_EVERY_MS);
  setInterval(renderAge, AGE_EVERY_MS);
  setInterval(() => auth.token && renderTokenState(true), 1000);
  $("start").addEventListener("click", () => startReference($("start")));
  $("start-empty").addEventListener("click", () => startReference($("start-empty")));
  $("ex-q").addEventListener("input", () => {
    ui.query = $("ex-q").value;
    applyEvidenceFilter();
  });
  window.addEventListener("hashchange", () => {
    const id = hashId();
    if (id && id.toLowerCase() !== view.id) openIncident(id);
  });
  try {
    await mintToken();
  } catch {
    showMessage("Could not reach the fake token issuer through this page's proxy. Is the stack up?", "error");
    return;
  }
  const id = hashId();
  if (id) openIncident(id);
}

main();
