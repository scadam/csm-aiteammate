"""Thin dynamic manager and fleet pages backed by the ASGI APIs."""

from __future__ import annotations

import html
import json

from .spec import SolutionSpec


def control_plane_page(spec: SolutionSpec, view: str) -> str:
    title = spec.control_plane.manager.title if view == "manager" else spec.control_plane.fleet.title
    tokens = {
        "__APP_NAME__": html.escape(spec.solution.name),
        "__PAGE_TITLE__": html.escape(title),
        "__VIEW__": json.dumps(view),
        "__TERMS__": json.dumps(spec.solution.terms.model_dump()).replace("<", "\\u003c"),
    }
    page = _PAGE
    for key, value in tokens.items():
        page = page.replace(key, value)
    return page


_PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>__PAGE_TITLE__</title>
  <script src="https://res.cdn.office.net/teams-js/2.31.0/js/MicrosoftTeams.min.js" crossorigin="anonymous"></script>
  <style>
    :root{
      --ink:#18232b;--muted:#5f6d75;--subtle:#7b878e;--line:#d9e0e2;
      --line-strong:#c5ced1;--surface:#fff;--surface-soft:#f6f8f8;
      --canvas:#edf2f2;--brand:#0b625b;--brand-strong:#084a45;
      --brand-soft:#e6f2f0;--warning:#9b5c0d;--warning-soft:#fff4dd;
      --danger:#a33a31;--danger-soft:#fcecea;--ok:#26734d;--ok-soft:#e9f5ee;
      --shadow:0 1px 2px rgba(18,40,47,.06),0 7px 22px rgba(18,40,47,.04);
      --font:"Aptos","Segoe UI Variable Text","Segoe UI",sans-serif;
    }
    *{box-sizing:border-box}
    html{background:var(--canvas)}
    body{margin:0;min-width:0;background:linear-gradient(180deg,#f8fafa 0,#edf2f2 440px);color:var(--ink);font:14px/1.5 var(--font);letter-spacing:0}
    button,select,textarea{font-family:var(--font);letter-spacing:0}
    button:focus-visible,select:focus-visible,textarea:focus-visible,a:focus-visible{outline:3px solid rgba(11,98,91,.22);outline-offset:2px}
    .topbar{background:rgba(255,255,255,.96);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10;backdrop-filter:blur(10px)}
    .topbar-inner{max-width:1360px;min-height:68px;margin:0 auto;padding:0 28px;display:flex;align-items:center;justify-content:space-between;gap:24px}
    .product{min-width:0;display:flex;align-items:center;gap:11px}
    .product-mark{width:36px;height:36px;flex:0 0 36px;border-radius:6px;background:var(--brand);color:#fff;display:grid;place-items:center;font-size:13px;font-weight:700}
    .brand{min-width:0;font-size:16px;font-weight:700;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .brand small{display:block;margin-top:2px;color:var(--muted);font-size:12px;font-weight:400;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    nav{align-self:stretch;display:flex;align-items:center;gap:4px}
    nav a{height:40px;min-width:74px;border-radius:5px;color:var(--muted);display:grid;place-items:center;padding:0 12px;text-decoration:none;font-size:13px;font-weight:600}
    nav a:hover{background:var(--surface-soft);color:var(--ink)}
    nav a.active{background:var(--brand-soft);color:var(--brand-strong)}
    main{max-width:1360px;margin:0 auto;padding:30px 28px 56px}
    .page-heading{min-height:58px;margin-bottom:22px;display:flex;align-items:flex-end;justify-content:space-between;gap:20px}
    .eyebrow{margin-bottom:4px;color:var(--brand);font-size:12px;font-weight:700;text-transform:uppercase}
    h1{margin:0;font-size:28px;font-weight:650;line-height:1.2}
    h2{margin:0;font-size:16px;font-weight:650;line-height:1.3}
    .agent-state{height:32px;border:1px solid #bcd7cc;border-radius:16px;background:var(--ok-soft);color:var(--ok);display:flex;align-items:center;gap:8px;padding:0 12px;font-size:12px;font-weight:650;white-space:nowrap}
    .state-dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px rgba(38,115,77,.12)}
    .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin-bottom:18px}
    .metric{min-height:94px;border:1px solid var(--line);border-radius:6px;background:var(--surface);box-shadow:0 1px 2px rgba(18,40,47,.03);padding:16px 17px;display:flex;flex-direction:column;justify-content:space-between}
    .metric b{font-size:26px;font-weight:650;line-height:1;font-variant-numeric:tabular-nums}
    .metric span{color:var(--muted);font-size:12px;font-weight:600}
    .metric.attention{border-color:#e5c07b;background:var(--warning-soft)}
    .metric.attention b,.metric.attention span{color:#784708}
    .workspace-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px}
    .span-two{grid-column:1/-1}
    .panel{min-width:0;border:1px solid var(--line);border-radius:6px;background:var(--surface);box-shadow:var(--shadow);overflow:hidden}
    .panel-heading{min-height:56px;border-bottom:1px solid var(--line);padding:14px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px}
    .panel-heading-group{display:flex;align-items:center;gap:9px;min-width:0}
    .panel-heading .count{min-width:24px;height:24px;border-radius:12px;background:var(--surface-soft);color:var(--muted);display:grid;place-items:center;padding:0 7px;font-size:12px;font-weight:650}
    .panel-heading .count.attention{background:var(--warning-soft);color:var(--warning)}
    .panel-body{padding:0 18px 18px}
    .panel-body.flush{padding:0}
    .form{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding-top:18px}
    .form label{display:grid;gap:6px;color:var(--ink);font-size:12px;font-weight:650}
    .form .wide{grid-column:1/-1}
    select,textarea{width:100%;border:1px solid var(--line-strong);border-radius:5px;background:#fff;color:var(--ink);padding:9px 10px;font-size:14px;font-weight:400}
    select{height:42px}
    textarea{min-height:82px;resize:vertical;line-height:1.45}
    select:hover,textarea:hover{border-color:#9eabad}
    button{min-height:38px;width:max-content;border:1px solid var(--brand);border-radius:5px;background:var(--brand);color:#fff;cursor:pointer;padding:8px 14px;font-size:13px;font-weight:650}
    button:hover{border-color:var(--brand-strong);background:var(--brand-strong)}
    button:disabled{cursor:wait;opacity:.6}
    button.secondary{border-color:var(--line-strong);background:#fff;color:var(--ink)}
    button.secondary:hover{border-color:#9eabad;background:var(--surface-soft)}
    button.danger{border-color:#e7b8b4;background:var(--danger-soft);color:var(--danger)}
    button.danger:hover{border-color:var(--danger);background:var(--danger);color:#fff}
    .form-actions{grid-column:1/-1;display:flex;justify-content:flex-end;padding-top:2px}
    .data-table{width:100%;border-collapse:collapse;table-layout:auto}
    .data-table th,.data-table td{text-align:left;border-bottom:1px solid #e6ebed;padding:11px 14px;vertical-align:middle}
    .data-table th{height:40px;background:var(--surface-soft);color:var(--muted);font-size:11px;font-weight:700;white-space:nowrap}
    .data-table td{font-size:13px;overflow-wrap:anywhere}
    .data-table tbody tr:last-child td{border-bottom:0}
    .data-table tbody tr:hover{background:#fafcfc}
    .data-table td:first-child{font-weight:600}
    .status{width:max-content;border-radius:12px;background:var(--surface-soft);color:var(--muted);display:inline-flex;align-items:center;padding:3px 8px;font-size:11px;font-weight:700;white-space:nowrap}
    .status.pending_review{background:var(--warning-soft);color:var(--warning)}
    .status.complete,.status.approved{background:var(--ok-soft);color:var(--ok)}
    .status.failed,.status.rejected{background:var(--danger-soft);color:var(--danger)}
    .empty{min-height:112px;color:var(--muted);display:grid;place-items:center;padding:24px;text-align:center}
    .error{border:1px solid #efc1bd;border-radius:6px;background:var(--danger-soft);color:var(--danger);padding:14px 16px}
    .actions{display:flex;gap:7px;flex-wrap:wrap}
    .actions button{min-height:32px;padding:5px 10px;font-size:12px}
    .reference{white-space:nowrap}
    .loading{min-height:180px;color:var(--muted);display:grid;place-items:center}
    .ui-toolbar{display:flex;align-items:end;gap:9px;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid var(--line);background:#fbfcfc}
    .ui-filter{display:grid;gap:4px;min-width:138px;color:var(--muted);font-size:10px;font-weight:700}
    .ui-filter input{height:36px;border:1px solid var(--line-strong);border-radius:5px;padding:6px 9px;color:var(--ink);font-size:13px}
    .ui-bulk{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-left:auto}
    .ui-bulk span{color:var(--muted);font-size:11px;font-weight:650}
    .ui-sort{min-height:0;border:0;background:transparent;color:inherit;padding:0;font-size:11px}
    .ui-sort:hover{border:0;background:transparent;color:var(--brand)}
    .ui-json{display:block;max-width:480px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;color:var(--muted)}
    .progress{display:grid;grid-template-columns:minmax(76px,1fr) auto;gap:7px;align-items:center;min-width:120px}
    .progress progress{width:100%;height:8px;accent-color:var(--brand)}
    .progress span{color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
    .ui-footer{min-height:42px;border-top:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:12px;padding:7px 14px;color:var(--muted);font-size:11px}
    .ui-pagination{display:flex;align-items:center;gap:7px}.ui-pagination button{min-height:29px;padding:4px 8px}
    @media(max-width:800px){
      .topbar-inner{padding:10px 18px;flex-wrap:wrap;gap:8px 16px}
      .product{max-width:100%}nav{width:100%;height:42px}nav a{height:36px;min-width:0;flex:1}
      main{padding:24px 18px 44px}.page-heading{align-items:flex-start}.workspace-grid{grid-template-columns:1fr}.span-two{grid-column:auto}
      .form{grid-template-columns:1fr}.form .wide{grid-column:auto}.form-actions{grid-column:auto}
    }
    @media(max-width:560px){
      main{padding:20px 12px 38px}.page-heading{min-height:0;margin-bottom:18px;align-items:flex-start}.agent-state{display:none}
      h1{font-size:24px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.metric{min-height:82px;padding:13px}.metric b{font-size:23px}
      .workspace-grid{gap:12px}.panel-heading{padding:13px 14px}.panel-body{padding:0 14px 14px}
      .data-table,.data-table tbody,.data-table tr,.data-table td{display:block;width:100%}.data-table thead{display:none}
      .data-table tr{border-bottom:1px solid var(--line);padding:9px 14px}.data-table tbody tr:last-child{border-bottom:0}
      .data-table td{border:0;padding:4px 0;display:grid;grid-template-columns:minmax(96px,38%) minmax(0,1fr);gap:10px;align-items:start}
      .data-table td::before{content:attr(data-label);color:var(--muted);font-size:11px;font-weight:700}
      .data-table tbody tr:hover{background:transparent}.decision-cell{grid-template-columns:1fr!important;gap:7px!important}.decision-cell .actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));padding-top:0}.decision-cell .actions button{width:100%;white-space:nowrap}
    }
  </style>
</head>
<body>
<header class="topbar"><div class="topbar-inner"><div class="product"><div class="product-mark" aria-hidden="true">AI</div><div class="brand">__APP_NAME__<small id="identity">Loading identity</small></div></div><nav aria-label="Primary"><a href="/manager">Manager</a><a href="/fleet">Fleet</a><a href="/docs">API</a></nav></div></header>
<main><div class="page-heading"><div><div class="eyebrow" id="workspace-label">Human oversight</div><h1>__PAGE_TITLE__</h1></div><div class="agent-state" id="agent-state"><span class="state-dot"></span><span>Checking runtime</span></div></div><div id="content"><div class="loading">Loading workspace</div></div></main>
<script>
const VIEW=__VIEW__, TERMS=__TERMS__;
let IDENTITY_TOKEN='';
let CURRENT_IDENTITY=null,UI_RESOURCES=[],UI_REFRESH_TIMER=null;
const AG_UI_STATE=new Map(),AG_UI_IN_FLIGHT=new Set();
const TEAMS_TIMEOUT_MS=1500;
function looksTeamsEmbedded(){try{const query=(window.location.search||'').toLowerCase();return window.self!==window.top||query.includes('teams')||query.includes('subentityid')||query.includes('frame_context')||/teams\.|office\.|microsoft365\./.test(document.referrer||'')}catch(e){return true}}
function withTimeout(promise,milliseconds,label){return Promise.race([promise,new Promise((_,reject)=>setTimeout(()=>reject(new Error(`${label} after ${milliseconds}ms`)),milliseconds))])}
// Complete the Teams web-host handshake immediately; identity loading must not delay it.
(function notifyTeamsLoaded(){try{const teams=window.microsoftTeams;if(!teams||!teams.app||!looksTeamsEmbedded())return;teams.app.initialize().then(()=>{if(typeof teams.app.notifySuccess==='function')teams.app.notifySuccess()}).catch(()=>{})}catch(e){}})();
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label=v=>String(v??'').replace(/([a-z0-9])([A-Z])/g,'$1 $2').replace(/[_-]+/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
const status=v=>`<span class="status ${esc(v)}">${esc(label(v))}</span>`;
const json=async(url,opts={})=>{const headers={...(opts.headers||{})};if(IDENTITY_TOKEN)headers.Authorization=`Bearer ${IDENTITY_TOKEN}`;const r=await fetch(url,{...opts,headers});const d=await r.json();if(!r.ok)throw new Error(d.detail||r.statusText);return d};
const reference=v=>{const text=String(v??'');if(text.length<18)return esc(text);const split=text.indexOf('_');const short=split>0?`${text.slice(0,split)} \u00b7 ${text.slice(split+1,split+9)}`:`${text.slice(0,12)}\u2026`;return `<span class="reference" title="${esc(text)}">${esc(short)}</span>`};
const value=(item,column)=>{const raw=item[column];if(column==='status')return status(raw);if(column==='updatedAt'&&raw){const date=new Date(raw);if(!Number.isNaN(date.valueOf()))return esc(date.toLocaleString())}if(column==='workflowId')return esc(label(raw));if(column==='id'||column.endsWith('Id'))return reference(raw);return esc(raw??'\u2014')};
const rows=(items,columns,empty='No records')=>items.length?`<table class="data-table"><thead><tr>${columns.map(c=>`<th>${esc(label(c))}</th>`).join('')}</tr></thead><tbody>${items.map(x=>`<tr>${columns.map(c=>`<td data-label="${esc(label(c))}">${value(x,c)}</td>`).join('')}</tr>`).join('')}</tbody></table>`:`<div class="empty">${esc(empty)}</div>`;
const metric=(name,value,attention=false)=>`<div class="metric${attention?' attention':''}"><b>${esc(value)}</b><span>${esc(label(name))}</span></div>`;
const panel=(title,body,{count=null,attention=false,wide=false,flush=true}={})=>`<section class="panel${wide?' span-two':''}"><div class="panel-heading"><div class="panel-heading-group"><h2>${esc(title)}</h2>${count===null?'':`<span class="count${attention?' attention':''}">${esc(count)}</span>`}</div></div><div class="panel-body${flush?' flush':''}">${body}</div></section>`;
async function me(){const d=await json('/api/me');CURRENT_IDENTITY=d;const identity=document.querySelector('#identity');identity.textContent=label(d.managerId||d.roles?.[0]||'signed in');identity.title=`${d.principalId} / ${d.managerId||''}`;return d}
async function initializeIdentity(){const teams=window.microsoftTeams;if(!teams||!teams.app||!teams.authentication||!looksTeamsEmbedded())return;try{await withTimeout(teams.app.initialize(),TEAMS_TIMEOUT_MS,'teams.initialize');IDENTITY_TOKEN=await withTimeout(teams.authentication.getAuthToken(),TEAMS_TIMEOUT_MS,'teams.getAuthToken')}catch(e){console.info('Teams SSO unavailable; production APIs remain fail-closed.',e?.message||e)}}
async function readiness(){const d=await json('/health');const state=document.querySelector('#agent-state');state.querySelector('span:last-child').textContent=d.status==='ready'?'Agent 365 ready':d.status==='offline'?'Offline development':'Agent 365 degraded';state.title=Object.entries(d.checks).filter(([,v])=>!v.ready).map(([k])=>label(k)).join(', ')}
function nav(){document.querySelector('#workspace-label').textContent=VIEW==='manager'?'Human oversight':'Fleet oversight';document.querySelectorAll('nav a').forEach(a=>{const active=a.getAttribute('href')===`/${VIEW}`;a.classList.toggle('active',active);if(active)a.setAttribute('aria-current','page')})}
function agThread(resource){return`control-plane:${VIEW}:${resource.id}:${CURRENT_IDENTITY?.principalId||'principal'}`}
async function agUiRun(resource,query={},resume=null){if(AG_UI_IN_FLIGHT.has(resource.id))return AG_UI_STATE.get(resource.id);AG_UI_IN_FLIGHT.add(resource.id);const prior=AG_UI_STATE.get(resource.id)||{};const threadId=prior.threadId||agThread(resource);const payload={threadId,runId:crypto.randomUUID(),state:{resourceId:resource.id,query},messages:[],tools:[],context:[],forwardedProps:{surface:'control_plane'}};if(resume)payload.resume=resume;let snapshot=null,interrupt=null,errorMessage='';try{const headers={'content-type':'application/json','accept':'text/event-stream'};if(IDENTITY_TOKEN)headers.Authorization=`Bearer ${IDENTITY_TOKEN}`;const response=await fetch('/api/ag-ui',{method:'POST',headers,body:JSON.stringify(payload)});if(!response.ok){let detail=response.statusText;try{detail=(await response.json()).detail||detail}catch(e){}throw new Error(detail)}const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';const consume=block=>{const line=block.split('\n').find(value=>value.startsWith('data:'));if(!line)return;const event=JSON.parse(line.slice(5).trim());if(event.type==='STATE_SNAPSHOT')snapshot=event.snapshot;if(event.type==='RUN_FINISHED')interrupt=event.outcome?.type==='interrupt'?event.outcome.interrupts?.[0]||null:null;if(event.type==='RUN_ERROR')errorMessage=event.message||'AG-UI run failed'};while(true){const{value,done}=await reader.read();buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});let split;while((split=buffer.indexOf('\n\n'))>=0){consume(buffer.slice(0,split));buffer=buffer.slice(split+2)}if(done)break}if(buffer.trim())consume(buffer);if(errorMessage)throw new Error(errorMessage);const state={resource,snapshot,interrupt,query,threadId,selected:new Set()};AG_UI_STATE.set(resource.id,state);return state}finally{AG_UI_IN_FLIGHT.delete(resource.id)}}
async function loadUiResources(audience){UI_RESOURCES=(await json('/api/ui/resources')).filter(resource=>resource.audience===audience&&resource.surfaces.includes('control_plane'));await Promise.all(UI_RESOURCES.map(async resource=>{try{await agUiRun(resource,{filters:{},sort_field:resource.default_sort.field,sort_direction:resource.default_sort.direction,page:1,page_size:resource.page_size})}catch(error){AG_UI_STATE.set(resource.id,{resource,error:error.message,query:{},selected:new Set(),threadId:agThread(resource)})}}))}
function uiCell(item,column){const raw=item[column];if(column==='status')return status(raw);if(column==='progress')return`<div class="progress"><progress max="100" value="${Number(raw)||0}"></progress><span>${esc(raw||0)}%</span></div>`;if(raw&&typeof raw==='object')return`<code class="ui-json">${esc(JSON.stringify(raw,null,2))}</code>`;return value(item,column)}
function generatedUiPanel(resource){const state=AG_UI_STATE.get(resource.id)||{},snapshot=state.snapshot;if(state.error)return panel(resource.title,`<div class="error">${esc(state.error)}</div>`,{wide:true,flush:false});if(!snapshot)return panel(resource.title,'<div class="loading">Loading live state</div>',{wide:true});const query=state.query||{},filters=query.filters||{},selected=state.selected||new Set(),items=snapshot.items||[],pagination=snapshot.pagination||{};const controls=`<div class="ui-toolbar">${resource.filters.map(field=>`<label class="ui-filter"><span>${esc(label(field))}</span><input data-ui-filter="${esc(field)}" data-resource="${esc(resource.id)}" value="${esc(filters[field]||'')}"></label>`).join('')}<button data-ui-apply="${esc(resource.id)}">Apply</button><button class="secondary" data-ui-refresh="${esc(resource.id)}" title="Refresh live state">Refresh</button>${resource.kind==='hitl'?`<div class="ui-bulk"><span>${selected.size} selected</span>${resource.bulk_actions.map(action=>`<button class="${action==='reject'?'danger':'secondary'}" data-ui-bulk="${esc(action)}" data-resource="${esc(resource.id)}" ${selected.size?'':'disabled'}>${esc(label(action))}</button>`).join('')}</div>`:''}</div>`;const selectHead=resource.kind==='hitl'?'<th><input type="checkbox" data-ui-all="'+esc(resource.id)+'" aria-label="Select all visible reviews"></th>':'';const headings=resource.columns.map(column=>`<th><button class="ui-sort" data-ui-sort="${esc(column)}" data-resource="${esc(resource.id)}">${esc(label(column))}${query.sort_field===column?(query.sort_direction==='asc'?' &#8593;':' &#8595;'):''}</button></th>`).join('');const actionHead=resource.kind==='hitl'?'<th>Decision</th>':'';const body=items.map(item=>{const select=resource.kind==='hitl'?`<td><input type="checkbox" data-ui-select="${esc(item.id)}" data-resource="${esc(resource.id)}" ${selected.has(item.id)?'checked':''}></td>`:'';const cells=resource.columns.map(column=>`<td data-label="${esc(label(column))}">${uiCell(item,column)}</td>`).join('');const actions=resource.kind==='hitl'?`<td class="decision-cell" data-label="Decision"><div class="actions">${(item.decisions||[]).map(action=>`<button class="${action==='reject'?'danger':action==='defer'?'secondary':''}" data-ui-row="${esc(item.id)}" data-ui-decision="${esc(action)}" data-resource="${esc(resource.id)}">${esc(label(action))}</button>`).join('')}</div></td>`:'';return`<tr>${select}${cells}${actions}</tr>`}).join('');const table=body?`<table class="data-table"><thead><tr>${selectHead}${headings}${actionHead}</tr></thead><tbody>${body}</tbody></table>`:'<div class="empty">No matching records</div>';const footer=`<div class="ui-footer"><span>${esc(pagination.total||0)} records · ${esc(snapshot.metrics?Object.entries(snapshot.metrics).map(([key,val])=>`${label(key)}: ${val}`).join(' · '):'')}</span><div class="ui-pagination"><button class="secondary" data-ui-page="${esc(resource.id)}" data-page="${Math.max(1,(pagination.page||1)-1)}" ${(pagination.page||1)<=1?'disabled':''}>Previous</button><span>Page ${esc(pagination.page||1)}</span><button class="secondary" data-ui-page="${esc(resource.id)}" data-page="${(pagination.page||1)+1}" ${pagination.hasMore?'':'disabled'}>Next</button></div></div>`;return`<section class="panel span-two" id="ui-panel-${esc(resource.id)}"><div class="panel-heading"><div class="panel-heading-group"><h2>${esc(resource.title)}</h2><span class="count${resource.kind==='hitl'&&pagination.total?' attention':''}">${esc(pagination.total||0)}</span></div></div><div class="panel-body flush">${controls}${table}${footer}</div></section>`}
function updateUiPanel(resourceId){const resource=UI_RESOURCES.find(item=>item.id===resourceId),target=document.querySelector(`#ui-panel-${CSS.escape(resourceId)}`);if(resource&&target)target.outerHTML=generatedUiPanel(resource)}
function queryFromControls(resource){const current=AG_UI_STATE.get(resource.id)?.query||{},filters={};document.querySelectorAll(`[data-ui-filter][data-resource="${CSS.escape(resource.id)}"]`).forEach(input=>{if(input.value.trim())filters[input.dataset.uiFilter]=input.value.trim()});return{filters,sort_field:current.sort_field||resource.default_sort.field,sort_direction:current.sort_direction||resource.default_sort.direction,page:1,page_size:resource.page_size}}
async function refreshUiResource(resource,query,resume=null){await agUiRun(resource,query||AG_UI_STATE.get(resource.id)?.query||{},resume);updateUiPanel(resource.id)}
async function resolveUiReviews(resource,ids,decision){if(UI_REFRESH_TIMER){clearInterval(UI_REFRESH_TIMER);UI_REFRESH_TIMER=null}let state=AG_UI_STATE.get(resource.id);if(!state?.interrupt){await refreshUiResource(resource,state?.query);state=AG_UI_STATE.get(resource.id)}if(!state?.interrupt)throw new Error('No open AG-UI review interrupt');const decisions=(state.snapshot?.items||[]).filter(item=>ids.has(item.id)).map(item=>{let final={};if(decision==='approve')final=item.proposedEffect||{};if(decision==='edit'){const edited=prompt('Edit the full approved effect JSON',JSON.stringify(item.proposedEffect||{},null,2));if(edited===null)return null;try{final=JSON.parse(edited)}catch(error){alert('Edited effect must be valid JSON');return null}}return{review_id:item.id,expected_digest:item.digest,decision,final}}).filter(Boolean);if(!decisions.length){scheduleUiRefresh();return}await agUiRun(resource,state.query,[{interruptId:state.interrupt.id,status:'resolved',payload:{decisions}}]);await Promise.all(UI_RESOURCES.filter(item=>item.id!==resource.id).map(item=>agUiRun(item,AG_UI_STATE.get(item.id)?.query||{})));await(VIEW==='manager'?manager():fleet())}
function scheduleUiRefresh(){if(UI_REFRESH_TIMER)clearInterval(UI_REFRESH_TIMER);UI_REFRESH_TIMER=setInterval(()=>{if(document.hidden||document.activeElement?.matches('input,textarea,select'))return;UI_RESOURCES.forEach(resource=>refreshUiResource(resource,AG_UI_STATE.get(resource.id)?.query).catch(()=>{}))},15000)}
async function manager(){const d=await json('/api/manager/summary');const fields=[...new Set(['subjectId',...d.summaryFields])];await loadUiResources('manager');
 document.querySelector('#identity').textContent=d.manager.name;
 const form=`<div class="form"><label>Workflow<select id="workflow">${d.workflows.map(w=>`<option value="${esc(w.id)}">${esc(w.title)}</option>`).join('')}</select></label><label>${esc(TERMS.subject_singular)}<select id="subject">${d.subjects.map(s=>`<option value="${esc(s.subjectId)}">${esc(s.name||s.subjectId)}</option>`).join('')}</select></label><label class="wide">Workflow context (JSON)<textarea id="input" spellcheck="false">{}</textarea></label><div class="form-actions"><button id="start-run" onclick="startRun()">Start workflow</button></div></div>`;
 const metrics=`<div class="metrics">${metric(TERMS.subject_plural,d.subjects.length)}${metric('Runs',d.runs.length)}${metric('Pending reviews',d.reviews.length,d.reviews.length>0)}</div>`;
 document.querySelector('#content').innerHTML=`${metrics}<div class="workspace-grid">${UI_RESOURCES.map(generatedUiPanel).join('')}${panel('Start workflow',form,{flush:false})}${panel(TERMS.subject_plural,rows(d.subjects,fields,`No ${TERMS.subject_plural.toLowerCase()} available`),{count:d.subjects.length})}</div>`;scheduleUiRefresh()}
async function fleet(){const d=await json('/api/fleet/summary');await loadUiResources('fleet');const m=d.metrics;const metrics=`<div class="metrics">${Object.entries(m).map(([k,v])=>metric(k,v,k==='pending_reviews'&&v>0)).join('')}</div>`;document.querySelector('#content').innerHTML=`${metrics}<div class="workspace-grid">${UI_RESOURCES.map(generatedUiPanel).join('')}${panel(TERMS.manager_plural,rows(d.managers,['id','name','subjectCount','activeRuns','pendingReviews'],`No ${TERMS.manager_plural.toLowerCase()} available`),{count:d.managers.length,wide:true})}</div>`;scheduleUiRefresh()}
async function startRun(){let input;try{input=JSON.parse(document.querySelector('#input').value||'{}')}catch(e){alert('Workflow context must be valid JSON');return}const button=document.querySelector('#start-run');button.disabled=true;button.textContent='Starting...';try{await json(`/api/workflows/${encodeURIComponent(document.querySelector('#workflow').value)}/runs`,{method:'POST',headers:{'content-type':'application/json','idempotency-key':crypto.randomUUID()},body:JSON.stringify({subject_id:document.querySelector('#subject').value,trigger_mode:'human',input})});await manager()}finally{if(button.isConnected){button.disabled=false;button.textContent='Start workflow'}}}
async function decide(id,decision,payload){let final={};if(decision==='approve')final=JSON.parse(payload||'{}');if(decision==='edit'){const edited=prompt('Edit the approved effect JSON',payload||'{}');if(edited===null)return;try{final=JSON.parse(edited)}catch(e){alert('Edited effect must be valid JSON');return}}await json(`/api/reviews/${encodeURIComponent(id)}`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({decision,final})});await manager()}
document.addEventListener('click',async event=>{const button=event.target.closest('button');if(!button)return;const resourceId=button.dataset.resource||button.dataset.uiApply||button.dataset.uiRefresh||button.dataset.uiPage;if(!resourceId)return;const resource=UI_RESOURCES.find(item=>item.id===resourceId);if(!resource)return;try{if(button.dataset.uiApply)await refreshUiResource(resource,queryFromControls(resource));else if(button.dataset.uiRefresh)await refreshUiResource(resource,AG_UI_STATE.get(resource.id)?.query);else if(button.dataset.uiSort){const current=AG_UI_STATE.get(resource.id)?.query||{};await refreshUiResource(resource,{...current,sort_field:button.dataset.uiSort,sort_direction:current.sort_field===button.dataset.uiSort&&current.sort_direction==='asc'?'desc':'asc',page:1})}else if(button.dataset.uiPage){const current=AG_UI_STATE.get(resource.id)?.query||{};await refreshUiResource(resource,{...current,page:Number(button.dataset.page)||1})}else if(button.dataset.uiBulk)await resolveUiReviews(resource,new Set(AG_UI_STATE.get(resource.id)?.selected||[]),button.dataset.uiBulk);else if(button.dataset.uiRow)await resolveUiReviews(resource,new Set([button.dataset.uiRow]),button.dataset.uiDecision)}catch(error){alert(error.message||error)}});
document.addEventListener('change',event=>{const input=event.target,resourceId=input.dataset.resource||input.dataset.uiAll;if(input.dataset.uiSelect){const state=AG_UI_STATE.get(resourceId);if(!state)return;input.checked?state.selected.add(input.dataset.uiSelect):state.selected.delete(input.dataset.uiSelect);updateUiPanel(resourceId)}else if(input.dataset.uiAll){const state=AG_UI_STATE.get(resourceId);if(!state)return;state.selected=new Set(input.checked?(state.snapshot?.items||[]).map(item=>item.id):[]);AG_UI_STATE.set(resourceId,state);updateUiPanel(resourceId)}});
async function load(){try{nav();await initializeIdentity();await Promise.all([me(),readiness()]);await(VIEW==='manager'?manager():fleet())}catch(e){document.querySelector('#content').innerHTML=`<div class="error">${esc(e.message)}. Open this control plane in Microsoft Teams, or enable explicit development identity locally.</div>`}}
load();
</script>
</body></html>'''
