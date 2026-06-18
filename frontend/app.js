const API = "";
const ICONS = { collapse: "«", up: "↑", down: "↓", mail: "✉", play: "▶", file: "⊞", x: "✕", stop: "■" };
const VBUCKET = { valid: "p-green", risky: "p-amber", invalid: "p-red" };
const LABELS = {
  personalized_first_line: "First line", company_category: "Category",
  ideal_customers: "Ideal customers", product_complimentary: "Compliment Q",
  value_proposition: "Value prop",
};
const state = { listId: null, variableSet: "ascendly_lean", selectable: [], selected: [],
  poll: null, selectedLeads: new Set(), running: false, jobId: null,
  view: "table", client: "ascendly", labels: {} };

function $(id){ return document.getElementById(id); }
function pretty(k){ return state.labels[k] || LABELS[k] || k.replace(/_/g, " ").replace(/^\w/, c => c.toUpperCase()); }
function icons(){ document.querySelectorAll("[data-i]").forEach(e => e.textContent = ICONS[e.getAttribute("data-i")] || ""); }

async function api(path, opts){
  const r = await fetch(API + path, opts);
  if(!r.ok) throw new Error(await r.text());
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

async function loadEnrichments(){
  const e = await api(`/api/enrichments?variable_set=${state.variableSet}`);
  state.selectable = e.selectable;
  state.selected = e.selectable.slice();
  state.labels = e.labels || {};
  const wrap = $("enrichChips");
  wrap.innerHTML = "";
  e.always.forEach(k => {
    const c = document.createElement("span");
    c.className = "chip lock"; c.textContent = pretty(k) + " · always";
    wrap.appendChild(c);
  });
  e.selectable.forEach(k => {
    const c = document.createElement("span");
    c.className = "chip on"; c.dataset.k = k; c.textContent = pretty(k);
    c.onclick = () => {
      c.classList.toggle("on");
      state.selected = [...wrap.querySelectorAll(".chip.on:not(.lock)")].map(x => x.dataset.k);
      if(state.listId) refresh();
    };
    wrap.appendChild(c);
  });
}

async function loadLists(){
  const lists = await api("/api/lists");
  const nav = $("lists");
  nav.innerHTML = "";
  lists.forEach(l => {
    const a = document.createElement("a");
    if(l.id === state.listId) a.className = "on";
    a.innerHTML = `<span class="ic">▦</span><span class="lbl">${esc(l.name)}</span><span class="ct">${kfmt(l.count)}</span>`;
    a.onclick = () => selectList(l.id, l.name, l.count);
    nav.appendChild(a);
  });
}

function kfmt(n){ return n >= 1000 ? (n/1000).toFixed(1) + "k" : String(n); }
function esc(s){ const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

async function selectList(id, name, count){
  state.listId = id;
  showView("table");
  state.selectedLeads.clear();
  if(state.poll){ clearInterval(state.poll); state.poll = null; }
  state.running = false; updateRunUI();
  $("viewTitle").textContent = name;
  $("viewSub").textContent = `${count} leads · ${state.variableSet}`;
  await loadLists();
  await refresh();
}

async function refresh(){
  if(!state.listId) return;
  const d = await api(`/api/lists/${state.listId}`);
  renderGrid(d);
  renderBar(d);
}

function renderGrid(d){
  const leads = d.leads || [];
  $("empty").hidden = leads.length > 0;
  const grid = $("grid"); grid.hidden = leads.length === 0;
  const cols = ["Email · Reoon", "Title gate", "ICP", ...state.selected.map(pretty)];
  $("head").innerHTML = `<th class="cbx"><input type="checkbox" id="selAll"></th><th>Lead</th>` +
    cols.map(c => `<th>${esc(c)}</th>`).join("") +
    `<th style="color:var(--acc-tx);cursor:pointer">+ enrichment</th>`;
  const body = $("body"); body.innerHTML = "";
  leads.forEach(ld => {
    const r = ld.result || {};
    const tr = document.createElement("tr");
    const ck = state.selectedLeads.has(ld.id) ? "checked" : "";
    let cells = `<td class="cbx"><input type="checkbox" class="rowcb" data-id="${ld.id}" ${ck}></td>`;
    cells += `<td class="lead"><b>${esc(ld.first_name)} ${esc(ld.last_name)}</b>` +
      `<s>${esc(ld.company)}${ld.title ? " · " + esc(ld.title) : ""}</s></td>`;
    cells += `<td>${emailCell(ld, r)}</td>`;
    cells += `<td>${titleCell(ld, r)}</td>`;
    cells += `<td>${icpCell(ld, r)}</td>`;
    state.selected.forEach(k => { cells += `<td>${varCell(ld, r, k)}</td>`; });
    cells += `<td></td>`;
    tr.innerHTML = cells;
    body.appendChild(tr);
  });

  const selAll = $("selAll");
  if(selAll){
    selAll.checked = leads.length > 0 && leads.every(l => state.selectedLeads.has(l.id));
    selAll.onchange = () => {
      if(selAll.checked) leads.forEach(l => state.selectedLeads.add(l.id));
      else leads.forEach(l => state.selectedLeads.delete(l.id));
      renderGrid(d); updateScope();
    };
  }
  body.querySelectorAll(".rowcb").forEach(cb => {
    cb.onchange = () => {
      const id = +cb.dataset.id;
      cb.checked ? state.selectedLeads.add(id) : state.selectedLeads.delete(id);
      const sa = $("selAll"); if(sa) sa.checked = leads.every(l => state.selectedLeads.has(l.id));
      updateScope();
    };
  });
  updateScope();
}

function updateScope(){
  const n = state.selectedLeads.size;
  const info = $("selInfo"), lim = $("limWrap");
  if(n > 0){ info.hidden = false; info.textContent = `${n} selected — Run uses these`; lim.style.opacity = ".4"; }
  else { info.hidden = true; lim.style.opacity = "1"; }
}

function updateRunUI(){
  $("runBtn").hidden = state.running;
  $("stopBtn").hidden = !state.running;
  const b2 = $("runBtn2"); if(b2) b2.disabled = state.running;
}

function hasResult(ld){ return ld.result && Object.keys(ld.result).length > 0; }
function hasVerify(ld){ return ld.verify && Object.keys(ld.verify).length > 0; }

function emailCell(ld){
  if(!hasVerify(ld)) return ld.email ? `<span class="sk">—</span>` : `<span class="sk">no email</span>`;
  const status = (ld.email_status || "").toLowerCase();
  const safe = ld.verify.is_safe_to_send === true;
  let bkt = safe || ["safe","valid"].includes(status) ? "valid"
    : ["invalid","disposable","spamtrap","disabled"].includes(status) ? "invalid" : "risky";
  const label = status ? status.replace(/_/g, " ") : (safe ? "valid" : "risky");
  return `<span class="pill ${VBUCKET[bkt]}">${esc(label)}</span>`;
}
function titleCell(ld, r){
  if(!hasResult(ld)) return `<span class="sk">queued</span>`;
  if(r._title_gate === "rejected") return `<span class="pill p-red">✕ Rejected</span>`;
  return `<span class="pill p-green">Pass</span>`;
}
function icpCell(ld, r){
  if(!hasResult(ld)) return `<span class="sk">queued</span>`;
  if(r._title_gate === "rejected") return `<span class="sk">skipped</span>`;
  if(r.ICPReview === "ICP") return `<span class="pill p-acc">ICP</span><span class="reason">${esc(r.ICP_reason||"")}</span>`;
  return `<span class="pill p-gray">Non-ICP</span><span class="reason">${esc(r.ICP_reason||"")}</span>`;
}
function varCell(ld, r, k){
  if(!hasResult(ld)) return `<span class="sk">queued</span>`;
  const v = r[k];
  if(v === undefined || v === "") return `<span class="sk">—</span>`;
  if(v === "N/A") return `<span class="sk">N/A</span>`;
  return `<span class="ln" title="${esc(v)}">${esc(v)}</span>`;
}

function renderBar(d){
  const j = d.job, total = d.list.count;
  const bar = $("bar"), stat = $("stat"), cost = $("cost");
  if(!j){ stat.textContent = `${total} leads · ${state.selected.length} enrichments selected`;
    bar.style.width = "0%"; cost.textContent = ""; return; }
  const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
  bar.style.width = pct + "%";
  if(j.kind === "verify"){
    const s = j.summary || {};
    cost.textContent = `${j.cost || 0} cr`;
    const tail = j.status === "done" || j.status === "cancelled"
      ? ` · ${s.valid||0} valid · ${s.risky||0} risky · ${s.invalid||0} invalid` : "";
    stat.textContent = `${j.done} of ${j.total} verified${tail}${j.status === "cancelled" ? " · stopped" : ""}`;
  } else {
    cost.textContent = "$" + (j.cost || 0).toFixed(2);
    const tail = j.status === "done" || j.status === "cancelled"
      ? ` · ${j.icp} ICP · ${j.nonicp} Non-ICP · ${j.rejected} title-rejected` : ` · ${state.selected.length} enrichments`;
    stat.textContent = `${j.done} of ${j.total} enriched${tail}${j.status === "cancelled" ? " · stopped" : ""}`;
  }
}

function buildScope(total){
  if(state.selectedLeads.size > 0) return { scope: { lead_ids: [...state.selectedLeads] }, count: state.selectedLeads.size };
  const lim = parseInt($("limitN").value, 10);
  if(lim > 0) return { scope: { limit: lim }, count: Math.min(lim, total) };
  return { scope: {}, count: total };
}

async function startJob(kind){
  if(!state.listId || state.running) return;
  const d = await api(`/api/lists/${state.listId}`);
  const { scope, count } = buildScope(d.list.count);
  const verb = kind === "verify" ? "verify" : "enrich";
  const credit = kind === "verify" ? "Reoon" : "API";
  if(count > 50 && !confirm(`This will ${verb} ${count} leads and use ${credit} credit. Continue?`)) return;

  const ep = kind === "verify" ? "verify" : "run";
  const body = kind === "verify" ? scope : Object.assign({ enrichments: state.selected }, scope);
  const { job_id } = await api(`/api/lists/${state.listId}/${ep}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  state.jobId = job_id; state.running = true; updateRunUI();
  if(state.poll) clearInterval(state.poll);
  state.poll = setInterval(async () => {
    const j = await api(`/api/jobs/${job_id}`);
    await refresh();
    if(["done", "error", "cancelled"].includes(j.status)){
      clearInterval(state.poll); state.poll = null; state.running = false; updateRunUI();
      if(kind === "verify") loadBalance();
    }
  }, 500);
}

const run = () => startJob("enrich");
const verify = () => startJob("verify");

async function stop(){
  if(!state.jobId) return;
  await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
}

function exportCsv(){
  if(!state.listId) return;
  window.location = `/api/lists/${state.listId}/export`;
}

async function loadBalance(){
  try{
    const b = await api("/api/reoon/balance");
    const el = $("credits");
    if(!b.enabled) el.textContent = "Reoon: demo";
    else if(b.error) el.textContent = "Reoon: key error";
    else el.textContent = `Reoon: ${b.instant ?? 0} credits`;
  }catch(e){}
}

function showView(name){
  state.view = name;
  $("gridWrap").hidden = name !== "table";
  $("runbar").hidden = name !== "table";
  $("formatView").hidden = name !== "format";
  $("settingsView").hidden = name !== "settings";
  const t = name === "table";
  ["scope", "importBtn", "exportBtn", "verifyBtn", "runBtn", "stopBtn"].forEach(id => {
    const e = $(id); if(e) e.style.display = t ? "" : "none";
  });
  if(t) updateRunUI();
}

async function loadFormat(){
  showView("format");
  $("viewTitle").textContent = "Formats";
  $("viewSub").textContent = "Client profile & how each variable is written";
  if(!state.client) state.client = state.variableSet.split("_")[0];
  const [profiles, sets, customs] = await Promise.all([
    api("/api/profiles"), api("/api/variable-sets"),
    api("/api/custom-variables?variable_set=" + state.variableSet),
  ]);
  const profName = profiles.includes(state.client) ? state.client : profiles[0];
  const [profile, fmt] = await Promise.all([api("/api/profiles/" + profName), api("/api/format/" + state.variableSet)]);
  const idByName = {}; customs.forEach(c => idByName[c.name] = c.id);
  renderFormat(profile, fmt, profiles, sets, profName, idByName);
}

function renderFormat(profile, fmt, profiles, sets, profName, idByName){
  let h = `<div class="fv-sel">` +
    `<label>Client <select id="fProfile">` +
      profiles.map(p => `<option ${p === profName ? "selected" : ""}>${esc(p)}</option>`).join("") + `</select></label>` +
    `<label>Format set <select id="fSet">` +
      sets.map(s => `<option ${s === state.variableSet ? "selected" : ""}>${esc(s)}</option>`).join("") + `</select></label>` +
    `</div>`;
  h += `<div class="fv-h">Client profile <span class="muted">— who we're writing for</span></div><div class="card">`;
  if(profile.fields.length)
    profile.fields.forEach(f => { h += `<div class="kv"><div class="k">${esc(f.label)}</div><div class="v">${esc(f.value)}</div></div>`; });
  else h += `<div class="v sk">No profile fields for ${esc(profile.name)}.</div>`;
  h += `</div>`;
  h += `<div class="fv-h" style="display:flex;align-items:center">Variables <span class="muted" style="margin-left:6px">— what we generate & how to write them</span>` +
    `<button class="run" id="addVarBtn" style="margin-left:auto;padding:6px 11px">+ Add variable</button></div>`;
  h += builderHtml();
  fmt.variables.forEach(v => {
    const cid = idByName[v.name];
    h += `<div class="card vcard"><div class="vh"><span class="vname">${esc(v.label || v.name)}</span>` +
      (v.min_words ? `<span class="wr">${v.min_words}-${v.max_words} words</span>` : "") +
      (v.always ? `<span class="tag">always runs</span>` : "") +
      (v.custom ? `<span class="tag" style="background:var(--acc-bg);color:var(--acc-tx)">custom</span>` : "") +
      (cid ? `<span class="delx" data-del="${cid}" title="Delete">✕</span>` : "") + `</div>`;
    if(v.description) h += `<div class="vp">${esc(v.description)}</div>`;
    if(v.notes && v.notes.length) h += `<ul class="vn">` + v.notes.map(n => `<li>${esc(n)}</li>`).join("") + `</ul>`;
    h += `</div>`;
  });
  $("formatView").innerHTML = h;
  $("fProfile").onchange = e => { state.client = e.target.value; loadFormat(); };
  $("fSet").onchange = e => { state.variableSet = e.target.value; state.client = e.target.value.split("_")[0]; loadEnrichments(); loadFormat(); };
  $("addVarBtn").onclick = () => { const b = $("builder"); b.hidden = !b.hidden; };
  $("cvTemplate").oninput = detectPlaceholders;
  $("cvSave").onclick = saveCustom;
  $("cvCancel").onclick = () => { $("builder").hidden = true; };
  $("formatView").querySelectorAll("[data-del]").forEach(x => x.onclick = () => deleteCustom(x.dataset.del));
}

function builderHtml(){
  return `<div class="card builder" id="builder" hidden>
    <input id="cvName" placeholder="Variable name   e.g. Short pitch" />
    <textarea id="cvTemplate" rows="3" placeholder="Paste your format. Use {{placeholders}} for the parts to generate.\ne.g. {{ask about their client}}? We help {{industry}} get {{ideal customers}} by {{what we do}}."></textarea>
    <div class="brow">Whole-variable word range <input id="cvMin" type="number" min="1" placeholder="min" /> to <input id="cvMax" type="number" min="1" placeholder="max" /></div>
    <div class="phh">Placeholders</div>
    <div id="cvPlaceholders"><div class="sk">Add {{placeholders}} above to describe them here.</div></div>
    <div class="brow"><button class="run" id="cvSave">Save variable</button><button class="gbtn" id="cvCancel">Cancel</button></div>
  </div>`;
}

function detectPlaceholders(){
  const tpl = $("cvTemplate").value;
  const toks = [...new Set((tpl.match(/\{\{(.*?)\}\}/g) || []).map(s => s.slice(2, -2).trim()).filter(Boolean))];
  const wrap = $("cvPlaceholders");
  const prev = {};
  wrap.querySelectorAll("[data-tok]").forEach(el => {
    prev[el.dataset.tok] = { d: el.querySelector(".pdesc").value, mn: el.querySelector(".pmin").value,
      mx: el.querySelector(".pmax").value, ex: el.querySelector(".pex").value };
  });
  if(!toks.length){ wrap.innerHTML = `<div class="sk">Add {{placeholders}} above to describe them here.</div>`; return; }
  wrap.innerHTML = toks.map(t => {
    const p = prev[t] || {};
    return `<div class="phcard" data-tok="${esc(t)}"><div class="phtok">{{${esc(t)}}}</div>` +
      `<textarea class="pdesc" rows="2" placeholder="How to write this placeholder">${esc(p.d || "")}</textarea>` +
      `<div class="brow">words <input class="pmin" type="number" min="1" value="${esc(p.mn || "")}" /> to <input class="pmax" type="number" min="1" value="${esc(p.mx || "")}" /></div>` +
      `<textarea class="pex" rows="2" placeholder="Examples, one per line">${esc(p.ex || "")}</textarea></div>`;
  }).join("");
}

async function saveCustom(){
  const label = $("cvName").value.trim();
  if(!label){ alert("Give the variable a name first."); return; }
  const placeholders = [...$("cvPlaceholders").querySelectorAll("[data-tok]")].map(el => ({
    token: el.dataset.tok,
    description: el.querySelector(".pdesc").value.trim(),
    min_words: parseInt(el.querySelector(".pmin").value, 10) || null,
    max_words: parseInt(el.querySelector(".pmax").value, 10) || null,
    examples: el.querySelector(".pex").value.split("\n").map(s => s.trim()).filter(Boolean),
  }));
  const body = {
    variable_set: state.variableSet, label, template: $("cvTemplate").value,
    min_words: parseInt($("cvMin").value, 10) || null, max_words: parseInt($("cvMax").value, 10) || null,
    placeholders,
  };
  await api("/api/custom-variables", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  await loadEnrichments();
  loadFormat();
}

async function deleteCustom(id){
  if(!confirm("Delete this custom variable?")) return;
  await api("/api/custom-variables/" + id, { method: "DELETE" });
  await loadEnrichments();
  loadFormat();
}

async function loadSettings(){
  showView("settings");
  $("viewTitle").textContent = "Settings";
  $("viewSub").textContent = "Connections & defaults";
  let b = {}; try{ b = await api("/api/reoon/balance"); }catch(e){}
  const reoon = !b.enabled ? "Demo mode (no REOON_API_KEY set)"
    : (b.error ? "Key set, but balance check failed" : `Connected · ${b.instant ?? 0} instant credits`);
  const def = localStorage.getItem("defLimit") || "10";
  $("settingsView").innerHTML =
    `<div class="fv-h">Settings</div><div class="card">` +
    `<div class="kv"><div class="k">Reoon verification</div><div class="v">${esc(reoon)}</div></div>` +
    `<div class="kv"><div class="k">Enrichment</div><div class="v">Demo mode (set ENRICH_MODE=real to use the live engine)</div></div>` +
    `<div class="kv"><div class="k">Active format set</div><div class="v">${esc(state.variableSet)}</div></div>` +
    `<div class="kv"><div class="k">Default test cap</div><div class="v"><input id="defLimit" type="number" min="1" value="${esc(def)}"> leads</div></div>` +
    `</div>`;
  $("defLimit").onchange = e => { localStorage.setItem("defLimit", e.target.value); $("limitN").value = e.target.value; };
}

async function toggleWsMenu(){
  const menu = $("wsMenu");
  if(!menu.hidden){ menu.hidden = true; return; }
  const profiles = await api("/api/profiles");
  menu.innerHTML = profiles.map(p => `<a data-c="${esc(p)}">${esc(p)}</a>`).join("");
  menu.querySelectorAll("a").forEach(a => a.onclick = () => { setClient(a.dataset.c); menu.hidden = true; });
  menu.hidden = false;
}

async function setClient(c){
  state.client = c;
  $("wsName").textContent = c.charAt(0).toUpperCase() + c.slice(1);
  $("wsDot").textContent = c.charAt(0).toUpperCase();
  const sets = await api("/api/variable-sets");
  state.variableSet = sets.find(s => s === c + "_lean") || sets.find(s => s.startsWith(c + "_")) || state.variableSet;
  await loadEnrichments();
  if(state.view === "format") loadFormat();
  else if(state.view === "settings") loadSettings();
}

async function createList(){
  const name = ($("listName").value || "New list").trim();
  const file = $("fileInput").files[0];
  const { id } = await api("/api/lists", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, variable_set: state.variableSet }),
  });
  let count = 0;
  if(file){
    const fd = new FormData(); fd.append("file", file);
    const res = await api(`/api/lists/${id}/upload`, { method: "POST", body: fd });
    count = res.imported;
  }
  $("importPanel").hidden = true;
  $("listName").value = ""; $("fileInfo").textContent = "No file chosen"; $("fileInput").value = "";
  await loadLists();
  selectList(id, name, count);
}

function wireSidebar(){
  const app = $("app"), side = $("side"), handle = $("handle");
  let drag = false;
  $("collapseBtn").onclick = () => {
    app.classList.toggle("mini");
    if(!app.classList.contains("mini")) side.style.width = "";
  };
  handle.addEventListener("mousedown", e => { drag = true; e.preventDefault(); });
  document.addEventListener("mousemove", e => {
    if(!drag) return;
    const w = e.clientX - app.getBoundingClientRect().left;
    if(w < 130){ app.classList.add("mini"); side.style.width = ""; }
    else { app.classList.remove("mini"); side.style.width = Math.min(Math.max(w, 150), 320) + "px"; }
  });
  document.addEventListener("mouseup", () => { drag = false; });
}

function init(){
  icons();
  wireSidebar();
  $("importBtn").onclick = $("newListBtn").onclick = () => {
    showView("table");
    $("importPanel").hidden = !$("importPanel").hidden; $("enrichPanel").hidden = true;
  };
  $("importClose").onclick = () => { $("importPanel").hidden = true; };
  $("chooseBtn").onclick = () => $("fileInput").click();
  $("fileInput").onchange = e => {
    const f = e.target.files[0];
    $("fileInfo").textContent = f ? `${f.name}` : "No file chosen";
    if(f && !$("listName").value) $("listName").value = f.name.replace(/\.csv$/i, "");
  };
  $("createBtn").onclick = createList;
  $("enrichBtn").onclick = () => { showView("table"); $("enrichPanel").hidden = !$("enrichPanel").hidden; $("importPanel").hidden = true; };
  $("formatBtn").onclick = loadFormat;
  $("settingsBtn").onclick = loadSettings;
  $("wsBtn").onclick = e => { if(e.target.closest("#collapseBtn")) return; toggleWsMenu(); };
  $("runBtn").onclick = $("runBtn2").onclick = run;
  $("verifyBtn").onclick = verify;
  $("exportBtn").onclick = $("exportNav").onclick = exportCsv;
  $("stopBtn").onclick = stop;
  $("limitN").oninput = updateScope;
  const savedLimit = localStorage.getItem("defLimit"); if(savedLimit) $("limitN").value = savedLimit;
  showView("table");
  loadBalance();
  loadEnrichments().then(loadLists);
}

init();
