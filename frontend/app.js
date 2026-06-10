"use strict";

// agent-sentry single-page UI.
//
// Talks to the FastAPI backend (src/sentry_api.py): GET /skills builds the left nav, POST /run
// runs the selected skill and returns the briefing markdown for the right pane.
//
// Security:
//  - The API key is held ONLY in the module-level `apiKey` variable for this page session.
//    It is never written to localStorage/sessionStorage and never logged (CLAUDE.md).
//  - The briefing is LLM-generated markdown => rendered via DOMPurify.sanitize(marked.parse(..))
//    before it touches innerHTML. Everything else uses textContent.

// Skills that live-fetch via their own standalone harness (Glean + Linear + web), dispatched
// out-of-process by the API — slower, and not stored in Graphiti. Used only to set a UI hint;
// the backend is the source of truth for routing. Keep in sync with sentry_api._EXTERNAL_HARNESS.
const LIVE_FETCH = new Set(["daily-pulse"]);

let apiKey = "";          // in-memory only
let baseUrl = "";         // API origin, e.g. http://127.0.0.1:8787
let skills = [];          // [{name, description, briefing:{...}}]
let activeSkill = null;   // skill name currently shown
let running = false;      // guards concurrent /run calls
let lastBrief = null;     // {skill, briefing_path} the feedback widget targets
let fbRating = null;      // "up" | "down" — chosen rating for the current brief
let sendingFeedback = false;

const $ = (id) => document.getElementById(id);

// --- small helpers -------------------------------------------------------------------

function setStatus(el, msg, kind) {
  el.textContent = msg || "";
  el.className = "status" + (kind ? " " + kind : "");
}

function normBase(value) {
  return (value || "").trim().replace(/\/+$/, ""); // strip trailing slashes
}

async function api(path, options = {}) {
  const headers = Object.assign({ "X-API-Key": apiKey }, options.headers || {});
  if (options.body) headers["Content-Type"] = "application/json";
  const res = await fetch(baseUrl + path, { ...options, headers });
  return res;
}

// Map an error response to a user-facing message (no internal detail leaked).
async function errorMessage(res) {
  switch (res.status) {
    case 401: return "Invalid or missing API key.";
    case 404: return "Unknown skill.";
    case 429: {
      const retry = res.headers.get("Retry-After");
      return "Rate limit exceeded." + (retry ? ` Retry in ${retry}s.` : "");
    }
    case 503: return "API not configured (SENTRY_API_KEY unset on the server).";
    default: {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (_) { /* ignore */ }
      return detail || `Request failed (HTTP ${res.status}).`;
    }
  }
}

// --- connect + load skills -----------------------------------------------------------

async function connect() {
  baseUrl = normBase($("base-url").value);
  apiKey = $("api-key").value;
  const status = $("conn-status");

  if (!baseUrl) { setStatus(status, "Set an API base URL.", "err"); return; }
  if (!apiKey) { setStatus(status, "Enter the API key.", "err"); return; }

  setStatus(status, "Connecting…", "busy");
  try {
    const res = await api("/skills");
    if (!res.ok) { setStatus(status, await errorMessage(res), "err"); return; }
    skills = await res.json();
    renderNav();
    setStatus(status, `Connected — ${skills.length} skill(s).`, "ok");
  } catch (_) {
    setStatus(status, "Could not reach the API. Is it running and the base URL correct?", "err");
  }
}

function renderNav() {
  const nav = $("skill-nav");
  nav.replaceChildren();
  if (!skills.length) {
    const p = document.createElement("p");
    p.className = "nav-empty";
    p.textContent = "No skills found.";
    nav.appendChild(p);
    return;
  }
  for (const skill of skills) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nav-item";
    btn.title = skill.description || "";

    const name = document.createElement("span");
    name.className = "ni-name";
    name.textContent = skill.name;
    btn.appendChild(name);

    if (LIVE_FETCH.has(skill.name)) {
      const live = document.createElement("span");
      live.className = "ni-live";
      live.textContent = "live";
      live.title = "Live-fetch: runs its own harness (Glean + web); slower";
      name.appendChild(live);
    }

    const relay = (skill.briefing && skill.briefing.relay) || [];
    const relayLine = document.createElement("span");
    relayLine.className = "ni-relay";
    relayLine.textContent = relay.length ? "relay: " + relay.join(", ") : "no relay";
    btn.appendChild(relayLine);

    btn.addEventListener("click", () => selectSkill(skill.name));
    nav.appendChild(btn);
  }
}

// --- select + render a skill panel ---------------------------------------------------

function selectSkill(name) {
  activeSkill = name;
  const skill = skills.find((s) => s.name === name);
  if (!skill) return;

  // highlight nav
  for (const item of document.querySelectorAll(".nav-item")) {
    item.classList.toggle("active", item.querySelector(".ni-name").textContent === name);
  }

  $("placeholder").classList.add("hidden");
  $("skill-panel").classList.remove("hidden");
  $("skill-name").textContent = skill.name;
  $("skill-desc").textContent = skill.description || "";

  // Live-fetch skills run their own harness out-of-process — flag the trade-offs.
  const hint = $("skill-hint");
  if (LIVE_FETCH.has(name)) {
    hint.textContent =
      "Live-fetch skill — pulls Slack/Gong/Linear + the web at run time, so this is slower " +
      "(up to a few minutes) and is not stored in Graphiti. Timeframe + dry-run apply; the " +
      "prompt field is ignored. Dry run writes the briefing but skips the Slack DM.";
    hint.classList.remove("hidden");
  } else {
    hint.textContent = "";
    hint.classList.add("hidden");
  }

  // reset run state for the freshly selected skill
  $("prompt").value = "";
  $("timeframe").value = "";
  setStatus($("run-status"), "", null);
  $("chips").replaceChildren();
  $("output").replaceChildren();
  resetFeedback();
}

// Hide + clear the feedback widget. One feedback target per rendered brief, so this runs on
// skill switch and at the start of every run.
function resetFeedback() {
  lastBrief = null;
  fbRating = null;
  sendingFeedback = false;
  $("feedback").classList.add("hidden");
  $("fb-up").classList.remove("selected");
  $("fb-down").classList.remove("selected");
  $("fb-up").setAttribute("aria-pressed", "false");
  $("fb-down").setAttribute("aria-pressed", "false");
  $("fb-comment").value = "";
  $("fb-submit").disabled = true;
  setStatus($("fb-status"), "", null);
}

// --- run -----------------------------------------------------------------------------

async function runSkill(event) {
  event.preventDefault();
  if (running || !activeSkill) return;

  const status = $("run-status");
  const chips = $("chips");
  const output = $("output");
  chips.replaceChildren();
  output.replaceChildren();
  resetFeedback();

  const body = { skill: activeSkill, dry_run: $("dry-run").checked };
  const prompt = $("prompt").value.trim();
  if (prompt) body.prompt = prompt;
  const tf = parseInt($("timeframe").value, 10);
  if (Number.isFinite(tf) && tf > 0) body.timeframe_hours = tf;

  setRunning(true);
  const runningMsg = LIVE_FETCH.has(activeSkill)
    ? "Running… live fetch (Glean + web) can take a few minutes."
    : "Running… (agent calls can take a while)";
  setStatus(status, runningMsg, "busy");
  try {
    const res = await api("/run", { method: "POST", body: JSON.stringify(body) });
    if (!res.ok) { setStatus(status, await errorMessage(res), "err"); return; }
    const result = await res.json();
    renderResult(result);
    setStatus(status, "Done.", "ok");
  } catch (_) {
    setStatus(status, "Run failed: could not reach the API.", "err");
  } finally {
    setRunning(false);
  }
}

function setRunning(on) {
  running = on;
  $("run-btn").disabled = on;
}

function renderResult(result) {
  // status chips (textContent only — no HTML injection here)
  const chips = $("chips");
  const relayed = result.relayed || [];
  if (relayed.length) {
    for (const r of relayed) addChip(chips, "relayed: " + r, "relay");
  } else {
    addChip(chips, "not relayed", null);
  }
  addChip(chips, "graphiti: " + (result.graphiti_locked ? "locked" : "not locked"),
          result.graphiti_locked ? "locked" : null);
  if (result.briefing_path) addChip(chips, result.briefing_path, "path");

  // briefing markdown -> sanitized HTML
  const output = $("output");
  const content = result.content;
  if (!content) {
    output.textContent = "(no briefing content returned)";
    return;
  }
  const dirty = marked.parse(content);
  output.innerHTML = DOMPurify.sanitize(dirty);

  // A real briefing was produced -> let the user rate it so the agent can learn.
  if (result.briefing_path) {
    lastBrief = { skill: activeSkill, briefing_path: result.briefing_path };
    $("feedback").classList.remove("hidden");
  }
}

// --- feedback ------------------------------------------------------------------------

function chooseRating(rating) {
  fbRating = rating;
  const up = $("fb-up");
  const down = $("fb-down");
  up.classList.toggle("selected", rating === "up");
  down.classList.toggle("selected", rating === "down");
  up.setAttribute("aria-pressed", String(rating === "up"));
  down.setAttribute("aria-pressed", String(rating === "down"));
  $("fb-submit").disabled = false;
  // 👎 nudges for a reason — distilling a rule needs the comment; a bare 👎 is just a signal.
  $("fb-comment-label").textContent =
    rating === "down" ? "What was off? (so the agent can fix it next time)" : "Comment (optional)";
  if (rating === "down") $("fb-comment").focus();
}

async function sendFeedback() {
  if (sendingFeedback || !lastBrief || !fbRating) return;
  const status = $("fb-status");
  const body = {
    skill: lastBrief.skill,
    briefing_path: lastBrief.briefing_path,
    rating: fbRating,
    comment: $("fb-comment").value.trim() || undefined,
    dry_run: $("dry-run").checked,
  };

  sendingFeedback = true;
  $("fb-submit").disabled = true;
  setStatus(status, "Sending… (the agent is learning from this)", "busy");
  try {
    const res = await api("/feedback", { method: "POST", body: JSON.stringify(body) });
    if (!res.ok) {
      setStatus(status, await errorMessage(res), "err");
      $("fb-submit").disabled = false;
      return;
    }
    const result = await res.json();
    // result.summary is the agent's receipt text — show it as plain text (no HTML injection).
    const note = (result.summary || "").split("\n").find((l) => l.trim()) || "Thanks — feedback recorded.";
    setStatus(status, note, "ok");
  } catch (_) {
    setStatus(status, "Could not send feedback: API unreachable.", "err");
    $("fb-submit").disabled = false;
  } finally {
    sendingFeedback = false;
  }
}

function addChip(parent, text, kind) {
  const span = document.createElement("span");
  span.className = "chip" + (kind ? " " + kind : "");
  span.textContent = text;
  parent.appendChild(span);
}

// --- wire up -------------------------------------------------------------------------

$("connect-btn").addEventListener("click", connect);
$("api-key").addEventListener("keydown", (e) => { if (e.key === "Enter") connect(); });
$("run-form").addEventListener("submit", runSkill);
$("fb-up").addEventListener("click", () => chooseRating("up"));
$("fb-down").addEventListener("click", () => chooseRating("down"));
$("fb-submit").addEventListener("click", sendFeedback);

// --- bootstrap from .env-provided config ---------------------------------------------
// frontend/config.js (generated by start-ui.sh from .env) sets window.SENTRY_CONFIG with the
// API base URL + key, so credentials come from .env and the user is never asked for them. The
// key stays in the in-memory `apiKey` var only (set via connect()), same as the manual path.
function autoConnect() {
  const cfg = window.SENTRY_CONFIG;
  if (!cfg || !cfg.apiKey) return; // no injected creds -> leave the manual connect form visible
  if (cfg.baseUrl) $("base-url").value = cfg.baseUrl;
  $("api-key").value = cfg.apiKey;
  $("conn-creds").classList.add("hidden"); // creds are from .env; don't prompt for them
  connect();
}
autoConnect();
