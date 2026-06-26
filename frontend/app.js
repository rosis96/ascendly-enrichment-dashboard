const API = "";
const ICONS = { collapse: "«", up: "↑", down: "↓", mail: "✉", play: "▶", file: "⊞", x: "✕", stop: "■", cols: "▦", tag: "▤", check: "✓", at: "@" };
const VBUCKET = { valid: "p-green", risky: "p-amber", invalid: "p-red" };
const ROW_CAP_STEP = 250;   // how many rows to render at once (windowing for smoothness)
const state = { listId: null, variableSet: "ascendly_lean", selectable: [], selected: [],
  poll: null, selectedLeads: new Set(), running: false, jobId: null,
  view: "table", client: "ascendly", labels: {}, editId: null, filter: "all", industryFilter: "",
  rowCap: ROW_CAP_STEP, lastCount: 0, tick: 0 };

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
  if(r.status === 401){ window.location = "/login"; throw new Error("auth required"); }
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

function deleteListPopup(name){
  return new Promise(resolve => {
    const m = $("mapModal");
    m.innerHTML = `<div class="modal-box" style="width:460px">` +
      `<div class="modal-h">Delete "${esc(name)}"?<i class="modal-x" id="delX">✕</i></div>` +
      `<div class="modal-sub">What should happen to this list's leads?</div>` +
      `<div class="delacts">` +
      `<button class="gbtn" id="delKeep">Keep the leads — move them to "Saved leads" and remove only this list</button>` +
      `<button class="run stop" id="delBoth">Delete the list AND its leads from the database</button>` +
      `<span class="gtact" id="delCancel">Cancel</span></div></div>`;
    m.hidden = false;
    const done = v => { m.hidden = true; resolve(v); };
    $("delX").onclick = $("delCancel").onclick = () => done("cancel");
    $("delKeep").onclick = () => done("keep");
    $("delBoth").onclick = () => done("both");
  });
}

async function deleteList(id, name){
  const choice = await deleteListPopup(name);
  if(choice === "cancel") return;
  try{
    await api("/api/lists/" + id + (choice === "keep" ? "?keep=1" : ""), { method: "DELETE" });
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
  state.rowCap = ROW_CAP_STEP;
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
  state.lastCount = d.list ? d.list.count : state.lastCount;
  renderGrid(d);
  renderBar(d);
  return d;
}

function startPolling(jobId){
  state.jobId = jobId; state.running = true; state.tick = 0; updateRunUI();
  if(state.poll) clearInterval(state.poll);
  state.poll = setInterval(async () => {
    let j;
    try{ j = await api("/api/jobs/" + jobId); }catch(e){ return; }
    // cheap: update only the progress bar every tick (no grid rebuild)
    renderBar({ job: j, list: { count: state.lastCount || j.total || 0 } });
    state.tick++;
    const finished = ["done", "error", "cancelled"].includes(j.status);
    // full grid refresh only every ~6s, and once when the run finishes
    if(finished || state.tick % 6 === 0) await refresh();
    if(finished){
      clearInterval(state.poll); state.poll = null; state.running = false; updateRunUI();
      loadBalance(); loadLists();
    }
  }, 1000);
}

function renderGrid(d){
  const leads = d.leads || [];
  $("empty").hidden = leads.length > 0;
  const grid = $("grid"); grid.hidden = leads.length === 0;

  // filter bar with counts
  const counts = { all: leads.length, enriched: 0, nonicp: 0, rejected: 0, notrun: 0, error: 0, tpass: 0, trej: 0,
    espMicrosoft: 0, espGoogle: 0, espOther: 0, espUnknown: 0 };
  leads.forEach(l => {
    counts[leadCat(l)]++;
    if(l.title_status === "pass") counts.tpass++;
    else if(l.title_status === "rejected") counts.trej++;
    if(l.esp) counts["esp" + l.esp] = (counts["esp" + l.esp] || 0) + 1;
  });
  const gt = $("gridtools");
  gt.hidden = leads.length === 0;
  const chips = [["all", "All"], ["enriched", "Enriched"], ["nonicp", "Non-ICP"],
    ["rejected", "Title-rejected"], ["error", "No website"], ["notrun", "Not run"]];
  // show title-check chips only once a title check has been run
  if(counts.tpass || counts.trej){
    chips.push(["tpass", "Title ✓"], ["trej", "Title ✗"]);
  }
  // ESP chips, present buckets only
  [["espMicrosoft", "Microsoft"], ["espGoogle", "Google"], ["espOther", "ESP other"], ["espUnknown", "ESP ?"]]
    .forEach(([k, label]) => { if(counts[k]) chips.push([k, label]); });
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
  const notrunIds = leads.filter(l => leadCat(l) === "notrun").map(l => l.id);
  const selNr = notrunIds.length
    ? `<span class="gtact" data-act="selnr">Select not-run ${notrunIds.length}</span>` : "";
  const tpassIds = leads.filter(l => l.title_status === "pass").map(l => l.id);
  const selTp = tpassIds.length
    ? `<span class="gtact" data-act="seltp">Select title ✓ ${tpassIds.length}</span>` : "";
  const acts = pre + selTp + selNr + (n > 0
    ? `<span class="gtact del" data-act="del">Delete ${n}</span><span class="gtact" data-act="clr">Clear ${n}</span><span class="gtact" data-act="exp">Export ${n}</span>`
    : `<span class="gtact" data-act="clrall">Clear results</span>`);
  gt.innerHTML = `<div class="fchips">${chipHtml}</div><div class="gtacts">${acts}</div>`;
  gt.querySelectorAll("[data-f]").forEach(c => c.onclick = () => { state.filter = c.dataset.f; state.rowCap = ROW_CAP_STEP; renderGrid(d); });
  const indSel = gt.querySelector("#indFilter");
  if(indSel) indSel.onchange = () => { state.industryFilter = indSel.value; state.rowCap = ROW_CAP_STEP; renderGrid(d); };
  const wire = (act, fn) => { const e = gt.querySelector(`[data-act="${act}"]`); if(e) e.onclick = fn; };
  wire("selnr", () => {
    notrunIds.forEach(id => state.selectedLeads.add(id));
    state.filter = "notrun";
    renderGrid(d); updateScope();
  });
  wire("seltp", () => {
    tpassIds.forEach(id => state.selectedLeads.add(id));
    state.filter = "tpass"; state.rowCap = ROW_CAP_STEP;
    renderGrid(d); updateScope();
  });
  wire("split", splitByIndustry);
  wire("del", deleteSelected);
  wire("clr", () => clearResults([...state.selectedLeads]));
  wire("exp", exportCsv);
  wire("clrall", () => clearResults([]));

  // apply filter; in "all", surface enriched rows to the top
  let view;
  if(state.filter === "all") view = leads.slice();
  else if(state.filter === "tpass") view = leads.filter(l => l.title_status === "pass");
  else if(state.filter === "trej") view = leads.filter(l => l.title_status === "rejected");
  else if(state.filter.startsWith("esp")) view = leads.filter(l => ("esp" + (l.esp || "")) === state.filter);
  else view = leads.filter(l => leadCat(l) === state.filter);
  if(state.industryFilter) view = view.filter(l => (l.industry || "") === state.industryFilter);
  if(state.filter === "all"){
    view.sort((a, b) => (leadCat(a) === "enriched" ? 0 : 1) - (leadCat(b) === "enriched" ? 0 : 1));
  }
  state.viewIds = view.map(l => l.id);

  const cols = ["Email · Reoon", "Title gate", "ICP", "Industry", "ESP", ...state.selected.map(pretty)];
  $("head").innerHTML = `<th class="cbx"><input type="checkbox" id="selAll"></th><th>Lead</th>` +
    cols.map(c => `<th>${esc(c)}</th>`).join("") +
    `<th style="color:var(--acc-tx);cursor:pointer">+ enrichment</th>`;
  // Windowing: only render the first rowCap rows so a 3000-row list doesn't all
  // live in the DOM (the main source of scroll/interaction lag). A "Show more"
  // row reveals the next batch. Selection/export still operate on the full view.
  const ncol = cols.length + 3;   // checkbox + lead + cols + "+ enrichment"
  const shown = view.slice(0, state.rowCap);
  const body = $("body"); body.innerHTML = "";
  const frag = document.createDocumentFragment();
  shown.forEach(ld => {
    const r = ld.result || {};
    const tr = document.createElement("tr");
    const ck = state.selectedLeads.has(ld.id) ? "checked" : "";
    let cells = `<td class="cbx"><input type="checkbox" class="rowcb" data-id="${ld.id}" ${ck}></td>`;
    const ini = (((ld.first_name || "")[0] || "") + ((ld.last_name || "")[0] || "")).toUpperCase()
      || ((ld.company || "?")[0] || "?").toUpperCase();
    cells += `<td class="lead leadcell" data-id="${ld.id}"><div class="leadrow"><span class="avatar">${esc(ini)}</span>` +
      `<div class="leadtext"><b>${esc(ld.first_name)} ${esc(ld.last_name)}</b>` +
      `<s>${esc(ld.company)}${ld.title ? " · " + esc(ld.title) : ""}</s></div></div></td>`;
    cells += `<td>${emailCell(ld, r)}</td>`;
    cells += `<td>${titleCell(ld, r)}</td>`;
    cells += `<td>${icpCell(ld, r)}</td>`;
    cells += `<td>${ld.industry ? `<span class="pill p-gray">${esc(ld.industry)}</span>` : `<span class="sk">—</span>`}</td>`;
    cells += `<td>${espCell(ld)}</td>`;
    state.selected.forEach(k => { cells += `<td>${varCell(ld, r, k)}</td>`; });
    cells += `<td></td>`;
    tr.innerHTML = cells;
    frag.appendChild(tr);
  });
  if(view.length > shown.length){
    const tr = document.createElement("tr");
    tr.className = "morerow";
    tr.innerHTML = `<td colspan="${ncol}">Showing ${shown.length} of ${view.length} · ` +
      `<a class="gtact" id="showMore">Show ${Math.min(ROW_CAP_STEP, view.length - shown.length)} more</a>` +
      ` · <a class="gtact" id="showAll">Show all</a></td>`;
    frag.appendChild(tr);
  }
  body.appendChild(frag);
  const sm = $("showMore"); if(sm) sm.onclick = () => { state.rowCap += ROW_CAP_STEP; renderGrid(d); };
  const sa = $("showAll"); if(sa) sa.onclick = () => { state.rowCap = view.length; renderGrid(d); };

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
  ["varsBtn", "classifyBtn", "runBtn", "verifyBtn", "pipelineBtn", "importBtn", "exportBtn", "titleBtn", "espBtn"].forEach(id => {
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
  if(!hasResult(ld)){
    // standalone title-check result (before any enrichment)
    if(ld.title_status === "pass") return `<span class="pill p-green">✓ Pass</span>`;
    if(ld.title_status === "rejected") return `<span class="pill p-red">✕ Rejected</span>`;
    return `<span class="sk">queued</span>`;
  }
  if(r._status === "error") return `<span class="pill p-amber">no website</span>`;
  if(r._title_gate === "rejected") return `<span class="pill p-red">✕ Rejected</span>`;
  return `<span class="pill p-green">Pass</span>`;
}
function espCell(ld){
  const e = ld.esp;
  if(!e) return `<span class="sk">—</span>`;
  if(e === "Microsoft") return `<span class="pill p-acc">Microsoft</span>`;
  if(e === "Google") return `<span class="pill p-green">Google</span>`;
  if(e === "Other") return `<span class="pill p-gray">Other</span>`;
  return `<span class="sk">unknown</span>`;
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
  } else if(j.kind === "titlecheck"){
    cost.textContent = "free";
    const tail = (j.status === "done" || j.status === "cancelled")
      ? ` · ${s.tpass||0} pass · ${s.trej||0} rejected` : "";
    stat.textContent = `${pre}${j.done} of ${j.total} title-checked${tail}`;
  } else if(j.kind === "esp"){
    cost.textContent = "free";
    const tail = (j.status === "done" || j.status === "cancelled")
      ? ` · ${s.microsoft||0} Microsoft · ${s.google||0} Google · ${s.other||0} other · ${s.unknown||0} unknown` : "";
    stat.textContent = `${pre}${j.done} of ${j.total} ESP-checked${tail}`;
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
  } else if(kind === "titlecheck"){
    eligible = candidates.filter(l => !l.title_status);
  } else if(kind === "esp"){
    eligible = candidates.filter(l => !l.esp);
  } else if(kind === "pipeline"){
    eligible = candidates.filter(l => !hasVerify(l) || (isSafeLead(l) && !hasResult(l)));
  } else {
    eligible = candidates.filter(l => (!onlySafe || isSafeLead(l)) && !hasResult(l));
  }

  let skipDone = true;
  if(eligible.length === 0){
    if(kind === "verify"){ alert("All leads in scope are already verified."); return; }
    if(kind === "classify"){ alert("All leads in scope are already classified."); return; }
    if(kind === "titlecheck"){
      if(!confirm("All leads in scope are already title-checked. Re-check them?")) return;
      skipDone = false; eligible = candidates;
    } else if(kind === "esp"){
      if(!confirm("All leads in scope already have an ESP result. Re-check them?")) return;
      skipDone = false; eligible = candidates;
    } else {
      if(!confirm("All leads in scope are already enriched. Re-run and overwrite their copy? (Use this to regenerate with the latest rules.)")) return;
      skipDone = false;
      eligible = kind === "pipeline" ? candidates : candidates.filter(l => !onlySafe || isSafeLead(l));
    }
    if(eligible.length === 0){ alert("Nothing to run in this scope."); return; }
  }
  const count = eligible.length;

  const verb = kind === "verify" ? "verify" : (kind === "classify" ? "classify" : (kind === "titlecheck" ? "title-check" : (kind === "esp" ? "ESP-check" : (kind === "pipeline" ? "verify + enrich" : "enrich"))));
  // title check and ESP are free, so no credit confirmation
  if(kind !== "titlecheck" && kind !== "esp"){
    const credit = kind === "verify" ? "Reoon" : (kind === "classify" ? "a little OpenAI" : (kind === "pipeline" ? "Reoon + OpenAI" : "OpenAI"));
    if(count > 50 && !confirm(`This will ${verb} ${count} leads and use ${credit} credit. Continue?`)) return;
  }

  const ep = kind === "verify" ? "verify" : (kind === "classify" ? "classify" : (kind === "titlecheck" ? "title-check" : (kind === "esp" ? "esp-check" : (kind === "pipeline" ? "run-pipeline" : "run"))));
  const body = Object.assign({ skip_done: skipDone }, scope);
  if(kind === "enrich" || kind === "pipeline") body.enrichments = state.selected;
  if(kind === "enrich") body.only_safe = onlySafe;
  const w = parseInt(($("workersN") || {}).value, 10);
  if(w > 0) body.workers = w;

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
const titleCheck = () => startJob("titlecheck");
const espCheck = () => startJob("esp");

async function stop(){
  if(!state.jobId) return;
  await api(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
}

async function exportCsv(){
  if(!state.listId) return;
  // Send ids in the POST body (not the URL) so big selections can't hit HTTP 431.
  let ids = [];
  if(state.selectedLeads.size > 0) ids = [...state.selectedLeads];
  else if(state.filter !== "all") ids = (state.viewIds || []);
  try{
    const r = await fetch(`/api/lists/${state.listId}/export`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    if(r.status === 401){ window.location = "/login"; return; }
    if(!r.ok){ alert("Export failed. Try again."); return; }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const fname = m ? m[1] : "export.csv";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = fname;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }catch(e){ alert("Export failed. Try again."); }
}

async function loadBalance(){
  try{
    const b = await api("/api/reoon/balance");
    const el = $("credits");
    if(!b.enabled) el.textContent = "Reoon: demo";
    else if(b.error) el.textContent = "Reoon: key error";
    else el.textContent = `Reoon: ${b.daily ?? 0} free + ${b.instant ?? 0} paid`;
  }catch(e){}
}

// ---------------- Database (Apollo-style) view ----------------
function dbState(){
  if(!state.db) state.db = { filters: {}, page: 1, pageSize: 50, selected: new Set(), selectAll: false, tax: null, data: null };
  return state.db;
}

async function loadDatabase(){
  showView("database");
  $("viewTitle").textContent = "Database";
  $("viewSub").textContent = `All leads in ${state.variableSet} — filter, then send to a workspace`;
  const db = dbState();
  if(!db.tax){ try{ db.tax = await api("/api/taxonomy"); }catch(e){ db.tax = []; } }
  await fetchDatabase();
  // reconnect to a database-wide job that's still running on the server
  try{
    const a = await api(`/api/workspaces/${encodeURIComponent(state.variableSet)}/active-job`);
    if(a.job) pollDbJob(a.job.id, a.job.kind);
  }catch(e){}
}

async function fetchDatabase(){
  const db = dbState();
  const body = Object.assign({}, db.filters, { page: db.page, page_size: db.pageSize });
  try{
    db.data = await api(`/api/workspaces/${encodeURIComponent(state.variableSet)}/database`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  }catch(e){ $("databaseView").innerHTML = `<div class="empty">Couldn't load the database.</div>`; return; }
  renderDatabase();
}

function renderDatabase(){
  const db = dbState(), d = db.data, f = db.filters;
  if(!d) return;
  const opt = (cur, pairs) => pairs.map(([v, lbl]) => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(lbl)}</option>`).join("");
  const indPairs = [["", "All industries"], ["__unclassified__", "— Unclassified —"]].concat((db.tax || []).map(x => [x, x]));
  const espPairs = [["", "Any ESP"], ["Microsoft", "Microsoft"], ["Google", "Google"], ["Other", "Other"], ["Unknown", "Unknown"]];
  const titlePairs = [["", "Any title"], ["pass", "Title ✓"], ["rejected", "Title ✗"]];
  let h = `<div class="dbjobs"><span class="dbjobs-l">Run on the whole database (skips already-done):</span>` +
    `<button class="gbtn" id="dbTitleAll">✓ Title check</button>` +
    `<button class="gbtn" id="dbEspAll">@ ESP</button>` +
    `<button class="gbtn" id="dbClassifyAll">▤ Classify</button>` +
    `<span class="muted" id="dbJobMsg"></span></div>` +
    `<div class="dbfilters">
    <select id="dbInd">${opt(f.industry || "", indPairs)}</select>
    <select id="dbEsp">${opt(f.esp || "", espPairs)}</select>
    <select id="dbTitle">${opt(f.title_status || "", titlePairs)}</select>
    <input id="dbEmpMin" type="number" min="0" placeholder="min emp" value="${f.employees_min ?? ""}" />
    <input id="dbEmpMax" type="number" min="0" placeholder="max emp" value="${f.employees_max ?? ""}" />
    <input id="dbCountry" placeholder="Country" value="${esc(f.country || "")}" />
    <input id="dbSeniority" placeholder="Seniority" value="${esc(f.seniority || "")}" />
    <input id="dbSearch" placeholder="Search name / company / email" value="${esc(f.q || "")}" />
    <button class="run" id="dbApply">Apply</button>
    <span class="gtact" id="dbClear">Clear</span>
  </div>`;
  h += `<div class="dbbar">
    <span class="dbcount"><b>${d.total.toLocaleString()}</b> match${d.total === 1 ? "" : "es"} · ${d.grand_total.toLocaleString()} total in workspace</span>
    <span class="selinfo" id="dbSelInfo"></span>
    <span class="gtact" id="dbExport">Export ${d.total.toLocaleString()}</span>
    <span class="gtact" id="dbSendBtn">Send to workspace →</span>
  </div>`;
  h += `<div class="dbselnotice" id="dbSelNotice" hidden></div>`;
  h += `<div class="dbsend" id="dbSendPanel" hidden></div>`;
  const fixedCols = ["Name", "Title", "Company", "Email", "Industry", "ESP", "Employees", "Country", "Seniority", "Email status"];
  const dataCols = d.data_columns || [];
  const pageAllSel = (d.leads || []).length > 0 && (d.leads || []).every(l => db.selectAll || db.selected.has(l.id));
  h += `<div class="dbtablewrap"><table class="dbtable"><thead><tr><th class="cbx"><input type="checkbox" id="dbSelAll" ${pageAllSel ? "checked" : ""}></th>` +
    fixedCols.map(c => `<th>${c}</th>`).join("") +
    dataCols.map(c => `<th>${esc(c)}</th>`).join("") + `</tr></thead><tbody>`;
  (d.leads || []).forEach(l => {
    const ck = (db.selectAll || db.selected.has(l.id)) ? "checked" : "";
    const data = l.data || {};
    h += `<tr><td class="cbx"><input type="checkbox" class="dbcb" data-id="${l.id}" ${ck}></td>` +
      `<td><b class="dblink" data-id="${l.id}">${esc(l.first_name)} ${esc(l.last_name)}</b></td><td>${esc(l.title)}</td>` +
      `<td>${esc(l.company)}</td><td><span class="dblink" data-id="${l.id}">${esc(l.email)}</span></td>` +
      `<td>${l.industry ? `<span class="pill p-gray">${esc(l.industry)}</span>` : `<span class="sk">—</span>`}</td>` +
      `<td>${espCell(l)}</td><td>${l.employees ?? ""}</td><td>${esc(l.country)}</td>` +
      `<td>${esc(l.seniority)}</td><td>${esc(l.email_status)}</td>` +
      dataCols.map(c => `<td>${esc(String(data[c] ?? ""))}</td>`).join("") + `</tr>`;
  });
  h += `</tbody></table></div>`;
  h += `<div class="dbpage">
    <button class="gbtn" id="dbPrev" ${d.page <= 1 ? "disabled" : ""}>‹ Prev</button>
    <span>Page ${d.page} of ${Math.max(1, d.pages)}</span>
    <button class="gbtn" id="dbNext" ${d.page >= d.pages ? "disabled" : ""}>Next ›</button>
    <select id="dbPageSize">${[50, 100, 200].map(n => `<option ${db.pageSize === n ? "selected" : ""}>${n}</option>`).join("")}</select>
  </div>`;
  $("databaseView").innerHTML = h;
  wireDatabase();
}

function wireDatabase(){
  const db = dbState();
  const num = id => { const n = parseInt(($(id) || {}).value, 10); return Number.isFinite(n) ? n : null; };
  const txt = id => { const e = $(id); return e && e.value.trim() ? e.value.trim() : null; };
  const readFilters = () => {
    db.filters = {
      industry: $("dbInd").value || null, esp: $("dbEsp").value || null,
      title_status: $("dbTitle").value || null,
      employees_min: num("dbEmpMin"), employees_max: num("dbEmpMax"),
      country: txt("dbCountry"), seniority: txt("dbSeniority"), q: txt("dbSearch"),
    };
  };
  const resetSel = () => { db.selected.clear(); db.selectAll = false; };
  $("dbApply").onclick = () => { readFilters(); db.page = 1; resetSel(); fetchDatabase(); };
  $("dbClear").onclick = () => { db.filters = {}; db.page = 1; resetSel(); fetchDatabase(); };
  $("dbSearch").onkeydown = e => { if(e.key === "Enter"){ readFilters(); db.page = 1; resetSel(); fetchDatabase(); } };
  $("dbPrev").onclick = () => { if(db.page > 1){ db.page--; fetchDatabase(); } };
  $("dbNext").onclick = () => { db.page++; fetchDatabase(); };
  $("dbPageSize").onchange = e => { db.pageSize = parseInt(e.target.value, 10); db.page = 1; fetchDatabase(); };
  const selAll = $("dbSelAll");
  selAll.onchange = () => {
    db.selectAll = false;
    (db.data.leads || []).forEach(l => selAll.checked ? db.selected.add(l.id) : db.selected.delete(l.id));
    renderDatabase();
  };
  document.querySelectorAll(".dbcb").forEach(cb => cb.onchange = () => {
    const id = +cb.dataset.id;
    if(db.selectAll){ db.selectAll = false; (db.data.leads || []).forEach(l => db.selected.add(l.id)); }
    cb.checked ? db.selected.add(id) : db.selected.delete(id);
    renderDatabase();
  });
  $("dbExport").onclick = dbExport;
  $("dbSendBtn").onclick = dbSend;
  const tA = $("dbTitleAll"); if(tA) tA.onclick = () => runDbJob("titlecheck");
  const eA = $("dbEspAll"); if(eA) eA.onclick = () => runDbJob("esp");
  const cA = $("dbClassifyAll"); if(cA) cA.onclick = () => runDbJob("classify");
  const byId = {}; (db.data.leads || []).forEach(l => { byId[l.id] = l; });
  document.querySelectorAll(".dblink").forEach(x => x.onclick = () => openDbDetail(byId[x.dataset.id]));
  updateDbSel();
}

function openDbDetail(l){
  if(!l) return;
  const fixed = [["Name", `${l.first_name || ""} ${l.last_name || ""}`.trim()], ["Title", l.title],
    ["Company", l.company], ["Email", l.email], ["Website", l.website], ["Industry", l.industry],
    ["ESP", l.esp], ["Employees", l.employees], ["Country", l.country], ["State", l.state],
    ["Seniority", l.seniority], ["Email status", l.email_status], ["Title check", l.title_status]];
  let h = `<div class="dtop"><div><div class="dname">${esc(`${l.first_name || ""} ${l.last_name || ""}`.trim())}</div>` +
    `<div class="dsub">${esc(l.company || "")}${l.title ? " · " + esc(l.title) : ""}</div></div><i class="dclose" id="dbDetClose">✕</i></div>`;
  const field = (k, v) => {
    if(v === undefined || v === null || v === "") return "";
    return `<div class="dfield"><div class="dk">${esc(k)}</div><div class="dval">${esc(String(v))}</div></div>`;
  };
  fixed.forEach(([k, v]) => { h += field(k, v); });
  const data = l.data || {};
  Object.keys(data).forEach(k => { h += field(k, data[k]); });
  const d = $("detail"); d.innerHTML = h; d.hidden = false;
  $("dbDetClose").onclick = () => { d.hidden = true; };
}

function updateDbSel(){
  const db = dbState(), d = db.data || {};
  const total = d.total || 0, pageLeads = d.leads || [];
  const pageAllSel = pageLeads.length > 0 && pageLeads.every(l => db.selectAll || db.selected.has(l.id));
  const info = $("dbSelInfo"), notice = $("dbSelNotice");
  if(info) info.textContent = db.selectAll ? `${total.toLocaleString()} selected`
    : (db.selected.size ? `${db.selected.size} selected` : "");
  if(notice){
    if(db.selectAll){
      notice.hidden = false;
      notice.innerHTML = `All <b>${total.toLocaleString()}</b> leads selected. <a class="gtact" id="dbClearSel">Clear selection</a>`;
    } else if(db.selected.size && pageAllSel && total > pageLeads.length){
      notice.hidden = false;
      notice.innerHTML = `All <b>${pageLeads.length}</b> on this page selected. ` +
        `<a class="gtact" id="dbSelAllFiltered">Select all ${total.toLocaleString()}</a> · <a class="gtact" id="dbClearSel">Clear</a>`;
    } else if(db.selected.size){
      notice.hidden = false;
      notice.innerHTML = `<b>${db.selected.size}</b> selected · <a class="gtact" id="dbClearSel">Clear</a>`;
    } else { notice.hidden = true; notice.innerHTML = ""; }
  }
  const sa = $("dbSelAllFiltered"); if(sa) sa.onclick = () => { db.selectAll = true; renderDatabase(); };
  const cl = $("dbClearSel"); if(cl) cl.onclick = () => { db.selectAll = false; db.selected.clear(); renderDatabase(); };
}

// Poll a running database-wide job. The job runs on the server regardless of the
// browser, so this just reflects its progress and can reconnect after a reload.
function pollDbJob(jobId, kind){
  const labels = { classify: "Classifying", esp: "Checking ESP", titlecheck: "Title-checking" };
  const setMsg = t => { const m = $("dbJobMsg"); if(m) m.textContent = t; };
  if(state.dbJobPoll) clearInterval(state.dbJobPoll);
  state.dbJobPoll = setInterval(async () => {
    let j; try{ j = await api("/api/jobs/" + jobId); }catch(e){ return; }
    const fin = ["done", "error", "cancelled"].includes(j.status);
    setMsg(`${labels[kind] || "Running"}: ${(j.done || 0).toLocaleString()} of ${(j.total || 0).toLocaleString()}` +
      (fin ? " — done ✓" : "… (keeps running in the background)"));
    if(fin){ clearInterval(state.dbJobPoll); state.dbJobPoll = null; fetchDatabase(); }
  }, 2000);
}

async function runDbJob(kind){
  const slug = encodeURIComponent(state.variableSet);
  const setMsg = t => { const m = $("dbJobMsg"); if(m) m.textContent = t; };
  if(kind === "classify" && !confirm("Classify every not-yet-classified lead in the whole database? This uses OpenAI credit.")) return;
  setMsg("Starting…");
  let r;
  try{
    r = await api(`/api/workspaces/${slug}/run-all`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, workers: kind === "classify" ? 100 : (kind === "esp" ? 40 : null) }) });
  }catch(e){ setMsg("Failed to start"); return; }
  if(!r.job_id || !r.count){ setMsg("Nothing to run — all leads already done"); return; }
  pollDbJob(r.job_id, kind);
}

// ids to send: explicit selection wins; "select all" uses the filters (no ids)
function dbSelectionIds(){
  const db = dbState();
  return (!db.selectAll && db.selected.size) ? [...db.selected] : null;
}

async function dbExport(){
  const db = dbState();
  const body = Object.assign({}, db.filters);
  const ids = dbSelectionIds(); if(ids) body.lead_ids = ids;
  try{
    const r = await fetch(`/api/workspaces/${encodeURIComponent(state.variableSet)}/database/export`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if(r.status === 401){ window.location = "/login"; return; }
    if(!r.ok){ alert("Export failed"); return; }
    const blob = await r.blob(), url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = `${state.variableSet}_database.csv`;
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  }catch(e){ alert("Export failed"); }
}

async function dbSend(){
  const db = dbState(), panel = $("dbSendPanel");
  if(!panel.hidden){ panel.hidden = true; return; }
  let wss = []; try{ wss = await api("/api/workspaces"); }catch(e){}
  const targets = wss.filter(w => w.key !== state.variableSet);
  const total = (db.data ? db.data.total : 0);
  const what = db.selectAll ? `${total.toLocaleString()} (all)`
    : (db.selected.size ? `${db.selected.size} selected` : `${total.toLocaleString()} filtered`);
  panel.innerHTML = `Send <b>${what}</b> to ` +
    `<select id="dbTarget">${targets.map(w => `<option value="${esc(w.key)}">${esc(w.name)}</option>`).join("")}</select> ` +
    `<input id="dbListName" placeholder="New list name (optional)" /> ` +
    `<button class="run" id="dbSendGo">Send</button> <span class="gtact" id="dbSendCancel">Cancel</span> <span id="dbSendMsg" class="muted"></span>`;
  panel.hidden = false;
  $("dbSendCancel").onclick = () => { panel.hidden = true; };
  $("dbSendGo").onclick = async () => {
    const target = $("dbTarget").value; if(!target) return;
    const body = Object.assign({}, db.filters, { target, list_name: $("dbListName").value.trim() });
    const ids = dbSelectionIds(); if(ids) body.lead_ids = ids;
    $("dbSendGo").disabled = true; $("dbSendMsg").textContent = "Sending…";
    try{
      const r = await api(`/api/workspaces/${encodeURIComponent(state.variableSet)}/database/send`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      $("dbSendMsg").textContent = `Copied ${r.copied} to ${r.target}. Switch to that workspace to verify/enrich.`;
      db.selected.clear(); updateDbSel();
    }catch(e){ $("dbSendMsg").textContent = "Send failed."; }
    $("dbSendGo").disabled = false;
  };
}

function showView(name){
  state.view = name;
  $("gridWrap").hidden = name !== "table";
  $("runbar").hidden = name !== "table";
  if(name !== "table"){
    $("gridtools").hidden = true;
    $("importPanel").hidden = true;
    $("enrichPanel").hidden = true;
  }
  $("formatView").hidden = name !== "format";
  const rv = $("rulesView"); if(rv) rv.hidden = name !== "rules";
  const dv = $("databaseView"); if(dv) dv.hidden = name !== "database";
  $("settingsView").hidden = name !== "settings";
  updateRunUI();
}

async function loadRules(){
  showView("rules");
  $("viewTitle").textContent = "Correction rules";
  $("viewSub").textContent = "Plain-English do / avoid rules added to every enrichment in this set. Applies live to leads not yet enriched.";
  const r = await api("/api/rules/" + encodeURIComponent(state.variableSet));
  $("rulesView").innerHTML =
    `<div class="fv-sel"><label>Format set <b>${esc(state.variableSet)}</b></label></div>` +
    `<p class="rules-help">One rule per line. The AI obeys these on top of everything else. Examples:<br>` +
    `&bull; In value_proposition, the "by ..." part must describe OUR service, never the prospect's own service.<br>` +
    `&bull; Never mention the prospect's pricing or specific dollar figures.<br>` +
    `&bull; Keep sentence 1 about getting them more clients, not about what they sell.</p>` +
    `<textarea id="rulesText" class="rules-ta" placeholder="Type one rule per line...">${esc(r.text || "")}</textarea>` +
    `<div class="rules-actions"><button class="run" id="rulesSave">Save rules</button>` +
    `<span class="muted" id="rulesMsg"></span></div>`;
  $("rulesSave").onclick = saveRules;
}

async function saveRules(){
  const text = $("rulesText").value;
  try{
    const res = await api("/api/rules/" + encodeURIComponent(state.variableSet), {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const m = $("rulesMsg");
    if(m){ m.textContent = `Saved · ${res.count} rule${res.count === 1 ? "" : "s"} active`;
      setTimeout(() => { m.textContent = ""; }, 4000); }
  }catch(e){ const m = $("rulesMsg"); if(m) m.textContent = "Save failed"; }
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
  h += `<div class="fv-h" style="display:flex;align-items:center;gap:8px">Client profile <span class="muted" style="margin-left:6px">— who we're writing for</span>` +
    (profile.editable ? `<button class="gbtn" id="profJsonBtn" style="margin-left:auto;padding:6px 11px">Paste profile JSON</button><span class="vacts"><span class="vact" id="wsDelete">delete workspace</span></span>` : "") + `</div><div class="card">`;
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
  if(profile.editable) h += profJsonPanelHtml();
  h += `<div class="fv-h" style="display:flex;align-items:center;gap:8px">Variables <span class="muted" style="margin-left:6px">— what we generate & how to write them</span>` +
    (profile.editable
      ? `<button class="gbtn" id="dlJsonBtn" style="margin-left:auto;padding:6px 11px">Download JSON</button><button class="gbtn" id="jsonBtn" style="padding:6px 11px">Paste variables JSON</button><button class="run" id="addVarBtn" style="padding:6px 11px">+ Add variable</button>`
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
  if($("profJsonBtn")) $("profJsonBtn").onclick = () => { const p = $("profJsonPanel"); p.hidden = !p.hidden; };
  if($("profJsonImport")) $("profJsonImport").onclick = importProfileJson;
  if($("profJsonCancel")) $("profJsonCancel").onclick = () => { $("profJsonPanel").hidden = true; };
  $("cvTemplate").oninput = detectPlaceholders;
  $("cvSave").onclick = saveCustom;
  $("cvCancel").onclick = () => { $("builder").hidden = true; resetBuilder(); };
  $("formatView").querySelectorAll("[data-del]").forEach(x => x.onclick = () => deleteCustom(x.dataset.del));
  $("formatView").querySelectorAll("[data-dup]").forEach(x => x.onclick = () => duplicateVar(x.dataset.dup));
  $("formatView").querySelectorAll("[data-hide]").forEach(x => x.onclick = () => toggleHide(x.dataset.hide, x.dataset.on !== "1"));
  $("formatView").querySelectorAll("[data-edit]").forEach(x => x.onclick = () => editCustom(parseInt(x.dataset.edit, 10)));
}

function profJsonPanelHtml(){
  const ph = '{\n  "service_brief": "What the client does...",\n  "main_offer": "...",\n  "what_we_are_pitching": "...",\n  "target_outcome": ["...", "..."],\n  "icp_summary": "..."\n}';
  return `<div class="card builder" id="profJsonPanel" hidden>
    <div class="blabel">Paste the CLIENT PROFILE JSON here (just the profile — variables go in the box below). Flat objects and arrays are fine.</div>
    <textarea id="profJsonText" rows="10" placeholder='${ph.replace(/'/g, "&#39;")}'></textarea>
    <div class="brow"><button class="run" id="profJsonImport">Import profile</button><button class="gbtn" id="profJsonCancel">Cancel</button><span class="savedmsg" id="profJsonMsg"></span></div>
  </div>`;
}

async function importProfileJson(){
  let data;
  try{ data = JSON.parse($("profJsonText").value); }
  catch(e){ const m = $("profJsonMsg"); m.textContent = "Invalid JSON — check the format."; m.style.color = "var(--red-tx)"; return; }
  const norm = normalizeConfigJson(data);
  try{
    await api(`/api/workspaces/${state.variableSet}/import`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: norm.profile, variables: [] }),   // profile only
    });
    const m = $("profJsonMsg"); m.style.color = "var(--green-tx)"; m.textContent = "✓ Profile imported";
    setTimeout(loadFormat, 700);
  }catch(e){ const m = $("profJsonMsg"); m.textContent = "Import failed: " + e.message; m.style.color = "var(--red-tx)"; }
}

function jsonPanelHtml(){
  const ph = '{\n  "variables": [\n    { "label": "Value Proposition", "min_words": 45, "max_words": 80,\n      "guidance": "How to write it...", "template": "We specialize in {{x}} ...",\n      "examples": ["..."], "placeholders": [{ "token": "x", "description": "...", "examples": ["..."] }] }\n  ]\n}';
  return `<div class="card builder" id="jsonPanel" hidden>
    <div class="blabel">Paste the VARIABLES JSON here (just the variables — the client profile goes in the box above). A bare array of variables also works.</div>
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

// Accept either { profile:{...}, variables:[...] } or a flat profile object
// (all the fields at the top level). A flat object becomes the profile.
function normalizeConfigJson(data){
  if(data && typeof data === "object" && !Array.isArray(data) && ("profile" in data || "variables" in data)){
    return { profile: data.profile || {}, variables: data.variables || [] };
  }
  return { profile: data || {}, variables: [] };
}

async function importJson(){
  let data;
  try{ data = JSON.parse($("jsonText").value); }
  catch(e){ const m = $("jsonMsg"); m.textContent = "Invalid JSON — check the format."; m.style.color = "var(--red-tx)"; return; }
  const body = Array.isArray(data) ? { profile: {}, variables: data } : normalizeConfigJson(data);
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
    : (b.error ? "Key set, but balance check failed" : `Connected · ${b.daily ?? 0} free daily + ${b.instant ?? 0} instant (paid) credits`);
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
      body: JSON.stringify(normalizeConfigJson(jsonData)),
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

// ---- CSV column mapping on import ----
function parseCsvHeaderLine(line){
  const out = []; let cur = "", q = false;
  for(let i = 0; i < line.length; i++){
    const c = line[i];
    if(q){ if(c === '"'){ if(line[i + 1] === '"'){ cur += '"'; i++; } else q = false; } else cur += c; }
    else { if(c === '"') q = true; else if(c === ',') { out.push(cur); cur = ""; } else cur += c; }
  }
  out.push(cur);
  return out.map(h => h.trim()).filter((h, i, a) => h !== "" || i < a.length);
}

function readCsvHeaders(file){
  return new Promise(resolve => {
    const fr = new FileReader();
    fr.onload = () => {
      const text = String(fr.result || "");
      const line = (text.split(/\r?\n/).find(l => l.trim()) || "");
      resolve(line ? parseCsvHeaderLine(line) : []);
    };
    fr.onerror = () => resolve([]);
    fr.readAsText(file.slice(0, 65536));
  });
}

const MAP_FIELDS = [
  ["first_name", "First name", ["first name", "firstname", "first_name"]],
  ["last_name", "Last name", ["last name", "lastname", "last_name"]],
  ["email", "Email", ["email"]],
  ["title", "Title", ["title", "jobtitle", "job title"]],
  ["company", "Company", ["company", "companyname", "company name"]],
  ["website", "Website", ["website", "url", "domain", "company website"]],
  ["employees", "# Employees", ["# employees", "employees", "employee count", "num employees", "company size", "headcount", "size"]],
  ["country", "Country", ["country"]],
  ["state", "State", ["state", "region", "province"]],
  ["seniority", "Seniority", ["seniority", "seniority level"]],
  ["industry", "Industry (already classified)", ["industry"]],
];

async function openMapModal(headers){
  state._mapHeaders = headers;
  await renderMapModal();
}

async function renderMapModal(){
  const headers = state._mapHeaders || [];
  let custom = [];
  try{ custom = await api("/api/import-fields"); }catch(e){ custom = []; }
  const lowmap = {}; headers.forEach(h => { lowmap[h.toLowerCase()] = h; });
  const guess = cands => { for(const c of cands){ if(lowmap[c]) return lowmap[c]; } return ""; };
  const optList = sel => `<option value="">Don't import</option>` +
    headers.map(h => `<option ${h === sel ? "selected" : ""}>${esc(h)}</option>`).join("");
  let h = `<div class="modal-box"><div class="modal-h">Map your CSV columns<i class="modal-x" id="mapClose">✕</i></div>` +
    `<div class="modal-sub">Pick which column feeds each field; "Don't import" to skip. Map our Industry column to import leads already-classified. ` +
    `Add custom fields (LinkedIn, Company Address…) — they're saved for every future import.</div><div class="maprows">`;
  MAP_FIELDS.forEach(([key, label, cands]) => {
    h += `<div class="maprow"><span class="mapf">${label}</span><select data-mf="${key}">${optList(guess(cands))}</select></div>`;
  });
  custom.forEach(name => {
    h += `<div class="maprow"><span class="mapf">${esc(name)} <i class="cf-del" data-cf="${esc(name)}" title="Remove this field">✕</i></span>` +
      `<select data-mf="${esc(name)}">${optList(guess([name.toLowerCase()]))}</select></div>`;
  });
  h += `</div><div class="mapacts"><button class="gbtn" id="mapAddField">+ Add custom field</button>` +
    `<button class="run" id="mapFinish">Finish mapping & import</button><span class="gtact" id="mapCancel">Cancel</span></div></div>`;
  const m = $("mapModal"); m.innerHTML = h; m.hidden = false;
  const close = () => { m.hidden = true; };
  $("mapClose").onclick = $("mapCancel").onclick = close;
  $("mapAddField").onclick = addImportField;
  m.querySelectorAll(".cf-del").forEach(x => x.onclick = async () => {
    try{ await api("/api/import-fields/" + encodeURIComponent(x.dataset.cf), { method: "DELETE" }); }catch(e){}
    renderMapModal();
  });
  $("mapFinish").onclick = () => {
    const mapping = {};
    m.querySelectorAll("[data-mf]").forEach(s => { if(s.value) mapping[s.dataset.mf] = s.value; });
    state._pendingMapping = mapping; close(); createList();
  };
}

async function addImportField(){
  const name = prompt("Custom field name (e.g. LinkedIn, Company Address):");
  if(!name || !name.trim()) return;
  try{
    await api("/api/import-fields", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }) });
  }catch(e){}
  renderMapModal();
}

async function createList(){
  const name = ($("listName").value || "New list").trim();
  const file = $("fileInput").files[0];
  // If a file is chosen, first show the column-mapping step (like Instantly/Bison).
  if(file && !state._pendingMapping){
    const headers = await readCsvHeaders(file);
    if(headers && headers.length){ openMapModal(headers); return; }
  }
  const mapping = state._pendingMapping; state._pendingMapping = null;
  const { id } = await api("/api/lists", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, variable_set: state.variableSet }),
  });
  let count = 0;
  if(file){
    const fd = new FormData(); fd.append("file", file);
    if(mapping) fd.append("mapping", JSON.stringify(mapping));
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
  $("rulesBtn").onclick = loadRules;
  $("databaseBtn").onclick = loadDatabase;
  const lo = $("logoutBtn");
  if(lo) lo.onclick = async () => {
    if(!confirm("Log out of the workspace?")) return;
    try{ await fetch("/api/logout", { method: "POST" }); }catch(e){}
    window.location = "/";
  };
  $("settingsBtn").onclick = loadSettings;
  $("wsBtn").onclick = e => { if(e.target.closest("#collapseBtn")) return; toggleWsMenu(); };
  $("runBtn").onclick = $("runBtn2").onclick = run;
  $("verifyBtn").onclick = verify;
  $("titleBtn").onclick = titleCheck;
  $("espBtn").onclick = espCheck;
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
