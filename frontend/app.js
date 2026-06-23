const API = "";
const ICONS = { collapse: "«", up: "↑", down: "↓", mail: "✉", play: "▶", file: "⊞", x: "✕", stop: "■", cols: "▦", tag: "▤" };
const VBUCKET = { valid: "p-green", risky: "p-amber", invalid: "p-red" };
const state = { listId: null, variableSet: "ascendly_lean", selectable: [], selected: [],
  poll: null, selectedLeads: new Set(), running: false, jobId: null,
  view: "table", client: "ascendly", labels: {}, editId: null, filter: "all", industryFilter: "" };

function leadCat(ld){
  if(!ld.result || Object.keys(ld.result).length === 0) return "notrun";
  const r = ld.result;
  if(r._status === "error") return "error";
  if(r._title_gate === "rejected") return "rejected";
  if(r.ICPReview === "Non-ICP") return "nonicp";
  return "enriched";
}

function isSafeLead(ld){
  const v = ld.verify || {};
  const st = (ld.email_status || "").toLowerCase();
  return v.is_safe_to_send === true || ["safe", "valid"].includes(st);
}

function $(id){ return document.getElementById(id); }
function pretty(k){ return state.labels[k] || (k || "").replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()); }
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
  const lists = await api("/api/lists?variable_set=" + encodeURIComponent(state.variableSet));
  const nav = $("lists");
  nav.innerHTML = "";
  lists.forEach(l => {
    const a = document.createElement("a");
    if(l.id === state.listId) a.className = "on";
    a.innerHTML = `<span class="ic">▦</span><span class="lbl">${esc(l.name)}</span>` +
      `<span class="ct">${kfmt(l.count)}</span><span class="ldel" title="Delete list">✕</span>`;
    a.onclick = () => selectList(l.id, l.name, l.count);
    a.querySelector(".ldel").onclick = e => { e.stopPropagation(); deleteList(l.id, l.name); };
    nav.appendChild(a);
  });
}

async function splitByIndustry(){
  if(!confirm("Create a separate list for each industry? (Copies leads into new per-industry lists; the original stays.)")) return;
  const r = await api(`/api/lists/${state.listId}/split-by-industry`, { method: "POST" });
  alert(`Created ${r.created.length} industry lists.`);
  loadLists();
}

async function deleteSelected(){
  const ids = [...state.selectedLeads];
  if(!ids.length) return;
  if(!confirm(`Delete ${ids.length} lead${ids.length > 1 ? "s" : ""}? This can't be undone.`)) return;
  await api(`/api/lists/${state.listId}/leads?ids=${ids.join(",")}`, { method: "DELETE" });
  state.selectedLeads.clear();
  await refresh();
  loadLists();
}

async function clearResults(ids){
  const all = !ids || !ids.length;
  if(!confirm(all
      ? "Clear ALL enrichment results in this list? (Verification is kept.)"
      : `Clear enrichment for ${ids.length} selected lead${ids.length > 1 ? "s" : ""}?`)) return;
  await api(`/api/lists/${state.listId}/clear`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lead_ids: ids || [] }),
  });
  state.selectedLeads.clear();
  await refresh();
}

async function deleteList(id, name){
  if(!confirm(`Delete list "${name}" and all its leads? This can't be undone.`)) return;
  try{
    await api("/api/lists/" + id, { method: "DELETE" });
  }catch(e){
    alert("Couldn't delete the list: " + e.message);
    return;
  }
  if(state.listId === id){
    state.listId = null;
    $("viewTitle").textContent = "No list selected";
    $("viewSub").textContent = "";
    $("grid").hidden = true; $("empty").hidden = false; $("gridtools").hidden = true;
  }
  loadLists();
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
  try{ localStorage.setItem("lastList", JSON.stringify({ ws: state.variableSet, id })); }catch(e){}
  await loadLists();
  const d = await refresh();
  // resume: if a job is still running for this list, start showing live progress
  if(d && d.job && ["queued", "running", "cancelling"].includes(d.job.status)){
    startPolling(d.job.id);
  }
}

async function refresh(){
  if(!state.listId) return null;
  const d = await api(`/api/lists/${state.listId}`);
  renderGrid(d);
  renderBar(d);
  return d;
}

function startPolling(jobId){
  state.jobId = jobId; state.running = true; updateRunUI();
  if(state.poll) clearInterval(state.poll);
  state.poll = setInterval(async () => {
    let j;
    try{ j = await api("/api/jobs/" + jobId); }catch(e){ return; }
    await refresh();
    if(["done", "error", "cancelled"].includes(j.status)){
      clearInterval(state.poll); state.poll = null; state.running = false; updateRunUI();
      loadBalance(); loadLists();
    }
  }, 800);
}

function renderGrid(d){
  const leads = d.leads || [];
  $("empty").hidden = leads.length > 0;
  const grid = $("grid"); grid.hidden = leads.length === 0;

  // filter bar with counts
  const counts = { all: leads.length, enriched: 0, nonicp: 0, rejected: 0, notrun: 0, error: 0 };
  leads.forEach(l => { counts[leadCat(l)]++; });
  const gt = $("gridtools");
  gt.hidden = leads.length === 0;
  const chips = [["all", "All"], ["enriched", "Enriched"], ["nonicp", "Non-ICP"],
    ["rejected", "Title-rejected"], ["error", "No website"], ["notrun", "Not run"]];
  const chipHtml = chips.map(([k, label]) =>
    `<span class="fchip${state.filter === k ? " on" : ""}" data-f="${k}">${label} <b>${counts[k] || 0}</b></span>`).join("");
  const n = state.selectedLeads.size;
  const industries = [...new Set(leads.map(l => l.industry).filter(Boolean))].sort();
  let pre = "";
  if(industries.length){
    pre = `<select id="indFilter" class="indsel"><option value="">All industries</option>` +
      industries.map(i => `<option ${state.industryFilter === i ? "selected" : ""}>${esc(i)}</option>`).join("") +
      `</select><span class="gtact" data-act="split">Split by industry</span>`;
  }
  const acts = pre + (n > 0
    ? `<span class="gtact del" data-act="del">Delete ${n}</span><span class="gtact" data-act="clr">Clear ${n}</span><span class="gtact" data-act="exp">Export ${n}</span>`
    : `<span class="gtact" data-act="clrall">Clear results</span>`);
  gt.innerHTML = `<div class="fchips">${chipHtml}</div><div class="gtacts">${acts}</div>`;
  gt.querySelectorAll("[data-f]").forEach(c => c.onclick = () => { state.filter = c.dataset.f; renderGrid(d); });
  const indSel = gt.querySelector("#indFilter");
  if(indSel) indSel.onchange = () => { state.industryFilter = indSel.value; renderGrid(d); };
  const wire = (act, fn) => { const e = gt.querySelector(`[data-act="${act}"]`); if(e) e.onclick = fn; };
  wire("split", splitByIndustry);
  wire("del", deleteSelected);
  wire("clr", () => clearResults([...state.selectedLeads]));
  wire("exp", exportCsv);
  wire("clrall", () => clearResults([]));

  // apply filter; in "all", surface enriched rows to the top
  let view = state.filter === "all" ? leads.slice() : leads.filter(l => leadCat(l) === state.filter);
  if(state.industryFilter) view = view.filter(l => (l.industry || "") === state.industryFilter);
  if(state.filter === "all"){
    view.sort((a, b) => (leadCat(a) === "enriched" ? 0 : 1) - (leadCat(b) === "enriched" ? 0 : 1));
  }
  state.viewIds = view.map(l => l.id);

  const cols = ["Email · Reoon", "Title gate", "ICP", "Industry", ...state.selected.map(pretty)];
  $("head").innerHTML = `<th class="cbx"><input type="checkbox" id="selAll"></th><th>Lead</th>` +
    cols.map(c => `<th>${esc(c)}</th>`).join("") +
    `<th style="color:var(--acc-tx);cursor:pointer">+ enrichment</th>`;
  const body = $("body"); body.innerHTML = "";
  view.forEach(ld => {
    const r = ld.result || {};
    const tr = document.createElement("tr");
    const ck = state.selectedLeads.has(ld.id) ? "checked" : "";
    let cells = `<td class="cbx"><input type="checkbox" class="rowcb" data-id="${ld.id}" ${ck}></td>`;
    cells += `<td class="lead leadcell" data-id="${ld.id}"><b>${esc(ld.first_name)} ${esc(ld.last_name)}</b>` +
      `<s>${esc(ld.company)}${ld.title ? " · " + esc(ld.title) : ""}</s></td>`;
    cells += `<td>${emailCell(ld, r)}</td>`;
    cells += `<td>${titleCell(ld, r)}</td>`;
    cells += `<td>${icpCell(ld, r)}</td>`;
    cells += `<td>${ld.industry ? `<span class="pill p-gray">${esc(ld.industry)}</span>` : `<span class="sk">—</span>`}</td>`;
    state.selected.forEach(k => { cells += `<td>${varCell(ld, r, k)}</td>`; });
    cells += `<td></td>`;
    tr.innerHTML = cells;
    body.appendChild(tr);
  });

  const selAll = $("selAll");
  if(selAll){
    selAll.checked = view.length > 0 && view.every(l => state.selectedLeads.has(l.id));
    selAll.onchange = () => {
      if(selAll.checked) view.forEach(l => state.selectedLeads.add(l.id));
      else view.forEach(l => state.selectedLeads.delete(l.id));
      renderGrid(d); updateScope();
    };
  }
  body.querySelectorAll(".rowcb").forEach(cb => {
    cb.onchange = () => {
      const id = +cb.dataset.id;
      cb.checked ? state.selectedLeads.add(id) : state.selectedLeads.delete(id);
      const sa = $("selAll"); if(sa) sa.checked = view.every(l => state.selectedLeads.has(l.id));
      updateScope();
    };
  });
  const byId = {}; view.forEach(l => { byId[l.id] = l; });
  body.querySelectorAll(".leadcell").forEach(c => c.onclick = () => openDetail(byId[c.dataset.id]));
  updateScope();
}

function openDetail(ld){
  if(!ld) return;
  const r = ld.result || {};
  const keys = Object.keys(r).filter(k => !k.startsWith("_"));
  let h = `<div class="dtop"><div><div class="dname">${esc(ld.first_name)} ${esc(ld.last_name)}</div>` +
    `<div class="dsub">${esc(ld.company)}${ld.title ? " · " + esc(ld.title) : ""}</div></div>` +
    `<span class="dclose" id="dClose">✕</span></div>`;
  h += `<div class="drow"><span class="dk">Email</span> ${esc(ld.email || "—")}` +
    (ld.email_status ? ` <span class="reason">(${esc(ld.email_status)})</span>` : "") + `</div>`;
  h += `<div class="drow"><span class="dk">Industry</span> ${esc(ld.industry || "—")}</div>`;
  if(!keys.length){
    h += `<div class="sk" style="margin-top:12px">Not enriched yet.</div>`;
  } else {
    keys.forEach(k => {
      h += `<div class="dfield"><div class="dk">${esc(pretty(k))}</div><div class="dval">${esc(r[k] || "—")}</div></div>`;
    });
  }
  const d = $("detail"); d.innerHTML = h; d.hidden = false;
  $("dClose").onclick = () => { d.hidden = true; };
}

function updateScope(){
  const n = state.selectedLeads.size;
  const info = $("selInfo"), lim = $("limWrap");
  if(n > 0){ info.hidden = false; info.textContent = `${n} selected — Run uses these`; lim.style.opacity = ".4"; }
  else { info.hidden = true; lim.style.opacity = "1"; }
}

function updateRunUI(){
  const t = state.view === "table";
  const running = state.running;
  ["varsBtn", "classifyBtn", "runBtn", "verifyBtn", "pipelineBtn", "importBtn", "exportBtn"].forEach(id => {
    const e = $(id); if(e) e.style.display = (t && !running) ? "" : "none";
  });
  const sb = $("stopBtn"); if(sb) sb.style.display = (t && running) ? "" : "none";
  const sc = $("scope"); if(sc) sc.style.display = t ? "" : "none";
  const b2 = $("runBtn2"); if(b2) b2.disabled = running;
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
  if(r._status === "error") return `<span class="pill p-amber">no website</span>`;
  if(r._title_gate === "rejected") return `<span class="pill p-red">✕ Rejected</span>`;
  return `<span class="pill p-green">Pass</span>`;
}
function icpCell(ld, r){
  if(!hasResult(ld)) return `<span class="sk">queued</span>`;
  if(r._status === "error") return `<span class="sk">site unreachable</span>`;
  if(r._title_gate === "rejected") return `<span class="sk">skipped</span>`;
  if(r.ICPReview === "ICP") return `<span class="pill p-acc">ICP</span><span class="reason">${esc(r.ICP_reason||"")}</span>`;
  return `<span class="pill p-gray">Non-ICP</span><span class="reason">${esc(r.ICP_reason||"")}</span>`;
}
function varCell(ld, r, k){
  if(!hasResult(ld)) return `<span class="sk">queued</span>`;
  if(r._status === "error") return `<span class="sk">—</span>`;
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
  const live = ["queued", "running", "cancelling"].includes(j.status);
  const pre = live ? "● Running · " : (j.status === "cancelled" ? "Stopped · " : "");
  const s = j.summary || {};
  if(j.kind === "verify"){
    cost.textContent = `${j.cost || 0} cr`;
    const tail = (j.status === "done" || j.status === "cancelled")
      ? ` · ${s.valid||0} valid · ${s.risky||0} risky · ${s.invalid||0} invalid` : "";
    stat.textContent = `${pre}${j.done} of ${j.total} verified${tail}`;
  } else if(j.kind === "pipeline"){
    cost.textContent = `${s.cr||0} cr · $${(j.cost || 0).toFixed(2)}`;
    const tail = (j.status === "done" || j.status === "cancelled")
      ? ` · ${s.enriched||0} enriched · ${s.unsafe||0} unsafe · ${s.rejected||0} title-rejected` : "";
    stat.textContent = `${pre}${j.done} of ${j.total} processed${tail}`;
  } else if(j.kind === "classify"){
    cost.textContent = "$" + (j.cost || 0).toFixed(2);
    const tail = (j.status === "done" || j.status === "cancelled")
      ? ` · ${s.classified||0} classified · ${s.nosite||0} no website` : "";
    stat.textContent = `${pre}${j.done} of ${j.total} classified${tail}`;
  } else {
    cost.textContent = "$" + (j.cost || 0).toFixed(2);
    const tail = (j.status === "done" || j.status === "cancelled")
      ? ` · ${j.icp} ICP · ${j.nonicp} Non-ICP · ${j.rejected} title-rejected` : "";
    stat.textContent = `${pre}${j.done} of ${j.total} enriched${tail}`;
  }
}


async function startJob(kind){
  if(!state.listId || state.running) return;
  const d = await api(`/api/lists/${state.listId}`);
  const leads = d.leads || [];

  let candidates, scope;
  if(state.selectedLeads.size > 0){
    candidates = leads.filter(l => state.selectedLeads.has(l.id));
    scope = { lead_ids: [...state.selectedLeads] };
  } else {
    const lim = parseInt($("limitN").value, 10);
    if(lim > 0){ candidates = leads.slice(0, lim); scope = { limit: lim }; }
    else { candidates = leads; scope = {}; }
  }

  const onlySafe = $("onlySafe").checked;
  // resume-aware eligibility: skip leads already done
  let eligible;
  if(kind === "verify"){
    eligible = candidates.filter(l => !hasVerify(l));
  } else if(kind === "classify"){
    eligible = candidates.filter(l => !l.industry);
  } else if(kind === "pipeline"){
    eligible = candidates.filter(l => !hasVerify(l) || (isSafeLead(l) && !hasResult(l)));
  } else {
    eligible = candidates.filter(l => (!onlySafe || isSafeLead(l)) && !hasResult(l));
  }

  let skipDone = true;
  if(eligible.length === 0){
    if(kind === "verify"){ alert("All leads in scope are already verified."); return; }
    if(kind === "classify"){ alert("All leads in scope are already classified."); return; }
    if(!confirm("All leads in scope are already enriched. Re-run and overwrite their copy? (Use this to regenerate with the latest rules.)")) return;
    skipDone = false;
    eligible = kind === "pipeline" ? candidates : candidates.filter(l => !onlySafe || isSafeLead(l));
    if(eligible.length === 0){ alert("Nothing to run in this scope."); return; }
  }
  const count = eligible.length;

  const verb = kind === "verify" ? "verify" : (kind === "classify" ? "classify" : (kind === "pipeline" ? "verify + enrich" : "enrich"));
  const credit = kind === "verify" ? "Reoon" : (kind === "classify" ? "a little OpenAI" : (kind === "pipeline" ? "Reoon + OpenAI" : "OpenAI"));
  if(count > 50 && !confirm(`This will ${verb} ${count} leads and use ${credit} credit. Continue?`)) return;

  const ep = kind === "verify" ? "verify" : (kind === "classify" ? "classify" : (kind === "pipeline" ? "run-pipeline" : "run"));
  const body = Object.assign({ skip_done: skipDone }, scope);
  if(kind === "enrich" || kind === "pipeline") body.enrichments = state.selected;
  if(kind === "enrich") body.only_safe = onlySafe;

  const { job_id } = await api(`/api/lists/${state.listId}/${ep}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  startPolling(job_id);
}

const run = () => startJob("enrich");
const verify = () => startJob("verify");
const pipeline = () => startJob("pipeline");
const classify = () => startJob("classify");

async function stop(){
  if(!state.jobId) return;
  await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
}

function exportCsv(){
  if(!state.listId) return;
  let ids = "";
  if(state.selectedLeads.size > 0) ids = [...state.selectedLeads].join(",");
  else if(state.filter !== "all") ids = (state.viewIds || []).join(",");
  window.location = `/api/lists/${state.listId}/export` + (ids ? `?ids=${encodeURIComponent(ids)}` : "");
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
  if(name !== "table") $("gridtools").hidden = true;
  $("formatView").hidden = name !== "format";
  $("settingsView").hidden = name !== "settings";
  updateRunUI();
}

async function loadFormat(){
  showView("format");
  $("viewTitle").textContent = "Formats";
  $("viewSub").textContent = "Client profile & how each variable is written";
  const [sets, customs, fmt] = await Promise.all([
    api("/api/variable-sets"),
    api("/api/custom-variables?variable_set=" + state.variableSet),
    api("/api/format/" + state.variableSet),
  ]);
  let profile;
  if(fmt.workspace){
    profile = { name: fmt.client_name, fields: fmt.profile_fields || [], editable: true };
  } else {
    const client = state.variableSet.split("_")[0];
    const profiles = await api("/api/profiles");
    const profName = profiles.includes(client) ? client : profiles[0];
    profile = await api("/api/profiles/" + profName);
    profile.editable = false;
  }
  const idByName = {}; customs.forEach(c => idByName[c.name] = c.id);
  renderFormat(profile, fmt, sets, idByName);
}

function renderFormat(profile, fmt, sets, idByName){
  let h = `<div class="fv-sel"><label>Format set <select id="fSet">` +
    sets.map(s => `<option ${s === state.variableSet ? "selected" : ""}>${esc(s)}</option>`).join("") +
    `</select></label></div>`;
  h += `<div class="fv-h" style="display:flex;align-items:center">Client profile <span class="muted" style="margin-left:6px">— who we're writing for</span>` +
    (profile.editable ? `<span class="vacts"><span class="vact" id="wsDelete">delete workspace</span></span>` : "") + `</div><div class="card">`;
  if(profile.editable){
    profile.fields.forEach(f => {
      h += `<div class="kv"><div class="k">${esc(f.label)}</div><div class="v">` +
        `<textarea class="pfield" data-key="${esc(f.key)}" rows="3" placeholder="Describe ${esc(f.label.toLowerCase())}">${esc(f.value)}</textarea></div></div>`;
    });
    h += `<div class="brow" style="margin-top:10px"><button class="run" id="wsSaveProfile">Save profile</button><span class="savedmsg" id="wsSavedMsg"></span></div>`;
  } else if(profile.fields.length){
    profile.fields.forEach(f => { h += `<div class="kv"><div class="k">${esc(f.label)}</div><div class="v">${esc(f.value)}</div></div>`; });
  } else {
    h += `<div class="v sk">No profile fields.</div>`;
  }
  h += `</div>`;
  h += `<div class="fv-h" style="display:flex;align-items:center;gap:8px">Variables <span class="muted" style="margin-left:6px">— what we generate & how to write them</span>` +
    (profile.editable
      ? `<button class="gbtn" id="dlJsonBtn" style="margin-left:auto;padding:6px 11px">Download JSON</button><button class="gbtn" id="jsonBtn" style="padding:6px 11px">Paste JSON</button><button class="run" id="addVarBtn" style="padding:6px 11px">+ Add variable</button>`
      : `<button class="gbtn" id="dlJsonBtn" style="margin-left:auto;padding:6px 11px">Download JSON</button><button class="run" id="addVarBtn" style="padding:6px 11px">+ Add variable</button>`) +
    `</div>`;
  h += builderHtml();
  if(profile.editable) h += jsonPanelHtml();
  fmt.variables.forEach(v => {
    const cid = idByName[v.name];
    let acts = "";
    if(v.custom && cid){
      acts = `<span class="vact" data-edit="${cid}">edit</span>` +
             `<span class="vact" data-dup="${esc(v.name)}">duplicate</span>` +
             `<span class="delx" data-del="${cid}" title="Delete">✕</span>`;
    } else {
      acts = `<span class="vact" data-dup="${esc(v.name)}">duplicate</span>`;
      if(!v.always) acts += `<span class="vact" data-hide="${esc(v.name)}" data-on="${v.hidden ? 1 : 0}">${v.hidden ? "unhide" : "hide"}</span>`;
    }
    h += `<div class="card vcard${v.hidden ? " hidden-var" : ""}"><div class="vh"><span class="vname">${esc(v.label || pretty(v.name))}</span><span class="vslug">${esc(v.name)}</span>` +
      (v.min_words ? `<span class="wr">${v.min_words}-${v.max_words} words</span>` : "") +
      (v.always ? `<span class="tag">always runs</span>` : "") +
      (v.custom ? `<span class="tag" style="background:var(--acc-bg);color:var(--acc-tx)">custom</span>` : "") +
      (v.hidden ? `<span class="tag hid">hidden</span>` : "") +
      `<span class="vacts">${acts}</span></div>`;
    if(v.description) h += `<div class="vp">${esc(v.description)}</div>`;
    if(v.notes && v.notes.length) h += `<ul class="vn">` + v.notes.map(n => `<li>${esc(n)}</li>`).join("") + `</ul>`;
    h += `</div>`;
  });
  $("formatView").innerHTML = h;
  $("fSet").onchange = e => { state.variableSet = e.target.value; state.client = e.target.value.split("_")[0]; loadEnrichments(); loadFormat(); };
  if($("wsSaveProfile")) $("wsSaveProfile").onclick = saveWorkspaceProfile;
  if($("wsDelete")) $("wsDelete").onclick = deleteWorkspace;
  $("addVarBtn").onclick = () => { resetBuilder(); $("builder").hidden = false; };
  if($("dlJsonBtn")) $("dlJsonBtn").onclick = downloadJson;
  if($("jsonBtn")) $("jsonBtn").onclick = () => { const p = $("jsonPanel"); p.hidden = !p.hidden; };
  if($("jsonImport")) $("jsonImport").onclick = importJson;
  if($("jsonCancel")) $("jsonCancel").onclick = () => { $("jsonPanel").hidden = true; };
  $("cvTemplate").oninput = detectPlaceholders;
  $("cvSave").onclick = saveCustom;
  $("cvCancel").onclick = () => { $("builder").hidden = true; resetBuilder(); };
  $("formatView").querySelectorAll("[data-del]").forEach(x => x.onclick = () => deleteCustom(x.dataset.del));
  $("formatView").querySelectorAll("[data-dup]").forEach(x => x.onclick = () => duplicateVar(x.dataset.dup));
  $("formatView").querySelectorAll("[data-hide]").forEach(x => x.onclick = () => toggleHide(x.dataset.hide, x.dataset.on !== "1"));
  $("formatView").querySelectorAll("[data-edit]").forEach(x => x.onclick = () => editCustom(parseInt(x.dataset.edit, 10)));
}

function jsonPanelHtml(){
  const ph = '{\n  "profile": { "service_brief": "...", "main_offer": "...", "what_we_are_pitching": "...", "target_outcome": "...", "icp_summary": "..." },\n  "variables": [\n    { "label": "Value Proposition", "min_words": 45, "max_words": 80,\n      "guidance": "How to write it...", "template": "We specialize in {{x}} ...",\n      "examples": ["..."], "placeholders": [{ "token": "x", "description": "...", "examples": ["..."] }] }\n  ]\n}';
  return `<div class="card builder" id="jsonPanel" hidden>
    <div class="blabel">Paste a JSON config (client profile + variables) — e.g. one ChatGPT built for you</div>
    <textarea id="jsonText" rows="11" placeholder='${ph.replace(/'/g, "&#39;")}'></textarea>
    <div class="brow"><button class="run" id="jsonImport">Import JSON</button><button class="gbtn" id="jsonCancel">Cancel</button><span class="savedmsg" id="jsonMsg"></span></div>
  </div>`;
}

async function downloadJson(){
  const data = await api("/api/format-json/" + state.variableSet);
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = state.variableSet + "_config.json";
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(a.href);
}

async function importJson(){
  let data;
  try{ data = JSON.parse($("jsonText").value); }
  catch(e){ const m = $("jsonMsg"); m.textContent = "Invalid JSON — check the format."; m.style.color = "var(--red-tx)"; return; }
  const body = { profile: data.profile || {}, variables: data.variables || [] };
  try{
    const r = await api(`/api/workspaces/${state.variableSet}/import`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const m = $("jsonMsg"); m.style.color = "var(--green-tx)";
    m.textContent = `✓ Imported ${r.variables_imported} variable(s)`;
    await loadEnrichments();
    setTimeout(loadFormat, 700);
  }catch(e){ const m = $("jsonMsg"); m.textContent = "Import failed: " + e.message; m.style.color = "var(--red-tx)"; }
}

function builderHtml(){
  return `<div class="card builder" id="builder" hidden>
    <input id="cvName" placeholder="Variable name   e.g. Personalization" />
    <div class="blabel">How to write it — rules & guidance</div>
    <textarea id="cvGuidance" rows="3" placeholder="Explain in plain words how this should be written. e.g. One sentence opening on a specific, real detail from the prospect's website. No pitch. No greeting. Mention something only someone who read their site would know."></textarea>
    <div class="blabel">Format <span class="sk">(optional — leave blank for free-form variables like personalization)</span></div>
    <textarea id="cvTemplate" rows="2" placeholder="Optional. Use {{placeholders}} for fill-in-the-blank parts.\ne.g. We help {{industry}} get {{ideal customers}} by {{what we do}}."></textarea>
    <div class="brow">Whole-variable word range <input id="cvMin" type="number" min="1" placeholder="min" /> to <input id="cvMax" type="number" min="1" placeholder="max" /></div>
    <div class="phh">Placeholders</div>
    <div id="cvPlaceholders"><div class="sk">Add {{placeholders}} in the format above to describe them here.</div></div>
    <div class="blabel">Examples <span class="sk">(one per line — sample outputs that show the AI what good looks like)</span></div>
    <textarea id="cvExamples" rows="3" placeholder="Your work for Acme Dental shows a clear focus on local clinics.\nThe way you bundle SEO with paid search is a sharp combo for B2B teams."></textarea>
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

function resetBuilder(){
  state.editId = null;
  ["cvName", "cvGuidance", "cvTemplate", "cvMin", "cvMax", "cvExamples"].forEach(id => { if($(id)) $(id).value = ""; });
  if($("cvPlaceholders")) detectPlaceholders();
  if($("cvSave")) $("cvSave").textContent = "Save variable";
}

async function saveCustom(){
  const label = $("cvName").value.trim();
  if(!label){ alert("Give the variable a name first."); return; }
  const guidance = $("cvGuidance").value.trim();
  const template = $("cvTemplate").value;
  if(!guidance && !template.trim()){ alert("Add either guidance (how to write it) or a {{format}}."); return; }
  const placeholders = [...$("cvPlaceholders").querySelectorAll("[data-tok]")].map(el => ({
    token: el.dataset.tok,
    description: el.querySelector(".pdesc").value.trim(),
    min_words: parseInt(el.querySelector(".pmin").value, 10) || null,
    max_words: parseInt(el.querySelector(".pmax").value, 10) || null,
    examples: el.querySelector(".pex").value.split("\n").map(s => s.trim()).filter(Boolean),
  }));
  const examples = $("cvExamples").value.split("\n").map(s => s.trim()).filter(Boolean);
  const body = {
    variable_set: state.variableSet, label, template, purpose: guidance, examples,
    min_words: parseInt($("cvMin").value, 10) || null, max_words: parseInt($("cvMax").value, 10) || null,
    placeholders, id: state.editId,
  };
  await api("/api/custom-variables", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  resetBuilder();
  await loadEnrichments();
  loadFormat();
}

async function editCustom(id){
  const customs = await api("/api/custom-variables?variable_set=" + state.variableSet);
  const row = customs.find(c => c.id === id);
  if(!row) return;
  const spec = row.spec || {};
  $("builder").hidden = false;
  state.editId = id;
  $("cvName").value = spec.label || row.label || "";
  $("cvGuidance").value = spec.purpose || "";
  $("cvTemplate").value = spec.template || "";
  $("cvMin").value = spec.min_words || "";
  $("cvMax").value = spec.max_words || "";
  $("cvExamples").value = (spec.example_outputs || []).join("\n");
  detectPlaceholders();
  const ph = spec.placeholders || {};
  $("cvPlaceholders").querySelectorAll("[data-tok]").forEach(el => {
    const p = ph[el.dataset.tok] || {};
    el.querySelector(".pdesc").value = p.description || "";
    el.querySelector(".pmin").value = p.min_words || "";
    el.querySelector(".pmax").value = p.max_words || "";
    el.querySelector(".pex").value = (p.examples || []).join("\n");
  });
  $("cvSave").textContent = "Update variable";
  $("builder").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function duplicateVar(name){
  await api("/api/custom-variables/duplicate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ variable_set: state.variableSet, name }),
  });
  await loadEnrichments();
  loadFormat();
}

async function toggleHide(name, hidden){
  await api("/api/hidden", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ variable_set: state.variableSet, name, hidden }),
  });
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
  let st = {}; try{ st = await api("/api/status"); }catch(e){}
  const reoon = !b.enabled ? "Demo mode (no REOON_API_KEY set)"
    : (b.error ? "Key set, but balance check failed" : `Connected · ${b.instant ?? 0} instant credits`);
  const storage = st.db ? `${st.db}${st.persistent ? " · persists across deploys" : " · resets on each deploy ⚠️"}` : "unknown";
  const def = localStorage.getItem("defLimit") || "10";
  $("settingsView").innerHTML =
    `<div class="fv-h">Settings</div><div class="card">` +
    `<div class="kv"><div class="k">Storage</div><div class="v">${esc(storage)}</div></div>` +
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
  const wss = await api("/api/workspaces");
  menu.innerHTML = wss.map(w =>
    `<a data-key="${esc(w.key)}" data-name="${esc(w.name)}">${esc(w.name)}` +
    (w.kind === "workspace" ? ` <span style="color:var(--hint);font-size:10px">workspace</span>` : "") + `</a>`
  ).join("") + `<a id="wsNewItem" style="color:var(--acc-tx)">+ New workspace</a>`;
  menu.querySelectorAll("a[data-key]").forEach(a => a.onclick = () => { setWorkspace(a.dataset.key, a.dataset.name); menu.hidden = true; });
  $("wsNewItem").onclick = () => { menu.hidden = true; openNewWorkspace(); };
  menu.hidden = false;
}

async function setWorkspace(key, name){
  state.variableSet = key;
  state.client = name;
  $("wsName").textContent = name;
  $("wsDot").textContent = (name[0] || "A").toUpperCase();
  try{ localStorage.setItem("ws", JSON.stringify({ key, name })); }catch(e){}
  // lists are scoped per workspace — clear current selection and reload them
  state.listId = null; state.selectedLeads.clear();
  if(state.poll){ clearInterval(state.poll); state.poll = null; }
  state.running = false;
  await loadEnrichments();
  await loadLists();
  if(state.view === "table"){
    $("viewTitle").textContent = "No list selected";
    $("viewSub").textContent = "Pick a list, or import one";
    $("grid").hidden = true; $("empty").hidden = false; $("gridtools").hidden = true;
    renderBar({ list: { count: 0 } });
    updateRunUI();
  } else if(state.view === "format") loadFormat();
  else if(state.view === "settings") loadSettings();
}

async function restoreWorkspace(){
  let saved = null;
  try{ saved = JSON.parse(localStorage.getItem("ws") || "null"); }catch(e){}
  if(!saved || !saved.key) return;
  try{
    const wss = await api("/api/workspaces");
    const match = wss.find(w => w.key === saved.key);
    if(match){
      state.variableSet = match.key;
      state.client = match.name;
      $("wsName").textContent = match.name;
      $("wsDot").textContent = (match.name[0] || "A").toUpperCase();
    }
  }catch(e){}
}

async function openNewWorkspace(){
  showView("format");
  $("viewTitle").textContent = "New workspace";
  $("viewSub").textContent = "Create a client workspace";
  const engineSets = await api("/api/engine-sets");
  const ph = '{\n  "profile": { "service_brief": "...", "main_offer": "...", "what_we_are_pitching": "...", "target_outcome": "...", "icp_summary": "..." },\n  "variables": [\n    { "label": "Value Proposition", "min_words": 45, "max_words": 80, "guidance": "How to write it...",\n      "template": "We help {{industry}} ...", "examples": ["..."], "placeholders": [{ "token": "industry", "examples": ["agencies"] }] }\n  ]\n}';
  $("formatView").innerHTML =
    `<div class="fv-h">New workspace</div><div class="card builder">` +
    `<input id="wsNewName" placeholder="Client / workspace name   e.g. Acme Co" />` +
    `<div class="brow">Start from <select id="wsNewBase">` +
    `<option value="">Blank (build variables yourself)</option>` +
    engineSets.map(s => `<option value="${esc(s)}">Clone: ${esc(s)}</option>`).join("") +
    `</select></div>` +
    `<div class="blabel">Or paste a JSON config <span class="sk">(optional — sets the client profile + all variables at once, e.g. one ChatGPT built)</span></div>` +
    `<textarea id="wsNewJson" rows="9" placeholder='${ph.replace(/'/g, "&#39;")}'></textarea>` +
    `<div class="brow"><button class="run" id="wsCreate">Create workspace</button>` +
    `<button class="gbtn" id="wsCancelNew">Cancel</button><span class="savedmsg" id="wsNewMsg"></span></div></div>`;
  $("wsCreate").onclick = createWorkspace;
  $("wsCancelNew").onclick = () => loadFormat();
}

async function createWorkspace(){
  const name = $("wsNewName").value.trim();
  if(!name){ alert("Name the workspace first."); return; }
  const jsonText = ($("wsNewJson").value || "").trim();
  let jsonData = null;
  if(jsonText){
    try{ jsonData = JSON.parse(jsonText); }
    catch(e){ const m = $("wsNewMsg"); m.textContent = "Invalid JSON — check the format."; m.style.color = "var(--red-tx)"; return; }
  }
  // if JSON is pasted it defines everything, so create blank then import; otherwise use the chosen base
  const base = jsonData ? "" : $("wsNewBase").value;
  const r = await api("/api/workspaces", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, base_set: base }),
  });
  if(jsonData){
    await api(`/api/workspaces/${r.key}/import`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: jsonData.profile || {}, variables: jsonData.variables || [] }),
    });
  }
  await setWorkspace(r.key, r.name);
  loadFormat();
}

async function saveWorkspaceProfile(){
  const profile = {};
  $("formatView").querySelectorAll(".pfield").forEach(t => { profile[t.dataset.key] = t.value; });
  const btn = $("wsSaveProfile"), msg = $("wsSavedMsg");
  btn.textContent = "Saving…";
  try{
    await api("/api/workspaces/" + state.variableSet, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ profile }),
    });
    const t = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if(msg) msg.textContent = `✓ Saved at ${t}`;
  }catch(e){
    if(msg){ msg.textContent = "Save failed — try again"; msg.style.color = "var(--red-tx)"; }
  }
  btn.textContent = "Save profile";
}

async function deleteWorkspace(){
  if(!confirm("Delete this workspace? Its custom variables go too.")) return;
  await api("/api/workspaces/" + state.variableSet, { method: "DELETE" });
  await setWorkspace("ascendly_lean", "Ascendly");
  loadFormat();
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
  $("enrichBtn").onclick = $("varsBtn").onclick = () => { showView("table"); $("enrichPanel").hidden = !$("enrichPanel").hidden; $("importPanel").hidden = true; };
  $("formatBtn").onclick = loadFormat;
  $("settingsBtn").onclick = loadSettings;
  $("wsBtn").onclick = e => { if(e.target.closest("#collapseBtn")) return; toggleWsMenu(); };
  $("runBtn").onclick = $("runBtn2").onclick = run;
  $("verifyBtn").onclick = verify;
  $("classifyBtn").onclick = classify;
  $("pipelineBtn").onclick = pipeline;
  $("exportBtn").onclick = $("exportNav").onclick = exportCsv;
  $("stopBtn").onclick = stop;
  $("limitN").oninput = updateScope;
  const savedLimit = localStorage.getItem("defLimit"); if(savedLimit) $("limitN").value = savedLimit;
  showView("table");
  loadBalance();
  restoreWorkspace().then(() => loadEnrichments()).then(() => loadLists()).then(restoreLastList);
}

async function restoreLastList(){
  let saved = null;
  try{ saved = JSON.parse(localStorage.getItem("lastList") || "null"); }catch(e){}
  if(!saved || saved.ws !== state.variableSet) return;
  try{
    const lists = await api("/api/lists?variable_set=" + encodeURIComponent(state.variableSet));
    const m = lists.find(l => l.id === saved.id);
    if(m) selectList(m.id, m.name, m.count);
  }catch(e){}
}

init();
