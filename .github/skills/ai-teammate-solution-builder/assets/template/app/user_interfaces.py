"""Spec-driven MCP Apps projections shared with the Teams control plane."""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .data import DataCatalog
from .spec import SolutionSpec, UiResource
from .state import StateStore


class UiQuery(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)
    sort_field: str = ""
    sort_direction: Literal["asc", "desc"] | None = None
    page: int = Field(default=1, ge=1)
    page_size: int | None = Field(default=None, ge=1, le=200)


class ReviewResolution(BaseModel):
    review_id: str
    expected_digest: str
    decision: Literal["approve", "edit", "reject", "defer"]
    final: dict[str, Any] = Field(default_factory=dict)


class ResolveReviewsInput(BaseModel):
    decisions: list[ReviewResolution] = Field(min_length=1, max_length=100)


class UserInterfaceService:
    def __init__(self, spec: SolutionSpec, data: DataCatalog, state: StateStore):
        self.spec = spec
        self.data = data
        self.state = state
        self.resources = {item.id: item for item in spec.user_interfaces.resources}
        self.by_tool = {item.tool_name: item for item in spec.user_interfaces.resources}
        self.workflows = {item.id: item for item in spec.workflows}

    def resource_for_tool(self, tool_name: str) -> UiResource | None:
        return self.by_tool.get(tool_name)

    def available(self, manager_id: str | None, roles: set[str]) -> list[dict[str, Any]]:
        result = []
        for resource in self.resources.values():
            try:
                self._authorize(resource, manager_id, roles)
            except PermissionError:
                continue
            result.append(resource.model_dump(mode="json"))
        return result

    def snapshot(
        self,
        resource_id: str,
        manager_id: str | None,
        roles: set[str],
        query: UiQuery | None = None,
    ) -> dict[str, Any]:
        resource = self.resources.get(resource_id)
        if resource is None:
            raise KeyError(f"Unknown UI resource {resource_id}")
        self._authorize(resource, manager_id, roles)
        query = query or UiQuery()
        runs = self.state.list_runs(manager_id if resource.audience == "manager" else None)
        reviews = self.state.list_reviews(
            manager_id if resource.audience == "manager" else None,
            pending_only=True,
        )
        if resource.source == "review_queue":
            by_run = {run["id"]: run for run in runs}
            items = [self._review_row(review, by_run.get(review["runId"])) for review in reviews]
        else:
            items = [self._run_row(run) for run in runs]
        metrics = self._metrics(runs, reviews, manager_id, resource.metrics)
        items = self._filter(resource, items, query.filters)
        sort_field = query.sort_field or resource.default_sort.field
        if sort_field not in set(resource.columns) | set(resource.filters):
            raise ValueError(f"Unsupported sort field {sort_field!r} for {resource.id}")
        direction = query.sort_direction or resource.default_sort.direction
        items.sort(key=lambda item: _sort_value(item.get(sort_field)), reverse=direction == "desc")
        page_size = min(query.page_size or resource.page_size, resource.page_size)
        total = len(items)
        start = (query.page - 1) * page_size
        page_items = items[start : start + page_size]
        return {
            "resource": resource.model_dump(mode="json"),
            "metrics": metrics,
            "items": page_items,
            "total": total,
            "page": query.page,
            "pageSize": page_size,
            "hasMore": start + page_size < total,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "provenance": self.state.provenance,
        }

    def interrupt_id(
        self, resource_id: str, manager_id: str, thread_id: str, items: list[dict[str, Any]]
    ) -> str:
        del items
        canonical = json.dumps(
            {
                "resource": resource_id,
                "manager": manager_id,
                "thread": thread_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"review_batch_{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"

    @staticmethod
    def review_digest(review: dict[str, Any]) -> str:
        proposed = review.get("context", {}).get("proposedEffect") or {}
        if proposed.get("digest"):
            return str(proposed["digest"])
        canonical = json.dumps(
            {
                "reviewId": review["id"],
                "runId": review["runId"],
                "managerId": review["managerId"],
                "context": review.get("context", {}),
                "decisions": review.get("decisions", []),
            },
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _authorize(
        self, resource: UiResource, manager_id: str | None, roles: set[str]
    ) -> None:
        if resource.audience == "manager":
            if not manager_id or manager_id not in {item.id for item in self.spec.managers}:
                raise PermissionError("Manager UI requires the assigned manager")
            return
        if not roles.intersection(self.spec.identity.fleet_roles):
            raise PermissionError("Fleet UI requires a configured fleet role")

    def _run_row(self, run: dict[str, Any]) -> dict[str, Any]:
        workflow = self.workflows[run["workflowId"]]
        results = run.get("results", [])
        finished = sum(item.get("status") in {"done", "skipped"} for item in results)
        progress = 100 if run["status"] == "complete" else round(100 * finished / len(workflow.stages))
        completed_ids = {item.get("stageId") for item in results}
        next_stage = next((stage.title for stage in workflow.stages if stage.id not in completed_ids), "Complete")
        current = results[-1].get("title", next_stage) if results else next_stage
        return {
            "id": run["id"],
            "workflowId": run["workflowId"],
            "workflowTitle": workflow.title,
            "managerId": run["managerId"],
            "subjectId": run["subjectId"],
            "status": run["status"],
            "progress": progress,
            "currentStage": current,
            "createdAt": run["createdAt"],
            "updatedAt": run["updatedAt"],
        }

    def _review_row(
        self, review: dict[str, Any], run: dict[str, Any] | None
    ) -> dict[str, Any]:
        proposed = review.get("context", {}).get("proposedEffect") or {}
        decisions = review.get("decisions", [])
        if not proposed:
            decisions = [decision for decision in decisions if decision != "edit"]
        workflow_id = run["workflowId"] if run else ""
        workflow = self.workflows.get(workflow_id)
        return {
            "id": review["id"],
            "runId": review["runId"],
            "workflowId": workflow_id,
            "workflowTitle": workflow.title if workflow else workflow_id,
            "managerId": review["managerId"],
            "subjectId": run["subjectId"] if run else "",
            "capabilityId": proposed.get("capabilityId", ""),
            "proposedEffect": proposed.get("arguments", {}),
            "status": review["status"],
            "digest": self.review_digest(review),
            "decisions": decisions,
            "createdAt": review["createdAt"],
            "updatedAt": review["updatedAt"],
        }

    def _metrics(
        self,
        runs: list[dict[str, Any]],
        reviews: list[dict[str, Any]],
        manager_id: str | None,
        requested: list[str],
    ) -> dict[str, Any]:
        completed = sum(run["status"] == "complete" for run in runs)
        all_values = {
            "total_runs": len(runs),
            "active_runs": sum(run["status"] == "running" for run in runs),
            "completed_runs": completed,
            "failed_runs": sum(run["status"] == "failed" for run in runs),
            "pending_reviews": len(reviews),
            "completion_rate": round(100 * completed / len(runs)) if runs else 0,
            "managers": len(self.spec.managers),
            "subjects": (
                len(self.data.manager_subjects(manager_id))
                if manager_id
                else len(self.data.all_subjects())
            ),
        }
        return {name: all_values[name] for name in requested}

    @staticmethod
    def _filter(
        resource: UiResource, items: list[dict[str, Any]], filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        unsupported = set(filters) - set(resource.filters)
        if unsupported:
            raise ValueError(f"Unsupported filters for {resource.id}: {sorted(unsupported)}")
        active = {key: value for key, value in filters.items() if str(value).strip()}
        if not active:
            return items
        return [
            item
            for item in items
            if all(str(expected).lower() in str(item.get(key, "")).lower() for key, expected in active.items())
        ]


def mcp_app_html(resource: UiResource) -> str:
    config = json.dumps(resource.model_dump(mode="json"), separators=(",", ":")).replace("<", "\\u003c")
    title = html.escape(resource.title)
    return _MCP_APP.replace("__TITLE__", title).replace("__CONFIG__", config)


def _sort_value(value: Any) -> tuple[int, str]:
    if value is None:
        return (1, "")
    if isinstance(value, (int, float)):
        return (0, f"{value:020.6f}")
    return (0, str(value).lower())


_MCP_APP = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
:root{--ink:#18232b;--muted:#607078;--line:#d9e0e2;--soft:#f5f8f8;--brand:#0b625b;--brand2:#084a45;--warn:#8b5309;--warnbg:#fff4dd;--danger:#a33a31;--dangerbg:#fcecea;--ok:#26734d;--okbg:#e9f5ee;font-family:"Aptos","Segoe UI",sans-serif;color:var(--ink)}*{box-sizing:border-box}body{margin:0;background:#fff;font-size:13px;line-height:1.4}.shell{padding:14px}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px}.metric{border:1px solid var(--line);border-radius:6px;padding:11px;background:var(--soft)}.metric b{display:block;font-size:21px}.metric span{color:var(--muted);font-size:11px}.toolbar{display:flex;align-items:end;gap:8px;flex-wrap:wrap;margin:0 0 10px}.filter{display:grid;gap:3px;min-width:130px}.filter span{font-size:10px;font-weight:700;color:var(--muted)}input{height:34px;border:1px solid var(--line);border-radius:5px;padding:5px 8px}button{height:34px;border:1px solid var(--brand);border-radius:5px;background:var(--brand);color:#fff;padding:5px 10px;font-weight:650;cursor:pointer}button:hover{background:var(--brand2)}button.secondary{border-color:var(--line);background:#fff;color:var(--ink)}button.danger{border-color:#e7b8b4;background:var(--dangerbg);color:var(--danger)}button:disabled{opacity:.5;cursor:not-allowed}.bulk{display:flex;gap:6px;margin-left:auto}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:6px}table{width:100%;border-collapse:collapse;min-width:640px}th,td{padding:9px 10px;text-align:left;border-bottom:1px solid #e8eded;vertical-align:top}th{background:var(--soft);font-size:10px;color:var(--muted);white-space:nowrap}th button{height:auto;border:0;background:transparent;color:inherit;padding:0;font-size:10px}td code{display:block;max-width:420px;white-space:pre-wrap;overflow-wrap:anywhere}.status{display:inline-block;border-radius:12px;background:var(--soft);padding:2px 7px;font-size:10px;font-weight:700}.status.pending_review,.status.pending{background:var(--warnbg);color:var(--warn)}.status.complete,.status.approved{background:var(--okbg);color:var(--ok)}.status.failed,.status.rejected{background:var(--dangerbg);color:var(--danger)}.row-actions{display:flex;gap:5px;flex-wrap:wrap}.row-actions button{height:28px;font-size:11px;padding:3px 7px}.empty{padding:28px;text-align:center;color:var(--muted)}.footer{display:flex;justify-content:space-between;align-items:center;color:var(--muted);padding-top:9px;font-size:11px}.error{padding:12px;background:var(--dangerbg);color:var(--danger);border-radius:6px}@media(max-width:600px){.shell{padding:10px}.bulk{width:100%;margin:0}.table-wrap{border:0}table,tbody,tr,td{display:block;min-width:0}thead{display:none}tr{border:1px solid var(--line);border-radius:6px;margin-bottom:8px;padding:7px}td{border:0;padding:4px}.metrics{grid-template-columns:repeat(2,1fr)}}</style></head>
<body><main class="shell"><div id="content"><div class="empty">Loading __TITLE__</div></div></main><script>
const CONFIG=__CONFIG__;let SNAPSHOT=null,SORT={field:CONFIG.default_sort.field,direction:CONFIG.default_sort.direction},SELECTED=new Set(),seq=0;const pending=new Map();
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const label=v=>String(v??'').replace(/([a-z0-9])([A-Z])/g,'$1 $2').replace(/[_-]+/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
function notify(method,params={}){window.parent.postMessage({jsonrpc:'2.0',method,params},'*')}function request(method,params){const id=`ui-${Date.now()}-${++seq}`;window.parent.postMessage({jsonrpc:'2.0',id,method,params},'*');return new Promise((resolve,reject)=>pending.set(id,{resolve,reject}))}function call(name,args){return request('tools/call',{name,arguments:args})}
function unpack(value){let v=value?.structuredContent??value?.structured_content??value;if(v?.content&&Array.isArray(v.content)){const t=v.content.find(x=>x.type==='text')?.text;if(t){try{v=JSON.parse(t)}catch(e){}}}if(typeof v==='string'){try{v=JSON.parse(v)}catch(e){}}return v?.data??v}
window.addEventListener('message',event=>{if(event.source!==window.parent)return;const m=event.data||{};if(m.id&&pending.has(m.id)){const p=pending.get(m.id);pending.delete(m.id);m.error?p.reject(new Error(m.error.message||'Tool call failed')):p.resolve(unpack(m.result));return}if(m.method==='ui/notifications/tool-result'){SNAPSHOT=unpack(m.params?.result??m.params);SELECTED.clear();render()}});
function args(){const filters={};document.querySelectorAll('[data-filter]').forEach(x=>{if(x.value.trim())filters[x.dataset.filter]=x.value.trim()});return{filters,sort_field:SORT.field,sort_direction:SORT.direction,page:1,page_size:CONFIG.page_size}}
async function refresh(){try{SNAPSHOT=unpack(await call(CONFIG.tool_name,args()));SELECTED.clear();render()}catch(e){error(e)}}
function metric(k,v){return`<div class="metric"><b>${esc(v)}${k==='completion_rate'?'%':''}</b><span>${esc(label(k))}</span></div>`}function cell(v){if(v&&typeof v==='object')return`<code>${esc(JSON.stringify(v,null,2))}</code>`;return esc(v??'—')}function status(v){return`<span class="status ${esc(v)}">${esc(label(v))}</span>`}
function render(){if(!SNAPSHOT){return}const metrics=Object.entries(SNAPSHOT.metrics||{}).map(([k,v])=>metric(k,v)).join('');const filters=CONFIG.filters.map(f=>`<label class="filter"><span>${esc(label(f))}</span><input data-filter="${esc(f)}" value=""></label>`).join('');const bulk=CONFIG.kind==='hitl'?`<div class="bulk">${CONFIG.bulk_actions.map(a=>`<button class="${a==='reject'?'danger':'secondary'}" data-bulk="${esc(a)}" ${SELECTED.size?'':'disabled'}>${esc(label(a))} (${SELECTED.size})</button>`).join('')}</div>`:'';const selectHead=CONFIG.kind==='hitl'?'<th><input type="checkbox" data-all aria-label="Select all"></th>':'';const heads=CONFIG.columns.map(c=>`<th><button data-sort="${esc(c)}">${esc(label(c))}${SORT.field===c?(SORT.direction==='asc'?' ↑':' ↓'):''}</button></th>`).join('');const actionHead=CONFIG.kind==='hitl'?'<th>Actions</th>':'';const rows=(SNAPSHOT.items||[]).map(item=>{const select=CONFIG.kind==='hitl'?`<td><input type="checkbox" data-select="${esc(item.id)}" ${SELECTED.has(item.id)?'checked':''}></td>`:'';const values=CONFIG.columns.map(c=>`<td>${c==='status'?status(item[c]):cell(item[c])}</td>`).join('');const actions=CONFIG.kind==='hitl'?`<td><div class="row-actions">${(item.decisions||[]).map(a=>`<button class="${a==='reject'?'danger':'secondary'}" data-row="${esc(item.id)}" data-decision="${esc(a)}">${esc(label(a))}</button>`).join('')}</div></td>`:'';return`<tr>${select}${values}${actions}</tr>`}).join('');const table=rows?`<div class="table-wrap"><table><thead><tr>${selectHead}${heads}${actionHead}</tr></thead><tbody>${rows}</tbody></table></div>`:'<div class="empty">No matching records</div>';document.querySelector('#content').innerHTML=`<div class="metrics">${metrics}</div><div class="toolbar">${filters}<button data-apply>Apply</button>${bulk}</div>${table}<div class="footer"><span>${esc(SNAPSHOT.total||0)} records</span><span>${esc(SNAPSHOT.provenance||'')}</span></div>`;notify('ui/notifications/size-changed',{height:document.documentElement.scrollHeight})}
function review(item,decision){let final={};if(decision==='approve')final=item.proposedEffect||{};if(decision==='edit'){const edited=prompt('Edit the full approved effect JSON',JSON.stringify(item.proposedEffect||{},null,2));if(edited===null)return null;try{final=JSON.parse(edited)}catch(e){alert('Edited effect must be valid JSON');return null}}return{review_id:item.id,expected_digest:item.digest,decision,final}}
async function decide(ids,decision){const decisions=(SNAPSHOT.items||[]).filter(x=>ids.has(x.id)).map(x=>review(x,decision)).filter(Boolean);if(!decisions.length)return;try{await call('resolve_reviews',{decisions,idempotency_key:crypto.randomUUID()});await refresh()}catch(e){error(e)}}function error(e){document.querySelector('#content').innerHTML=`<div class="error">${esc(e.message||e)}</div>`}
document.addEventListener('click',e=>{const t=e.target.closest('button');if(!t)return;if(t.dataset.apply!==undefined)refresh();if(t.dataset.sort){SORT={field:t.dataset.sort,direction:SORT.field===t.dataset.sort&&SORT.direction==='asc'?'desc':'asc'};refresh()}if(t.dataset.bulk)decide(new Set(SELECTED),t.dataset.bulk);if(t.dataset.row){const item=(SNAPSHOT.items||[]).find(x=>x.id===t.dataset.row);if(item)decide(new Set([item.id]),t.dataset.decision)}});document.addEventListener('change',e=>{const t=e.target;if(t.dataset.select){t.checked?SELECTED.add(t.dataset.select):SELECTED.delete(t.dataset.select);render()}if(t.dataset.all!==undefined){SELECTED=new Set(t.checked?(SNAPSHOT.items||[]).map(x=>x.id):[]);render()}});
async function initialize(){await request('ui/initialize',{protocolVersion:'2026-01-26',clientInfo:{name:'ai-teammate-generated-ui',version:'1.0.0'},appCapabilities:{availableDisplayModes:['inline','fullscreen']}});notify('ui/notifications/initialized',{})}initialize().catch(error);
</script></body></html>'''
