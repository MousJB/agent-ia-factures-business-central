"use strict";

const SAMPLE_LABELS = {
  "01": ["Facture conforme", "Fabrikam — commande 106001"],
  "02": ["Écart de montant", "Wide World Importers — prix +12 %"],
  "03": ["Doublon", "First Up Consultants — déjà saisie"],
  "04": ["Fournisseur inconnu", "Atelier Numérique Bordeaux"],
  "05": ["Total incohérent", "Graphic Design Institute — TTC faux"],
};
const STATUS_LABELS = {
  processing: "Traitement en cours",
  awaiting_validation: "Brouillon — à valider",
  blocked: "Bloquée — aucun brouillon",
  validated: "Validée",
  rejected: "Rejetée",
  failed: "Échec",
};
const RECO_LABELS = {
  validate: "✅ Recommandation de l'agent : valider",
  review: "⚠️ Recommandation de l'agent : à vérifier avant validation",
  reject: "⛔ Recommandation de l'agent : rejeter",
};
const SEVERITY_ICON = { error: "⛔", warning: "⚠️", info: "ℹ️" };

let currentJobId = null;
let pollTimer = null;

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const eur = (n) => (n == null ? "—" : Number(n).toLocaleString("fr-FR", { style: "currency", currency: "EUR" }));
const num = (n) => Number(n).toLocaleString("fr-FR", { maximumFractionDigits: 3 });
const frDate = (iso) => (iso ? new Date(iso + "T00:00:00").toLocaleDateString("fr-FR") : "—");

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => (el.hidden = true), 5000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let body = null;
  try { body = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const detail = body && body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : response.statusText;
    throw new Error(detail);
  }
  return body;
}

// ------------------------------------------------------------------ init
async function init() {
  try {
    const cfg = await api("/api/config");
    $("#badges").innerHTML = [
      `ERP : <b>${esc(cfg.erp_mode === "mock" ? "Mock CRONUS" : "Business Central")}</b>`,
      `LLM : <b>${esc(cfg.llm_provider)}</b>`,
      `Modèle : <b>${esc(cfg.llm_model)}</b>`,
    ].map((t) => `<span class="badge">${t}</span>`).join("");
    $("#resetBtn").hidden = cfg.erp_mode !== "mock";
  } catch (e) {
    toast("Configuration illisible : " + e.message, true);
  }

  const samples = await api("/api/samples").catch(() => []);
  $("#samples").innerHTML = samples.map((name) => {
    const [title, sub] = SAMPLE_LABELS[name.slice(0, 2)] || [name, ""];
    return `<button class="sample" data-name="${esc(name)}">${esc(title)}<small>${esc(sub)}</small></button>`;
  }).join("") || '<span class="muted">Aucun exemple (lancer scripts/generate_invoices.py).</span>';
  $("#samples").addEventListener("click", (ev) => {
    const btn = ev.target.closest(".sample");
    if (btn) startJob(api(`/api/samples/${encodeURIComponent(btn.dataset.name)}`, { method: "POST" }));
  });

  const drop = $("#dropzone");
  const input = $("#fileInput");
  input.addEventListener("change", () => input.files[0] && upload(input.files[0]));
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));

  $("#resetBtn").addEventListener("click", async () => {
    if (!confirm("Restaurer les données CRONUS d'origine et vider l'historique ?")) return;
    try {
      await api("/api/demo/reset", { method: "POST" });
      currentJobId = null;
      location.reload();
    } catch (e) { toast(e.message, true); }
  });
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("job");
  if (fromHash) {
    currentJobId = fromHash;
    api(`/api/invoices/${fromHash}`).then((job) => { renderJob(job, true); if (job.status === "processing") poll(job.id); })
      .catch(() => history.replaceState(null, "", location.pathname));
  }
  refreshHistory();
}

function upload(file) {
  if (file.type && file.type !== "application/pdf") return toast("Seuls les fichiers PDF sont acceptés.", true);
  const form = new FormData();
  form.append("file", file);
  startJob(api("/api/invoices", { method: "POST", body: form }));
  $("#fileInput").value = "";
}

async function startJob(promise) {
  try {
    const job = await promise;
    currentJobId = job.id;
    renderJob(job, true);
    poll(job.id);
    refreshHistory();
  } catch (e) {
    toast(e.message, true);
  }
}

function poll(jobId) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (jobId !== currentJobId) return;
    try {
      const job = await api(`/api/invoices/${jobId}`);
      renderJob(job);
      if (job.status === "processing") poll(jobId); else refreshHistory();
    } catch (e) {
      toast(e.message, true);
    }
  }, 600);
}

async function refreshHistory() {
  const jobs = await api("/api/invoices").catch(() => []);
  const list = $("#history");
  if (!jobs.length) { list.innerHTML = '<li class="muted">Aucun traitement.</li>'; return; }
  list.innerHTML = jobs.map((j) => `
    <li><button data-id="${esc(j.id)}" class="${j.id === currentJobId ? "active" : ""}">
      <span class="name">${esc(j.extracted?.invoice_number || j.filename)}</span>
      <span class="status ${esc(j.status)}">${esc(STATUS_LABELS[j.status] || j.status)}</span>
    </button></li>`).join("");
  list.onclick = async (ev) => {
    const btn = ev.target.closest("button[data-id]");
    if (!btn) return;
    currentJobId = btn.dataset.id;
    const job = await api(`/api/invoices/${currentJobId}`);
    renderJob(job, true);
    if (job.status === "processing") poll(job.id);
    refreshHistory();
  };
}

// ------------------------------------------------------------------ rendering
function renderJob(job, fresh = false) {
  let root = $("#content .job");
  if (fresh || !root || root.dataset.id !== job.id) {
    $("#content").innerHTML = "";
    root = $("#jobTemplate").content.firstElementChild.cloneNode(true);
    root.dataset.id = job.id;
    history.replaceState(null, "", `#job=${job.id}`);
    $("#content").appendChild(root);
    $(".pdf", root).src = `/api/invoices/${job.id}/pdf`;
    $(".validateBtn", root).addEventListener("click", () => decide(job.id, "validate"));
    $(".rejectBtn", root).addEventListener("click", () => decide(job.id, "reject"));
  }

  $(".job-title", root).textContent = job.extracted
    ? `${job.extracted.vendor_name} — facture ${job.extracted.invoice_number}`
    : job.filename;
  const duration = job.duration_seconds != null ? ` · traité en ${num(job.duration_seconds)} s` : "";
  $(".job-meta", root).textContent = `${job.filename} · LLM ${job.llm_provider} · ERP ${job.erp_mode}${duration}`;
  const status = $(".status", root);
  status.className = `status ${job.status}`;
  status.textContent = STATUS_LABELS[job.status] || job.status;

  renderDecision(root, job);
  renderAnomalies(root, job);
  renderExtracted(root, job);
  renderErp(root, job);
  renderTimeline(root, job);
}

function renderDecision(root, job) {
  const box = $(".decision", root);
  if (job.status === "processing") { box.hidden = true; return; }
  box.hidden = false;
  box.className = `decision card rec-${job.recommendation || "review"}`;
  let headline = RECO_LABELS[job.recommendation] || "";
  if (job.status === "validated") headline = "✅ Facture validée et comptabilisée dans l'ERP";
  if (job.status === "rejected") headline = "⛔ Facture rejetée";
  if (job.status === "failed") headline = "Échec du traitement";
  $(".recommendation", box).textContent = headline;
  $(".summary", box).textContent = job.error || job.summary || "";
  if (job.human_comment) $(".summary", box).textContent += `\nCommentaire : ${job.human_comment}`;

  const canValidate = job.status === "awaiting_validation";
  const canReject = ["awaiting_validation", "blocked", "failed"].includes(job.status);
  $(".validateBtn", box).hidden = !canValidate;
  $(".rejectBtn", box).hidden = !canReject;
  $(".comment", box).hidden = !(canValidate || canReject);
  $(".validateBtn", box).textContent = job.draft_invoice ? `Valider la facture ${job.draft_invoice.number}` : "Valider";
}

function renderAnomalies(root, job) {
  const el = $(".anomalies", root);
  if (job.status === "processing" && !job.anomalies.length) { el.innerHTML = '<span class="muted">Contrôles en cours…</span>'; return; }
  if (!job.anomalies.length) { el.innerHTML = '<div class="no-anomaly">Aucune anomalie détectée</div>'; return; }
  const order = { error: 0, warning: 1, info: 2 };
  el.innerHTML = [...job.anomalies].sort((a, b) => order[a.severity] - order[b.severity]).map((a) => `
    <div class="anomaly ${esc(a.severity)}">
      <span class="icon">${SEVERITY_ICON[a.severity] || ""}</span>
      <div><span class="code">${esc(a.code)}</span>${esc(a.message)}</div>
    </div>`).join("");
}

function renderExtracted(root, job) {
  const inv = job.extracted;
  const el = $(".extracted", root);
  if (!inv) { el.className = "extracted muted"; el.textContent = job.status === "processing" ? "En cours d'extraction…" : "—"; return; }
  el.className = "extracted";
  el.innerHTML = `
    <dl class="kv">
      <dt>Fournisseur</dt><dd>${esc(inv.vendor_name)}</dd>
      <dt>N° TVA</dt><dd>${esc(inv.vendor_vat_number || "—")}</dd>
      <dt>N° facture</dt><dd>${esc(inv.invoice_number)}</dd>
      <dt>Date</dt><dd>${frDate(inv.invoice_date)}</dd>
      <dt>Échéance</dt><dd>${frDate(inv.due_date)}</dd>
      <dt>Commande citée</dt><dd>${esc(inv.purchase_order_reference || "—")}</dd>
    </dl>
    <table>
      <thead><tr><th>Désignation</th><th class="num">Qté</th><th class="num">PU HT</th><th class="num">Montant HT</th></tr></thead>
      <tbody>${inv.lines.map((l) => `<tr><td>${esc(l.description)}</td><td class="num">${num(l.quantity)}</td>
        <td class="num">${eur(l.unit_price)}</td><td class="num">${eur(l.line_total ?? l.quantity * l.unit_price)}</td></tr>`).join("")}</tbody>
      <tfoot class="totals">
        <tr><td colspan="3">Total HT</td><td class="num">${eur(inv.subtotal_excl_tax)}</td></tr>
        <tr><td colspan="3">TVA</td><td class="num">${eur(inv.tax_amount)}</td></tr>
        <tr><td colspan="3">Total TTC</td><td class="num">${eur(inv.total_incl_tax)}</td></tr>
      </tfoot>
    </table>`;
}

function renderErp(root, job) {
  const el = $(".erp-data", root);
  const parts = [];
  if (job.vendor) parts.push(`<dt>Fournisseur ERP</dt><dd>${esc(job.vendor.number)} — ${esc(job.vendor.name)}</dd>`);
  if (job.purchase_order) {
    parts.push(`<dt>Bon de commande</dt><dd>${esc(job.purchase_order.number)} du ${frDate(job.purchase_order.order_date)} — ${eur(job.purchase_order.total_excl_tax)} HT</dd>`);
  }
  if (job.draft_invoice) {
    parts.push(`<dt>Facture d'achat</dt><dd>${esc(job.draft_invoice.number)} — statut <b>${esc(job.draft_invoice.status)}</b></dd>`);
  }
  if (!parts.length) { el.className = "erp-data muted"; el.textContent = job.status === "processing" ? "Recherche en cours…" : "Aucune correspondance."; return; }
  el.className = "erp-data";
  el.innerHTML = `<dl class="kv">${parts.join("")}</dl>`;
}

function renderTimeline(root, job) {
  const list = $(".timeline", root);
  const html = job.steps.map((s, i) => {
    const next = job.steps[i + 1];
    // A tool call is merged with its result; show the call alone only while it is pending.
    if (s.kind === "tool_call" && next && next.tool_name === s.tool_name && next.kind !== "tool_call") return "";
    const pending = s.kind === "tool_call" ? " pending" : "";
    const tool = s.tool_name ? `<span class="tool">${esc(s.tool_name)}()</span>` : "";
    const call = s.kind !== "tool_call" && s.tool_name ? job.steps[i - 1] : null;
    const payload = [
      call && call.payload ? `<details><summary>Arguments</summary><pre>${esc(JSON.stringify(call.payload, null, 2))}</pre></details>` : "",
      s.payload ? `<details><summary>${s.kind === "tool_call" ? "Arguments" : "Réponse de l'outil"}</summary><pre>${esc(JSON.stringify(s.payload, null, 2))}</pre></details>` : "",
    ].join("");
    const title = s.kind === "tool_result" || s.kind === "error" ? s.title.replace(/^Résultat — /, "") : s.title;
    return `<li class="${esc(s.kind)}${pending}"><div class="t-title">${esc(title)}${tool}</div>
      ${s.content ? `<div class="t-body">${esc(s.content)}</div>` : ""}${payload}</li>`;
  }).join("");
  const last = job.steps.at(-1);
  if (job.status === "processing" && !(last && last.kind === "tool_call")) {
    list.innerHTML = html + '<li class="thought pending"><div class="t-title">L\'agent réfléchit</div></li>';
  } else {
    list.innerHTML = html;
  }
}

async function decide(jobId, action) {
  const root = $("#content .job");
  const comment = $(".comment", root).value.trim() || null;
  if (action === "reject" && !confirm("Rejeter cette facture ? Le brouillon éventuel sera supprimé de l'ERP.")) return;
  root.querySelectorAll(".decision .btn").forEach((b) => (b.disabled = true));
  try {
    const job = await api(`/api/invoices/${jobId}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comment }),
    });
    renderJob(job);
    refreshHistory();
    toast(action === "validate" ? "Facture validée dans l'ERP." : "Facture rejetée.");
  } catch (e) {
    toast(e.message, true);
  } finally {
    root.querySelectorAll(".decision .btn").forEach((b) => (b.disabled = false));
  }
}

init();
