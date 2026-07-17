"""AG-UI state projection and self-contained MCP App for specification review."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import yaml
from ag_ui.core import (
    ActivitySnapshotEvent,
    EventType,
    Interrupt,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
)

from .core import DraftStore


RESOURCE_URI = "ui://ai-teammate-solution-builder/spec-studio"
RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"


def project_spec_graph(spec: dict[str, Any]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    def add(node_id: str, lane: str, title: str, kind: str, pointer: str, value: Any) -> None:
        nodes.append(
            {
                "id": node_id,
                "lane": lane,
                "title": title,
                "kind": kind,
                "pointer": pointer,
                "value": value,
            }
        )

    solution = spec.get("solution", {})
    add("solution", "Purpose", solution.get("name", "Unnamed solution"), "solution", "/solution", solution)
    agent = spec.get("agent", {})
    add("agent", "Teammate", agent.get("display_name", "Agent"), "agent", "/agent", agent)
    edges.append({"from": "solution", "to": "agent", "label": "defines"})

    identity = spec.get("identity", {})
    add("identity", "Identity", "Manager OBO + Agentic user", "identity", "/identity", identity)
    edges.append({"from": "agent", "to": "identity", "label": "acts as"})
    for index, manager in enumerate(spec.get("managers", [])):
        node_id = f"manager:{manager.get('id', index)}"
        add(node_id, "Identity", manager.get("name", node_id), "manager", f"/managers/{index}", manager)
        edges.append({"from": "identity", "to": node_id, "label": "assigned"})

    for key, lane, title in (
        ("runtime", "Runtime", "Runtime topology"),
        ("observability", "Governance", "A365 observability"),
        ("mcp_exposure", "Integrations", "MCP exposure"),
        ("teams_app", "Experience", "Teams application"),
        ("control_plane", "Experience", "Control plane"),
        ("a365", "Governance", "Agent 365 setup"),
    ):
        value = spec.get(key, {})
        if value:
            node_id = f"section:{key}"
            add(node_id, lane, title, key, f"/{key}", value)
            edges.append({"from": "solution", "to": node_id, "label": "configures"})

    capability_nodes: dict[str, str] = {}
    for index, capability in enumerate(spec.get("capabilities", [])):
        capability_id = str(capability.get("id", index))
        node_id = f"capability:{capability_id}"
        capability_nodes[capability_id] = node_id
        add(node_id, "Capabilities", capability.get("title", capability_id), capability.get("kind", "capability"), f"/capabilities/{index}", capability)

    workflow_nodes: dict[str, str] = {}
    for workflow_index, workflow in enumerate(spec.get("workflows", [])):
        workflow_id = str(workflow.get("id", workflow_index))
        node_id = f"workflow:{workflow_id}"
        workflow_nodes[workflow_id] = node_id
        add(node_id, "Workflows", workflow.get("title", workflow_id), "workflow", f"/workflows/{workflow_index}", workflow)
        edges.append({"from": "agent", "to": node_id, "label": "runs"})
        previous = node_id
        for stage_index, stage in enumerate(workflow.get("stages", [])):
            stage_id = f"stage:{workflow_id}:{stage.get('id', stage_index)}"
            add(stage_id, "Workflow stages", stage.get("title", stage_id), stage.get("type", "stage"), f"/workflows/{workflow_index}/stages/{stage_index}", stage)
            edges.append({"from": previous, "to": stage_id, "label": "next"})
            previous = stage_id
            capability_id = str(stage.get("capability", ""))
            if capability_id in capability_nodes:
                edges.append({"from": stage_id, "to": capability_nodes[capability_id], "label": "uses"})

    for skill_index, skill in enumerate(spec.get("skills", [])):
        skill_id = str(skill.get("id", skill_index))
        node_id = f"skill:{skill_id}"
        add(node_id, "Skills", skill.get("title", skill_id), "skill", f"/skills/{skill_index}", skill)
        edges.append({"from": "agent", "to": node_id, "label": "loads"})
        for workflow_id in skill.get("workflows", []):
            if workflow_id in workflow_nodes:
                edges.append({"from": node_id, "to": workflow_nodes[workflow_id], "label": "starts"})

    for index, server in enumerate(spec.get("mcp_servers", [])):
        node_id = f"mcp:{server.get('id', index)}"
        add(node_id, "Integrations", server.get("name", node_id), "mcp", f"/mcp_servers/{index}", server)
    for index, source in enumerate(spec.get("openapi_sources", [])):
        node_id = f"openapi:{source.get('id', index)}"
        add(node_id, "Integrations", source.get("name", node_id), "openapi", f"/openapi_sources/{index}", source)
    for index, source in enumerate(spec.get("data_sources", [])):
        node_id = f"data:{source.get('id', index)}"
        add(node_id, "Data", source.get("name", node_id), "data", f"/data_sources/{index}", source)

    for index, resource in enumerate(spec.get("user_interfaces", {}).get("resources", [])):
        node_id = f"ui:{resource.get('id', index)}"
        add(node_id, "Experience", resource.get("title", node_id), resource.get("kind", "ui"), f"/user_interfaces/resources/{index}", resource)
        edges.append({"from": "agent", "to": node_id, "label": "presents"})

    lane_order = [
        "Purpose",
        "Teammate",
        "Identity",
        "Runtime",
        "Skills",
        "Workflows",
        "Workflow stages",
        "Capabilities",
        "Integrations",
        "Data",
        "Experience",
        "Governance",
    ]
    lanes = [
        {"id": lane, "nodes": [node["id"] for node in nodes if node["lane"] == lane]}
        for lane in lane_order
        if any(node["lane"] == lane for node in nodes)
    ]
    return {"lanes": lanes, "nodes": nodes, "edges": edges}


def studio_snapshot(store: DraftStore) -> dict[str, Any]:
    session = store.session
    draft: dict[str, Any] = {}
    validation = {"valid": False, "errors": ["No draft has been authored"], "warnings": [], "digest": ""}
    graph = {"lanes": [], "nodes": [], "edges": []}
    if store.draft_file.is_file():
        draft = store.draft()
        validation = store.validate().as_dict()
        graph = project_spec_graph(draft)
    source_manifest = (
        json.loads(store.source_manifest_file.read_text(encoding="utf-8"))
        if store.source_manifest_file.is_file()
        else {}
    )
    source_preview = store.source_file.read_text(encoding="utf-8")[:30000] if store.source_file.is_file() else ""
    confirmation = store.active_confirmation()
    execution = (
        json.loads(store.execution_file.read_text(encoding="utf-8"))
        if store.execution_file.is_file()
        else None
    )
    history = []
    for path in sorted(store.history_dir.glob("*.patch.json"), reverse=True)[:25]:
        history.append(json.loads(path.read_text(encoding="utf-8")))
    digest = str(validation.get("digest", ""))
    return {
        "session": session,
        "source": {"manifest": source_manifest, "preview": source_preview},
        "draft": draft,
        "canonicalSpec": yaml.safe_dump(draft, sort_keys=False, allow_unicode=False) if draft else "",
        "validation": validation,
        "graph": graph,
        "history": history,
        "confirmation": confirmation,
        "execution": execution,
        "confirmationPhrase": f"CONFIRM {digest[:12]}" if digest else "",
    }


def studio_ag_ui_events(store: DraftStore, *, thread_id: str, run_id: str) -> list[dict[str, Any]]:
    snapshot = studio_snapshot(store)
    events = [
        RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id),
        StepStartedEvent(type=EventType.STEP_STARTED, step_name="project_specification"),
        StateSnapshotEvent(type=EventType.STATE_SNAPSHOT, snapshot=snapshot),
        ActivitySnapshotEvent(
            type=EventType.ACTIVITY_SNAPSHOT,
            message_id=f"activity:{store.session['sessionId']}",
            activity_type="SPECIFICATION_REVIEW",
            content={
                "revision": snapshot["session"]["revision"],
                "status": snapshot["session"]["status"],
                "valid": snapshot["validation"]["valid"],
                "errors": snapshot["validation"]["errors"],
                "nodeCount": len(snapshot["graph"]["nodes"]),
            },
        ),
        StepFinishedEvent(type=EventType.STEP_FINISHED, step_name="project_specification"),
    ]
    if snapshot["validation"]["valid"] and not snapshot["confirmation"]:
        events.append(
            RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=thread_id,
                run_id=run_id,
                result=snapshot,
                outcome=RunFinishedInterruptOutcome(
                    interrupts=[
                        Interrupt(
                            id=f"confirm:{snapshot['validation']['digest']}",
                            reason="input_required",
                            message="Review the draft and explicitly confirm before building.",
                            response_schema={
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "action": {"enum": ["scaffold", "scaffold_and_provision"]},
                                    "output_path": {"type": "string"},
                                    "force": {"type": "boolean"},
                                    "tenant_id": {"type": "string"},
                                    "acknowledgement": {"const": snapshot["confirmationPhrase"]},
                                },
                                "required": ["action", "output_path", "acknowledgement"],
                            },
                            metadata={
                                "digest": snapshot["validation"]["digest"],
                                "revision": snapshot["session"]["revision"],
                            },
                        )
                    ]
                ),
            )
        )
    else:
        events.append(
            RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=thread_id,
                run_id=run_id,
                result=snapshot,
                outcome=RunFinishedSuccessOutcome(),
            )
        )
    return [event.model_dump(mode="json", by_alias=True, exclude_none=True) for event in events]


def studio_html() -> str:
    rendered = _HTML.replace(
        "</style>",
        "@media(max-width:760px){html,body{max-width:100%;overflow-x:hidden}.top{align-items:flex-start;flex-direction:column}.tabs{overflow-x:auto;width:100%;white-space:nowrap}.tabs button{flex:0 0 auto}.lane{grid-template-columns:86px minmax(0,1fr)}.lane-nodes{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));overflow:visible}.node{min-width:0;width:100%}.source{max-width:100%;overflow:auto}}"
        "</style>",
    ).replace(
        "document.querySelector('#source').textContent=STATE.source?.preview||'No source extracted';",
        "document.querySelector('#specification').textContent=STATE.canonicalSpec||'No specification authored';"
        "document.querySelector('#source').textContent=STATE.source?.preview||'No source extracted';",
    ).replace(
        "for(const id of ['graph','source','history'])",
        "for(const id of ['graph','specification','source','history'])",
    ).replace(
        "Schema valid",
        "Schema and semantic checks valid",
    )
    confirmation_renderer = r'''function renderConfirm(){const box=document.querySelector('#confirm'),v=STATE.validation||{},c=STATE.confirmation,e=STATE.execution||{};if(c){box.innerHTML=`<h2>${e.status==='checkpoint'?'Administrator checkpoint':'Confirmed build'}</h2><p>Revision <b>${esc(c.revision)}</b> · <b>${esc(c.action)}</b></p><p>Output: <code>${esc(c.outputPath)}</code></p><p>Force overwrite: <b>${c.force?'Yes':'No'}</b>${c.tenantId?` · Tenant: <code>${esc(c.tenantId)}</code>`:''}</p><p><code>${esc(c.digest)}</code></p>${e.status==='checkpoint'?'<p>Complete the displayed A365 administrator action, then ask the agent to resume the same confirmed operation.</p>':''}`;return}box.innerHTML=`<h2>Confirm build stage</h2><p>Revision <b>${esc(STATE.session?.revision||0)}</b></p><p><code>${esc(v.digest||'Draft must be valid')}</code></p><div class="confirm-grid"><label>Action<select id="confirm-action"><option value="scaffold">Scaffold only</option><option value="scaffold_and_provision">Scaffold + A365</option></select></label><label>Force overwrite<select id="confirm-force"><option value="false">No</option><option value="true">Yes</option></select></label><label class="wide">Output path<input id="confirm-output" placeholder="Absolute path for generated solution"></label><label class="wide">Tenant ID (required for A365)<input id="confirm-tenant"></label><label class="wide">Type this exact phrase<input id="confirm-ack" placeholder="${esc(STATE.confirmationPhrase||'Draft must be valid')}"></label><button class="wide" data-confirm ${v.valid?'':'disabled'}>Confirm exact draft</button></div>`}
'''
    return rendered.replace("async function refresh(){", confirmation_renderer + "async function refresh(){")


_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Teammate Spec Studio</title><style>
:root{--ink:#172027;--muted:#5d6970;--line:#d6dddf;--soft:#f4f7f7;--canvas:#eef2f1;--surface:#fff;--brand:#006b5d;--brand2:#004f46;--warn:#8a5207;--warnbg:#fff2d6;--danger:#9d3129;--dangerbg:#fbe9e7;--ok:#246c49;--okbg:#e7f4ec;--font:"Aptos","Segoe UI",sans-serif;color:var(--ink);font-family:var(--font)}*{box-sizing:border-box}body{margin:0;background:var(--canvas);font:13px/1.45 var(--font);letter-spacing:0}.top{min-height:62px;background:var(--surface);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 18px}.brand{font-size:16px;font-weight:700}.brand small{display:block;color:var(--muted);font-size:11px;font-weight:400}.state{border-radius:14px;padding:4px 10px;background:var(--soft);font-size:11px;font-weight:700}.state.valid{background:var(--okbg);color:var(--ok)}.state.invalid{background:var(--dangerbg);color:var(--danger)}main{display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,34%);min-height:640px}.workspace{min-width:0;padding:16px}.inspector{border-left:1px solid var(--line);background:var(--surface);padding:16px;overflow:auto}.metrics{display:grid;grid-template-columns:repeat(4,minmax(100px,1fr));gap:8px;margin-bottom:12px}.metric{border:1px solid var(--line);background:var(--surface);padding:10px;border-radius:6px}.metric b{display:block;font-size:20px}.metric span{font-size:10px;color:var(--muted)}.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:12px}.tabs button{border:0;border-bottom:3px solid transparent;background:transparent;color:var(--muted);padding:8px 10px}.tabs button.active{color:var(--brand);border-bottom-color:var(--brand)}button{min-height:34px;border:1px solid var(--brand);border-radius:5px;background:var(--brand);color:white;padding:6px 11px;font-weight:650;cursor:pointer}button:hover{background:var(--brand2)}button.secondary{background:white;border-color:var(--line);color:var(--ink)}button.danger{background:var(--dangerbg);border-color:#e6b8b3;color:var(--danger)}button:disabled{opacity:.5;cursor:not-allowed}.lane{display:grid;grid-template-columns:120px minmax(0,1fr);gap:10px;margin-bottom:8px}.lane-title{font-size:10px;text-transform:uppercase;color:var(--muted);font-weight:800;padding-top:9px}.lane-nodes{display:flex;gap:7px;overflow:auto;padding:2px 2px 8px}.node{min-width:150px;max-width:220px;text-align:left;background:var(--surface);color:var(--ink);border:1px solid var(--line);padding:9px}.node.active{border-color:var(--brand);box-shadow:0 0 0 2px rgba(0,107,93,.13)}.node b{display:block;font-size:12px}.node span{display:block;margin-top:3px;color:var(--muted);font-size:10px}.panel{border:1px solid var(--line);border-radius:6px;background:var(--surface);padding:12px;margin-bottom:12px}.panel h2{font-size:14px;margin:0 0 9px}.errors{margin:0;padding-left:18px;color:var(--danger)}.warnings{margin:0;padding-left:18px;color:var(--warn)}label{display:grid;gap:4px;margin-bottom:9px;font-size:11px;font-weight:700}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:5px;padding:7px 8px;font:13px/1.4 var(--font);color:var(--ink);background:white}textarea{min-height:90px;resize:vertical}.field-row{display:grid;grid-template-columns:1fr auto;gap:7px;align-items:end}.field-row button{height:34px}.source{max-height:360px;overflow:auto;white-space:pre-wrap;background:#f8fafa;border:1px solid var(--line);padding:10px}.history{display:grid;gap:6px}.history-item{border-bottom:1px solid var(--line);padding-bottom:6px}.history-item b{font-size:11px}.history-item span{display:block;color:var(--muted);font-size:10px}.confirm-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.wide{grid-column:1/-1}.actions{display:flex;gap:7px;flex-wrap:wrap}.hidden{display:none!important}.empty{padding:28px;text-align:center;color:var(--muted)}code{font-family:"Cascadia Code",Consolas,monospace;font-size:11px;overflow-wrap:anywhere}@media(max-width:820px){main{grid-template-columns:1fr}.inspector{border-left:0;border-top:1px solid var(--line)}.metrics{grid-template-columns:repeat(2,1fr)}.lane{grid-template-columns:1fr}.confirm-grid{grid-template-columns:1fr}.wide{grid-column:auto}}</style></head>
<body><header class="top"><div class="brand">AI Teammate Spec Studio<small>Review the exact contract before any scaffold or tenant action</small></div><span id="state" class="state">Loading</span></header><main><section class="workspace"><div id="metrics" class="metrics"></div><div class="tabs"><button data-tab="graph" class="active">Architecture</button><button data-tab="specification">Specification</button><button data-tab="source">Requirements</button><button data-tab="history">History</button></div><div id="graph"></div><pre id="specification" class="source hidden"></pre><pre id="source" class="source hidden"></pre><div id="history" class="history hidden"></div></section><aside class="inspector"><div id="validation" class="panel"></div><div id="editor" class="panel"><h2>Inspector</h2><div class="empty">Select a graph node</div></div><div class="panel"><h2>Add or change any value</h2><label>JSON Pointer<input id="patch-path" placeholder="/solution/description"></label><label>Value (JSON)<textarea id="patch-value" placeholder='"A revised description"'></textarea></label><div class="actions"><button data-patch="replace">Replace</button><button data-patch="add" class="secondary">Add</button><button data-patch="remove" class="danger">Remove</button></div></div><div id="confirm" class="panel"></div></aside></main><script>
let seq=0,STATE=null,SELECTED=null;const pending=new Map();const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function request(method,params){const id=`studio-${Date.now()}-${++seq}`;parent.postMessage({jsonrpc:'2.0',id,method,params},'*');return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))}function call(name,args){return request('tools/call',{name,arguments:args})}function unpack(v){v=v?.structuredContent??v?.structured_content??v;if(v?.content){const text=v.content.find(x=>x.type==='text')?.text;if(text){try{v=JSON.parse(text)}catch(e){}}}if(typeof v==='string'){try{v=JSON.parse(v)}catch(e){}}return v?.data??v}addEventListener('message',event=>{if(event.source!==parent)return;const m=event.data||{};if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(new Error(m.error.message||'Tool error')):p.resolve(unpack(m.result));return}if(m.method==='ui/notifications/tool-result'){STATE=unpack(m.params?.result??m.params);render()}});
function metric(name,value){return`<div class="metric"><b>${esc(value)}</b><span>${esc(name)}</span></div>`}function render(){if(!STATE)return;const s=STATE.session||{},v=STATE.validation||{},g=STATE.graph||{};document.querySelector('#state').textContent=v.valid?(STATE.confirmation?'Confirmed':'Ready for confirmation'):'Draft needs work';document.querySelector('#state').className=`state ${v.valid?'valid':'invalid'}`;document.querySelector('#metrics').innerHTML=[metric('Revision',s.revision||0),metric('Graph nodes',(g.nodes||[]).length),metric('Errors',(v.errors||[]).length),metric('Warnings',(v.warnings||[]).length)].join('');document.querySelector('#graph').innerHTML=(g.lanes||[]).map(lane=>`<div class="lane"><div class="lane-title">${esc(lane.id)}</div><div class="lane-nodes">${lane.nodes.map(id=>{const n=g.nodes.find(x=>x.id===id);return`<button class="node ${SELECTED===id?'active':''}" data-node="${esc(id)}"><b>${esc(n?.title)}</b><span>${esc(n?.kind)}</span></button>`}).join('')}</div></div>`).join('')||'<div class="empty">The agent has not authored a draft yet.</div>';document.querySelector('#source').textContent=STATE.source?.preview||'No source extracted';document.querySelector('#history').innerHTML=(STATE.history||[]).map(h=>`<div class="history-item"><b>Revision ${esc(h.sequence)} · ${esc(h.channel)} · ${esc(h.actor)}</b><span>${esc(h.timestamp)} · ${esc((h.patch||[]).length)} change(s)</span></div>`).join('')||'<div class="empty">No revisions yet</div>';document.querySelector('#validation').innerHTML=`<h2>Validation</h2>${v.valid?'<div class="state valid">Schema valid</div>':`<ul class="errors">${(v.errors||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`}${(v.warnings||[]).length?`<ul class="warnings">${v.warnings.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}<p><code>${esc(v.digest||'No digest')}</code></p>`;renderEditor();renderConfirm()}
function renderEditor(){const box=document.querySelector('#editor'),node=(STATE.graph?.nodes||[]).find(x=>x.id===SELECTED);if(!node){box.innerHTML='<h2>Inspector</h2><div class="empty">Select a graph node</div>';return}const value=node.value||{};const fields=Object.entries(value).filter(([,v])=>['string','number','boolean'].includes(typeof v));box.innerHTML=`<h2>${esc(node.title)}</h2><p><code>${esc(node.pointer)}</code></p>${fields.map(([k,v])=>`<div class="field-row"><label>${esc(k)}<input data-field="${esc(k)}" value="${esc(v)}"></label><button data-save-field="${esc(k)}">Save</button></div>`).join('')||`<label>Object (JSON)<textarea id="node-json">${esc(JSON.stringify(value,null,2))}</textarea></label><button data-save-object>Replace object</button>`}`}
function renderConfirm(){const box=document.querySelector('#confirm'),v=STATE.validation||{};if(STATE.confirmation){box.innerHTML=`<h2>Confirmed</h2><p>Authorized: <b>${esc(STATE.confirmation.action)}</b></p><p><code>${esc(STATE.confirmation.digest)}</code></p>`;return}box.innerHTML=`<h2>Confirm build stage</h2><div class="confirm-grid"><label>Action<select id="confirm-action"><option value="scaffold">Scaffold only</option><option value="scaffold_and_provision">Scaffold + A365</option></select></label><label>Force overwrite<select id="confirm-force"><option value="false">No</option><option value="true">Yes</option></select></label><label class="wide">Output path<input id="confirm-output" placeholder="Absolute path for generated solution"></label><label class="wide">Tenant ID (required for A365)<input id="confirm-tenant"></label><label class="wide">Type this exact phrase<input id="confirm-ack" placeholder="${esc(STATE.confirmationPhrase||'Draft must be valid')}"></label><button class="wide" data-confirm ${v.valid?'':'disabled'}>Confirm exact draft</button></div>`}
async function refresh(){STATE=unpack(await call('studio_get_state',{session_id:STATE?.session?.sessionId||''}));render()}async function patch(operations){const result=unpack(await call('studio_patch',{session_id:STATE.session.sessionId,base_revision:STATE.session.revision,operations}));STATE=result;render()}document.addEventListener('click',async e=>{const t=e.target.closest('button');if(!t)return;try{if(t.dataset.tab){document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x===t));for(const id of ['graph','source','history'])document.querySelector(`#${id}`).classList.toggle('hidden',id!==t.dataset.tab)}else if(t.dataset.node){SELECTED=t.dataset.node;render()}else if(t.dataset.saveField){const node=STATE.graph.nodes.find(x=>x.id===SELECTED),input=document.querySelector(`[data-field="${CSS.escape(t.dataset.saveField)}"]`);let value=input.value;const old=node.value[t.dataset.saveField];if(typeof old==='number')value=Number(value);if(typeof old==='boolean')value=value==='true';await patch([{op:'replace',path:`${node.pointer}/${t.dataset.saveField.replace(/~/g,'~0').replace(/\//g,'~1')}`,value}])}else if(t.dataset.saveObject!==undefined){const node=STATE.graph.nodes.find(x=>x.id===SELECTED);await patch([{op:'replace',path:node.pointer,value:JSON.parse(document.querySelector('#node-json').value)}])}else if(t.dataset.patch){const path=document.querySelector('#patch-path').value.trim();const operation={op:t.dataset.patch,path};if(operation.op!=='remove')operation.value=JSON.parse(document.querySelector('#patch-value').value);await patch([operation])}else if(t.dataset.confirm!==undefined){STATE=unpack(await call('studio_confirm',{session_id:STATE.session.sessionId,action:document.querySelector('#confirm-action').value,output_path:document.querySelector('#confirm-output').value,force:document.querySelector('#confirm-force').value==='true',tenant_id:document.querySelector('#confirm-tenant').value,acknowledgement:document.querySelector('#confirm-ack').value}));render()}}catch(error){alert(error.message||error)}});async function initialize(){await request('ui/initialize',{protocolVersion:'2026-01-26',clientInfo:{name:'ai-teammate-spec-studio',version:'1.0.0'},appCapabilities:{availableDisplayModes:['inline','fullscreen']}});parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized',params:{}},'*')}initialize().catch(error=>alert(error.message||error));
</script></body></html>'''
