// ADR-70: the read-only demo viewer. Plain ES module, no build step, no dependencies.
//
// Same origin as the API through the demo-viewer proxy (demo/nginx.conf):
//   POST /token                                   FAKE token issuer: a persona's bearer token
//   POST /v1/incidents                            start the reference incident (the only write)
//   GET  /v1/incidents/{id}                       header, status, spend (budget)
//   GET  /v1/incidents/{id}/evidence              evidence list (paged by cursor)
//   GET  /v1/incidents/{id}/diagnoses/current     diagnosis, hypotheses, citations (404: none yet)
//   GET  /v1/incidents/{id}/timeline/stream       live timeline, SSE, read with fetch so the
//                                                 Authorization header can be sent
//
// SECURITY: every value from the API (evidence, model output, titles, IDs) is untrusted. It is
// put on the page only through textContent and createElement (see el()), never parsed as HTML,
// and never used as a link target.

const TOKEN_TTL_S = 300; // the fake issuer's default; the API re-checks streams every 5 minutes
const REFRESH_EARLY_S = 60; // mint a new token this long before the old one expires
const RETRY_DEFAULT_MS = 3000; // the stream's own `retry:` replaces it
const RETRY_MAX_MS = 30000;
const FINISHED = new Set(["closed", "cancelled"]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const PERSONAS = [
  {
    id: "responder",
    label: "Responder, orders team (all services)",
    sub: "demo-responder",
    groups: ["aisre:responder", "orders-team"],
  },
  {
    id: "responder-outside",
    label: "Responder outside the orders team (runbooks restricted)",
    sub: "demo-responder-outside",
    groups: ["aisre:responder"],
  },
  {
    id: "viewer",
    label: "Viewer, orders team (read only, cannot start)",
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
  "aisre.action.proposed.v1": "Fix proposed",
  "aisre.action.tested.v1": "Fix tested",
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
    if (v !== undefined && v !== null) node.setAttribute(k, String(v));
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

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z").replace(/:00Z$/, "Z");
}

function fmtClock(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toISOString().slice(11, 23) + "Z";
}

function fmtUsd(micros) {
  if (typeof micros !== "number") return "–";
  return "$" + (micros / 1e6).toFixed(micros < 1e6 ? 4 : 2);
}

function showMessage(text, kind = "info") {
  const m = $("message");
  m.textContent = text;
  m.dataset.kind = kind;
  m.hidden = !text;
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
  s.textContent = `Token valid ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} · groups ${auth.persona.groups.join(", ")}`;
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
  evidence: [], // EvidenceRecord, in the API's order
  evidenceLabel: new Map(), // evidence_id -> "E3"
  diagnosis: null,
  hypothesesFromEvents: new Map(), // hypothesis_id -> data, until a diagnosis lists them
  statusTrail: [], // statuses the incident has passed through, from the timeline
};

function resetView(id) {
  view.id = id;
  view.incident = null;
  view.events = new Map();
  view.lastSeq = 0;
  view.evidence = [];
  view.evidenceLabel = new Map();
  view.diagnosis = null;
  view.hypothesesFromEvents = new Map();
  view.statusTrail = [];
  clear($("timeline"));
  $("tl-count").textContent = "";
  $("evidence").replaceChildren(el("li", { className: "empty", text: "None yet." }));
  $("hypotheses").replaceChildren(el("li", { className: "empty", text: "None yet." }));
  $("diagnosis").replaceChildren(
    el("p", { className: "empty", text: "No diagnosis yet. It appears once the evidence is in." }),
  );
  clear($("diag-badges"));
  $("ev-count").textContent = "";
  $("hyp-count").textContent = "";
}

async function openIncident(id) {
  if (!UUID_RE.test(id)) {
    showMessage("That is not an incident ID (expected a UUID).", "error");
    return;
  }
  id = id.toLowerCase();
  if (location.hash.slice(1) !== id) history.replaceState(null, "", `#${id}`);
  $("open-id").value = id;
  stream.stop();
  resetView(id);
  $("incident").hidden = false;
  try {
    await refreshIncident();
  } catch (e) {
    $("incident").hidden = true;
    view.id = null;
    showMessage(
      e.status === 404
        ? "No such incident, or this persona may not read it (the API answers both the same way)."
        : `Could not read the incident: ${e.message}`,
      "error",
    );
    return;
  }
  await Promise.allSettled([refreshEvidence(), refreshDiagnosis()]);
  stream.start();
}

async function refreshIncident() {
  const { body } = await api(`/v1/incidents/${view.id}`);
  if (body.incident_id !== view.id) return;
  view.incident = body;
  renderIncident();
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
  renderDiagnosis();
  renderHypotheses();
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
    setLive("reconnecting", `Reconnecting in ${Math.round(delay / 1000)} s (${why})`);
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
        setLive("error", `Stream refused (${res.status})`);
        return;
      }
      this.scheduleReconnect(`HTTP ${res.status}`);
      return;
    }
    setLive("live", "Live");
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
      setLive("idle", `Stream ended: incident ${STATUS_LABEL[status].toLowerCase()}`);
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
    onEvent(event);
  },
};

function setLive(state, text) {
  const l = $("live");
  l.dataset.state = state;
  l.textContent = text;
}

function onEvent(event) {
  const type = String(event.type || "");
  const data = event.data || {};
  appendTimeline(event);
  if (type === "aisre.incident.status_changed.v1" && data.to_status) {
    view.statusTrail.push(data.to_status);
  } else if (type === "aisre.incident.handed_over.v1") {
    view.statusTrail.push("handed_over");
  } else if (type === "aisre.incident.closed.v1") {
    view.statusTrail.push("closed");
  } else if (type === "aisre.incident.cancelled.v1") {
    view.statusTrail.push("cancelled");
  } else if (type === "aisre.hypothesis.created.v1" && data.hypothesis_id) {
    view.hypothesesFromEvents.set(String(data.hypothesis_id), data);
    if (!view.diagnosis) renderHypotheses();
  }
  if (type.startsWith("aisre.incident.") || type.startsWith("aisre.diagnosis.")) {
    scheduleRefresh("incident");
  }
  if (type.startsWith("aisre.evidence.")) scheduleRefresh("evidence");
  if (type.startsWith("aisre.hypothesis.") || type.startsWith("aisre.diagnosis.")) {
    scheduleRefresh("diagnosis");
    scheduleRefresh("incident"); // spend moves with each model call
  }
  renderStepper();
}

// ------------------------------------------------------------------ rendering

function renderIncident() {
  const inc = view.incident;
  if (!inc) return;
  $("inc-ref").textContent = inc.display_ref;
  $("inc-scope").textContent = `${inc.service} / ${inc.environment}`;
  $("inc-title").textContent = inc.title;
  $("inc-started").textContent = `Symptom started ${fmtTime(inc.started_at)}`;
  $("inc-created").textContent = `opened ${fmtTime(inc.created_at)}`;
  const pill = $("inc-status");
  pill.textContent = STATUS_LABEL[inc.status] || inc.status;
  pill.dataset.tone = STATUS_TONE[inc.status] || "";
  renderStepper();
  renderSpend();
}

function renderStepper() {
  const inc = view.incident;
  const status = inc ? inc.status : "open";
  const seen = new Set(["open", ...view.statusTrail, status]);
  const verdict = seen.has("inconclusive") ? "inconclusive" : "diagnosed";
  const final = seen.has("cancelled") ? "cancelled" : "closed";
  const order = ["open", "investigating", verdict, "handed_over", final];
  const rank = (s) => {
    if (s === "open") return 0;
    if (s === "investigating") return 1;
    if (s === "handed_over") return 3;
    if (s === "closed" || s === "cancelled") return 4;
    return 2; // diagnosed, inconclusive, and the later M2+ states before a hand-over
  };
  const current = rank(status);
  const hints = {
    open: "alert accepted",
    investigating: "collecting evidence",
    diagnosed: "root cause found",
    inconclusive: "not established",
    handed_over: "people take over",
    closed: "by a person",
    cancelled: "stopped",
  };
  const list = $("stepper");
  clear(list);
  order.forEach((s, i) => {
    let state = "todo";
    if (i < current) state = "done";
    if (i === current) state = FINISHED.has(status) || status === "handed_over" ? "done" : "current";
    if (i === 2 && s === "inconclusive" && i <= current) state = "warn";
    if (i === 4 && s === "cancelled" && i <= current) state = "bad";
    if (i === current && i === 2 && !["diagnosed", "inconclusive"].includes(status)) {
      state = "current"; // a later state such as testing; its name is on the pill
    }
    list.append(
      el("li", { data: { state }, attrs: { "aria-current": i === current ? "step" : null } }, [
        STATUS_LABEL[s],
        el("small", { text: hints[s] }),
      ]),
    );
  });
}

function renderSpend() {
  const b = view.incident && view.incident.budget;
  const dl = $("spend");
  clear(dl);
  if (!b) {
    dl.append(el("dt", { text: "Not available" }), el("dd"));
    return;
  }
  const pct = b.max_cost_micros ? Math.min(100, (100 * b.spent_cost_micros) / b.max_cost_micros) : 0;
  const meter = el("div", { className: "meter", attrs: { role: "img", "aria-label": `${pct.toFixed(1)}% of the cost cap spent` } }, [
    el("span"),
  ]);
  meter.firstChild.style.width = `${pct}%`;
  dl.append(
    el("dt", { text: "Spent" }),
    el("dd", { text: `${fmtUsd(b.spent_cost_micros)} of ${fmtUsd(b.max_cost_micros)}` }),
    meter,
    el("dt", { text: "Reserved" }),
    el("dd", { text: fmtUsd(b.reserved_cost_micros) }),
    el("dt", { text: "Model calls" }),
    el("dd", { text: `${b.used_calls} of ${b.max_calls}` }),
  );
  dl.append(
    el("dt", { className: "note", text: "Fake provider: nothing billed; list price on its token counts." }),
  );
  dl.lastChild.style.gridColumn = "1 / -1";
  dl.lastChild.style.fontSize = "0.75rem";
}

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
    case "aisre.evidence.collected.v1": {
      const tier = d.strength_tier ? ` · tier ${d.strength_tier}` : "";
      return `${d.kind} from ${d.source_system}${tier}${d.is_partial ? " · partial" : ""}`;
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
      return `revision ${d.revision}, ${d.category}, ${d.confidence} confidence`;
    case "aisre.diagnosis.inconclusive.v1":
      return `revision ${d.revision}${d.stop_reason ? `: ${d.stop_reason}` : ""}`;
    case "aisre.diagnosis.reviewed.v1":
      return `revision ${d.revision}${d.review_notes ? `: ${d.review_notes}` : ""}`;
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

function appendTimeline(event) {
  const list = $("timeline");
  const seq = Number(event.sequence);
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  const item = el("li", { className: "tl fresh", data: { seq } }, [
    el("span", { className: "tl-seq", text: `#${seq}` }),
    el("div", {}, [
      el("div", {}, [
        el("span", {
          className: "tl-name",
          text: EVENT_LABEL[event.type] || event.type,
          data: { group: eventGroup(event.type) },
          attrs: { title: event.type },
        }),
      ]),
      describeEvent(event) ? el("div", { className: "tl-detail", text: describeEvent(event) }) : null,
      el("div", { className: "tl-time", text: `${fmtClock(event.time)} · ${event.actor || ""}` }),
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
  $("tl-count").textContent = `${view.events.size} events`;
  if (atBottom) list.scrollTop = list.scrollHeight;
}

function tierTag(tier) {
  if (!tier) return el("span", { className: "tag", text: "No tier", attrs: { title: "No strength tier assigned" } });
  const [name, cls] = TIERS[tier] || [`Tier ${tier}`, "supp"];
  return el("span", { className: `tag tier-${cls}`, text: `Tier ${tier} · ${name}` });
}

function renderEvidence() {
  const list = $("evidence");
  const filter = $("ev-filter");
  const kinds = [...new Set(view.evidence.map((e) => e.kind))].sort();
  const chosen = filter.value;
  clear(filter);
  filter.append(el("option", { text: "All", attrs: { value: "" } }));
  for (const k of kinds) filter.append(el("option", { text: k, attrs: { value: k } }));
  filter.value = kinds.includes(chosen) ? chosen : "";

  clear(list);
  const gaps = view.evidence.filter((e) => e.kind === "gap").length;
  $("ev-count").textContent = view.evidence.length
    ? `${view.evidence.length} records${gaps ? `, ${gaps} gap${gaps > 1 ? "s" : ""}` : ""}`
    : "";
  if (!view.evidence.length) {
    list.append(el("li", { className: "empty", text: "None yet." }));
    return;
  }
  for (const e of view.evidence) {
    const isGap = e.kind === "gap";
    const attrs = e.attributes || {};
    const restricted = attrs.restricted === true;
    const tags = [
      el("span", { className: "ev-label", text: view.evidenceLabel.get(e.evidence_id) }),
      el("span", { className: isGap ? "tag gap" : "tag kind", text: isGap ? "Gap" : e.kind }),
      isGap ? null : tierTag(e.strength_tier),
      restricted ? el("span", { className: "tag warn", text: "Restricted" }) : null,
      e.is_partial ? el("span", { className: "tag warn", text: "Partial" }) : null,
      e.flagged_injection ? el("span", { className: "tag bad", text: "Instruction flagged" }) : null,
      e.content_removed_at ? el("span", { className: "tag bad", text: "Content removed" }) : null,
      el("span", { className: "ev-source", text: `${e.source_system} · ${e.tool}` }),
    ];
    const body = isGap
      ? el("pre", { text: `Not collected: ${e.gap_reason}` })
      : el("pre", { text: e.summary });
    const foot = [];
    if (restricted) {
      foot.push(`Document ${attrs.doc_id ?? "?"} version ${attrs.doc_version ?? "?"}; text shown only to its permitted groups (owner: ${attrs.owner ?? "unknown"}).`);
    } else if (attrs.doc_id) {
      foot.push(`Document ${attrs.doc_id} version ${attrs.doc_version ?? "?"}`);
    }
    if (e.window_start || e.window_end) foot.push(`Window ${fmtTime(e.window_start)} to ${fmtTime(e.window_end)}`);
    foot.push(`Collected ${fmtTime(e.collected_at)}`);
    if (e.content_removed_reason) foot.push(`Removed: ${e.content_removed_reason}`);
    const item = el(
      "li",
      {
        className: `ev${isGap ? " gap" : ""}`,
        attrs: { id: `ev-${e.evidence_id}`, tabindex: "-1" },
        data: { kind: e.kind },
      },
      [el("div", { className: "ev-top" }, tags), body, el("div", { className: "ev-foot", text: foot.join(" · ") })],
    );
    item.hidden = Boolean(filter.value) && e.kind !== filter.value;
    list.append(item);
  }
}

/** A citation chip: click scrolls to the evidence item and highlights it. */
function citation(evidenceId, relation) {
  const label = view.evidenceLabel.get(evidenceId) || "E?";
  const btn = el("button", {
    className: `cite${relation === "contradicts" ? " contradicts" : ""}`,
    text: relation === "contradicts" ? `${label} contradicts` : label,
    attrs: { type: "button", title: `Evidence ${evidenceId}` },
  });
  btn.addEventListener("click", () => focusEvidence(evidenceId));
  return btn;
}

function focusEvidence(evidenceId) {
  const filter = $("ev-filter");
  if (filter.value) {
    filter.value = "";
    applyFilter();
  }
  const target = document.getElementById(`ev-${evidenceId}`);
  if (!target) {
    showMessage("That evidence record isn't in the list (yet).", "warn");
    return;
  }
  const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "center" });
  target.focus({ preventScroll: true });
  target.classList.remove("flash");
  void target.offsetWidth; // restart the highlight
  target.classList.add("flash");
  setTimeout(() => target.classList.remove("flash"), 2200);
}

function applyFilter() {
  const v = $("ev-filter").value;
  for (const item of $("evidence").children) {
    if (item.dataset.kind) item.hidden = Boolean(v) && item.dataset.kind !== v;
  }
}

function renderDiagnosis() {
  const box = $("diagnosis");
  const badges = $("diag-badges");
  clear(badges);
  const d = view.diagnosis;
  if (!d) {
    box.replaceChildren(
      el("p", { className: "empty", text: "No diagnosis yet. It appears once the evidence is in." }),
    );
    return;
  }
  // ADR-69: show the reviewed final outcome and confidence, not the diagnoser's own.
  const outcome = d.final_outcome || d.outcome;
  const confidence = d.final_confidence || d.confidence;
  const established = outcome === "established";
  badges.append(
    el("span", { className: "pill", data: { tone: established ? "ok" : "warn" }, text: established ? "Established" : "Inconclusive" }),
    el("span", { className: "pill", text: `${confidence} confidence` }),
    el("span", { className: "pill", text: d.category.replace(/_/g, " ") }),
    el("span", {
      className: "pill",
      data: { tone: d.reviewed ? "ok" : "warn" },
      text: d.reviewed ? "Reviewed" : "Not reviewed",
    }),
  );

  const parts = [];
  parts.push(
    el("p", {
      className: "diag-headline",
      data: { tone: established ? "ok" : "warn" },
      text: established ? "Root cause established" : "Root cause not established",
    }),
  );
  const leading = (d.hypotheses || []).find((h) => h.hypothesis_id === d.leading_hypothesis_id);
  if (leading && established) {
    parts.push(
      el("div", { className: "root-cause" }, [
        el("span", { className: "label", text: "Root cause" }),
        el("span", { text: leading.statement }),
      ]),
    );
  }
  parts.push(el("p", { className: "summary", text: d.summary }));

  if (d.claims && d.claims.length) {
    parts.push(el("h4", { className: "subhead", text: "Claims and their evidence" }));
    parts.push(
      el(
        "ol",
        { className: "claims" },
        d.claims.map((c) => el("li", {}, [c.text, ...c.evidence_ids.map((id) => citation(id, "supports"))])),
      ),
    );
  }
  if (d.ruled_out && d.ruled_out.length) {
    parts.push(el("h4", { className: "subhead", text: "Ruled out" }));
    parts.push(el("ul", { className: "plain-list" }, d.ruled_out.map((r) => el("li", { text: r }))));
  }
  if (d.missing_evidence && d.missing_evidence.length) {
    parts.push(el("h4", { className: "subhead", text: "Missing evidence" }));
    parts.push(el("ul", { className: "plain-list" }, d.missing_evidence.map((r) => el("li", { text: r }))));
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
  $("hyp-count").textContent = items.length ? `${items.length}` : "";
  if (!items.length) {
    list.append(el("li", { className: "empty", text: "None yet." }));
    return;
  }
  for (const h of items) {
    const status = h.status || "open";
    const [label, tone] = HYP_STATUS[status] || [status, ""];
    const leading = view.diagnosis && view.diagnosis.leading_hypothesis_id === h.hypothesis_id;
    const top = el("div", { className: "hyp-top" }, [
      el("span", { className: "tag", text: `H${h.ordinal}` }),
      el("span", { className: "pill", data: { tone }, text: label }),
      el("span", { className: "tag", text: `${h.confidence} confidence` }),
      el("span", { className: "tag", text: String(h.category || "").replace(/_/g, " ") }),
      leading ? el("span", { className: "tag tier-mech", text: "Leading" }) : null,
    ]);
    const children = [top, el("p", { className: "hyp-statement", text: h.statement })];
    if (h.status_reason && status !== "open") {
      children.push(el("p", { className: "hyp-tests", text: `Status: ${h.status_reason}` }));
    }
    if (h.confirm_test) children.push(el("p", { className: "hyp-tests", text: `Confirmed if: ${h.confirm_test}` }));
    if (h.refute_test) children.push(el("p", { className: "hyp-tests", text: `Refuted if: ${h.refute_test}` }));
    if (h.evidence && h.evidence.length) {
      children.push(
        el("div", { className: "hyp-links" }, [
          el("span", { className: "hyp-tests", text: "Evidence: " }),
          ...h.evidence.map((link) => citation(link.evidence_id, link.relation)),
        ]),
      );
    }
    list.append(
      el(
        "li",
        { className: `hyp${status === "ruled_out" ? " hyp-ruled" : ""}`, data: { status } },
        children,
      ),
    );
  }
}

// ------------------------------------------------------------------ actions

async function startReference() {
  const button = $("start");
  button.disabled = true;
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
    button.disabled = false;
  }
}

function hashId() {
  const h = decodeURIComponent(location.hash.slice(1)).trim();
  return UUID_RE.test(h) ? h : null;
}

function initPersonas() {
  const select = $("persona");
  for (const p of PERSONAS) select.append(el("option", { text: p.label, attrs: { value: p.id } }));
  select.value = auth.persona.id;
  select.addEventListener("change", async () => {
    auth.persona = PERSONAS.find((p) => p.id === select.value) || PERSONAS[0];
    auth.token = null;
    try {
      await mintToken();
    } catch {
      return;
    }
    // Reload what this persona may see (restricted runbooks differ by group).
    if (view.id) openIncident(view.id);
  });
}

async function main() {
  initPersonas();
  setInterval(() => auth.token && renderTokenState(true), 1000);
  $("start").addEventListener("click", startReference);
  $("open-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const id = $("open-id").value.trim();
    showMessage("");
    openIncident(id);
  });
  $("ev-filter").addEventListener("change", applyFilter);
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
