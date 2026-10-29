// PR Summarizer Bot dashboard.
// All data comes from the Flask API. Text is only ever inserted with textContent /
// createTextNode (never innerHTML), so untrusted PR titles or AI output cannot inject HTML.
"use strict";

const PAGE_SIZE = 10;
const POLL_MS = 5000;
const state = { offset: 0, total: 0, timer: null, loadId: 0 };

const $ = (id) => document.getElementById(id);

// ---- tiny DOM helper ------------------------------------------------------
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child);
  }
  return node;
}

// ---- API ------------------------------------------------------------------
async function api(path) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const resp = await fetch(path, { signal: controller.signal, headers: { Accept: "application/json" } });
    let data = null;
    try { data = await resp.json(); } catch (_) { /* non-JSON body */ }
    if (!resp.ok) {
      throw new Error((data && data.error) || `Request failed (HTTP ${resp.status})`);
    }
    return data;
  } catch (err) {
    if (err.name === "AbortError") throw new Error("The request timed out.");
    if (err instanceof TypeError) throw new Error("Cannot reach the backend.");
    throw err;
  } finally {
    clearTimeout(timeout);
  }
}

// ---- formatting helpers ---------------------------------------------------
function formatTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return isNaN(d) ? "–" : d.toLocaleString();
}

function safeGithubUrl(url) {
  return typeof url === "string" && url.startsWith("https://github.com/") ? url : null;
}

function statusLabel(item) {
  if (item.analysis_status === "success") return null;
  return { pending: "Pending", processing: "Analyzing…", failed: "Failed", skipped: "Skipped" }[item.analysis_status] || item.analysis_status;
}

function riskBadge(level) {
  if (!level) return null;
  const label = { LOW: "Low risk", MEDIUM: "Medium risk", HIGH: "High risk" }[level] || level;
  return el("span", { class: `badge badge-${String(level).toLowerCase()}`, text: label });
}

function statusBadge(item) {
  const label = statusLabel(item);
  return label ? el("span", { class: `badge badge-${item.analysis_status}`, text: label }) : null;
}

function oneLine(text, max = 160) {
  if (!text) return "";
  const flat = String(text).replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max - 1) + "…" : flat;
}

// ---- status + stats -------------------------------------------------------
function setBackendStatus(kind, text) {
  const badge = $("backend-status");
  badge.className = `badge badge-${kind}`;
  badge.textContent = text;
}

async function loadHealth() {
  try {
    const health = await api("/health?deep=1");
    if (health.database === "unavailable") setBackendStatus("error", "Backend up · database unavailable");
    else setBackendStatus("ok", "Backend online");
  } catch (err) {
    setBackendStatus("error", "Backend offline");
  }
}

async function loadStats() {
  const stats = await api("/api/stats");
  $("stat-total").textContent = stats.analyzed;
  $("stat-low").textContent = stats.low;
  $("stat-medium").textContent = stats.medium;
  $("stat-high").textContent = stats.high;
  const extra = [];
  if (stats.pending) extra.push(`${stats.pending} in progress`);
  if (stats.failed) extra.push(`${stats.failed} failed`);
  if (stats.skipped) extra.push(`${stats.skipped} skipped`);
  $("stat-extra").textContent = extra.join(" · ");
  return stats;
}

// ---- list -----------------------------------------------------------------
function showMessage(text, isError) {
  const box = $("message");
  box.hidden = !text;
  box.textContent = text || "";
  box.className = "message" + (isError ? " error" : "");
}

function prCard(item) {
  const url = safeGithubUrl(item.pr_url);
  const summary = item.analysis_status === "success"
    ? oneLine(item.summary)
    : (item.analysis_note ? oneLine(item.analysis_note) : "Waiting for analysis…");

  const detailsBtn = el("button", { type: "button", text: "View details", "aria-label": `View details for pull request ${item.pr_number} in ${item.repository_full_name}` });
  detailsBtn.addEventListener("click", () => openDetails(item.id));

  return el("article", { class: "pr-card" },
    el("div", { class: "pr-top" },
      el("div", { class: "pr-title", text: `#${item.pr_number} ${item.pr_title || "(no title)"}` }),
      el("div", { class: "badges" }, riskBadge(item.risk_level), statusBadge(item),
        item.analysis_scope === "partial" ? el("span", { class: "badge badge-medium", text: "Partial" }) : null)),
    el("div", { class: "pr-meta", text: `${item.repository_full_name} · by ${item.pr_author} · analyzed ${formatTime(item.analyzed_at || item.updated_at)} · ${item.files_count} file${item.files_count === 1 ? "" : "s"}` }),
    el("p", { class: "pr-summary", text: summary }),
    el("div", { class: "pr-actions" }, detailsBtn,
      url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: "Open on GitHub" }) : null));
}

async function loadList() {
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: state.offset });
  if ($("filter-risk").value) params.set("risk", $("filter-risk").value);
  if ($("filter-status").value) params.set("status", $("filter-status").value);
  const data = await api(`/api/summaries?${params}`);
  state.total = data.total;

  const list = $("list");
  list.replaceChildren(...data.items.map(prCard));
  if (data.total === 0) {
    const filtered = $("filter-risk").value || $("filter-status").value;
    showMessage(filtered
      ? "No pull requests match these filters."
      : "No pull requests analyzed yet. Open or update a pull request in a repository with the webhook configured and it will appear here.", false);
  } else {
    showMessage("", false);
  }
  const from = data.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + PAGE_SIZE, data.total);
  $("page-info").textContent = data.total ? `${from}–${to} of ${data.total}` : "";
  $("prev-btn").disabled = state.offset === 0;
  $("next-btn").disabled = state.offset + PAGE_SIZE >= data.total;
}

async function loadAll() {
  const myLoad = ++state.loadId;
  clearTimeout(state.timer);
  $("refresh-btn").disabled = true;
  $("refresh-btn").textContent = "Loading…";
  if (!$("list").children.length) showMessage("Loading…", false);
  loadHealth();
  try {
    const [stats] = await Promise.all([loadStats(), loadList()]);
    if (myLoad === state.loadId && stats.pending > 0) {
      state.timer = setTimeout(loadAll, POLL_MS); // keep refreshing while analyses are running
    }
  } catch (err) {
    $("list").replaceChildren();
    showMessage(`Could not load data: ${err.message}`, true);
  } finally {
    $("refresh-btn").disabled = false;
    $("refresh-btn").textContent = "Refresh";
  }
}

// ---- details dialog -------------------------------------------------------
function section(title, content) {
  return el("section", {}, el("h3", { text: title }), content);
}

function bulletList(items, emptyText) {
  if (!items || !items.length) return el("p", { text: emptyText });
  return el("ul", {}, ...items.map((i) => el("li", { text: String(i) })));
}

function filesTable(files) {
  if (!files || !files.length) return el("p", { text: "No file information stored." });
  const head = el("tr", {}, el("th", { text: "File" }), el("th", { text: "Status" }),
    el("th", { class: "num", text: "+" }), el("th", { class: "num", text: "−" }));
  const rows = files.map((f) => el("tr", {},
    el("td", { text: f.previous_filename ? `${f.previous_filename} → ${f.filename}` : f.filename }),
    el("td", { text: f.status }),
    el("td", { class: "num add", text: `+${f.additions}` }),
    el("td", { class: "num del", text: `−${f.deletions}` })));
  return el("table", { class: "files" }, el("thead", {}, head), el("tbody", {}, ...rows));
}

function renderDetails(d) {
  $("detail-title").textContent = `#${d.pr_number} ${d.pr_title || "(no title)"}`;
  const url = safeGithubUrl(d.pr_url);
  const body = $("detail-body");
  const limitation = d.analysis_note ? el("p", { class: "note", text: d.analysis_note }) : null;

  body.replaceChildren(
    el("p", { class: "pr-meta", text: `${d.repository_full_name} · ${d.head_branch} → ${d.base_branch} · by ${d.pr_author}` }),
    el("div", { class: "badges" }, riskBadge(d.risk_level), statusBadge(d) || el("span", { class: "badge badge-success", text: "Analyzed" }),
      d.analysis_scope ? el("span", { class: "badge badge-unknown", text: d.analysis_scope === "partial" ? "Partial analysis" : "Full analysis" }) : null),
    url ? el("p", {}, el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: "Open pull request on GitHub" })) : null,
    limitation,
    section("Summary", el("p", { text: d.summary || "No summary available for this record." })),
    section("Key changes", bulletList(d.key_changes, "None recorded.")),
    section("Risk reasons", bulletList(d.risk_reasons, "None recorded.")),
    section("Testing suggestions", bulletList(d.testing_suggestions, "None recorded.")),
    section("Suggested labels", bulletList(d.suggested_labels, "None recorded.")),
    section(`Changed files (${(d.files_changed || []).length})`, filesTable(d.files_changed)),
    section("Analysis info", el("p", { text: `Status: ${d.analysis_status}` + (d.model_name ? ` · Model: ${d.model_name}` : "") + ` · Last updated: ${formatTime(d.updated_at)}` })));
}

async function openDetails(id) {
  const dialog = $("detail-dialog");
  $("detail-title").textContent = "Loading…";
  $("detail-body").replaceChildren(el("p", { text: "Loading details…" }));
  if (!dialog.open) dialog.showModal();
  try {
    renderDetails(await api(`/api/summaries/${encodeURIComponent(id)}`));
  } catch (err) {
    $("detail-title").textContent = "Error";
    $("detail-body").replaceChildren(el("p", { class: "message error", text: `Could not load details: ${err.message}` }));
  }
}

// ---- wiring ---------------------------------------------------------------
$("refresh-btn").addEventListener("click", loadAll);
$("prev-btn").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE_SIZE); loadAll(); });
$("next-btn").addEventListener("click", () => { state.offset += PAGE_SIZE; loadAll(); });
for (const id of ["filter-risk", "filter-status"]) {
  $(id).addEventListener("change", () => { state.offset = 0; loadAll(); });
}
$("detail-close").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

loadAll();
