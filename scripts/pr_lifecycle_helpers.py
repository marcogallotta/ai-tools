"""Durable lifecycle helper facade plus manual Worker attempt recovery."""
from pr_lifecycle_helpers_base import *
from pr_lifecycle_helpers_base import (_continuation_handoff_present,_continuation_key,_handoff_key,_handoff_present,_integration_order_reason,_lease_json,_marker_fields,_mergeability_reason,_notice_key,_notice_present,_parse_time,_pr_base,_pr_branch,_pr_number,_pr_title,_pr_url,_reviewed_head,_utcnow)
from pr_lifecycle_external_replay import latest_external_dependency_record, resolve_external_dependency, resolve_external_dependency as parse_external_dependency
from pr_lifecycle_owner import owning_task_identity_from_pr, owning_task_identity_from_references, task_ids_from_pr
from pr_implementation_provenance import implementation_host_witness

from dataclasses import dataclass
import hashlib, json, re
from typing import Any, Mapping
from pr_lifecycle_support import FULL_SHA_RE, LifecycleError, WORKSPACE_RUNS_BETA, WorkspaceAgentDispatcher as _BaseWorkspaceAgentDispatcher

WORKER_ATTEMPT_MARKER="dish-worker-attempt:v1"; WORKER_AUTHORSHIP_MARKER="dish-worker-authorship:v1"
_WORKER_MODES={"Implementation","Code Review","Design Review","Audit"}
_WORKER_LATE={"resume_adopt","semantic_publication","draft_to_review_ready","final_handoff","code_review_verdict","design_review_verdict","override_sensitive"}
_ATTEMPT_RE=re.compile(r"<!--\s*dish-worker-attempt:v1\s+(\{.*?\})\s*-->",re.S)
_AUTHOR_RE=re.compile(r"<!--\s*dish-worker-authorship:v1\s+(\{.*?\})\s*-->",re.S)

@dataclass(frozen=True)
class WorkerAttempt:
    assignment_digest:str; attempt_id:str; generation:int; mode:str; state:str
    @property
    def accepted(self): return self.state=="accepted"

@dataclass(frozen=True)
class WorkerDispatchResult:
    idempotency_key:str; accepted:bool; status_code:int; attempt_id:str; generation:int; assignment_digest:str; mode:str
    conversation_url:str|None=None; run_id:str|None=None

@dataclass(frozen=True)
class WorkerLateActionDecision:
    allowed:bool; reason:str

def _canon(v): return json.dumps(dict(v),sort_keys=True,separators=(",",":"),ensure_ascii=False)
def _marker(name,v): return f"<!-- {name} {_canon(v)} -->"
def _exact_context(raw):
    if not isinstance(raw,Mapping) or not raw: raise LifecycleError("Worker dispatch requires exact durable context")
    v=dict(raw); task=str(v.get("task") or "").strip()
    if not task: raise LifecycleError("Worker durable context requires owning task")
    v["task"]=task
    if v.get("pr") is not None:
        try: pr=int(v["pr"])
        except (TypeError,ValueError) as e: raise LifecycleError("Worker code context requires numeric PR") from e
        branch=str(v.get("branch") or "").strip(); head=str(v.get("head") or "").strip().lower()
        if pr<=0 or not branch or FULL_SHA_RE.fullmatch(head) is None: raise LifecycleError("Worker code context requires exact PR, branch, and 40-hex head")
        v.update(pr=pr,branch=branch,head=head); return v
    rev=str(v.get("design_revision") or "").strip(); digest=str(v.get("design_digest") or "").strip().lower()
    if not rev or re.fullmatch(r"[0-9a-f]{64}",digest) is None: raise LifecycleError("Worker design context requires exact revision and SHA-256 digest")
    v.update(design_revision=rev,design_digest=digest); return v

def worker_assignment_digest(exact_context):
    return hashlib.sha256(_canon({"schema":"dish-worker-assignment:v1","context":_exact_context(exact_context)}).encode()).hexdigest()
def _attempt_id(digest,generation): return "wa-"+hashlib.sha256(f"{digest}:{generation}".encode()).hexdigest()[:24]
def _records(surface,sid):
    if hasattr(surface,"get_comments"): return [dict(x) for x in surface.get_comments(sid)]
    if hasattr(surface,"get_stories"): return [{"body":str(x.get("text") or ""),**dict(x)} for x in surface.get_stories(str(sid))]
    raise LifecycleError("Worker durable surface has no discussion read")
def _payloads(records,pattern):
    out=[]
    for item in records:
        for m in pattern.finditer(str(item.get("body") or "")):
            try: v=json.loads(m.group(1))
            except json.JSONDecodeError as e: raise LifecycleError("Worker durable marker contains invalid JSON") from e
            if not isinstance(v,dict): raise LifecycleError("Worker durable marker payload must be an object")
            out.append(v)
    return out
def _ensure(surface,sid,body):
    if body not in [str(x.get("body") or "") for x in _records(surface,sid)]: surface.add_comment(sid,body)
    if body not in [str(x.get("body") or "") for x in _records(surface,sid)]: raise LifecycleError("Worker durable marker write did not survive authoritative readback")

def recover_worker_attempt(records,assignment_digest):
    vals=[v for v in _payloads(records,_ATTEMPT_RE) if str(v.get("assignment_digest") or "")==assignment_digest]
    if not vals: return None
    parsed=[]
    for v in vals:
        try: gen=int(v.get("generation"))
        except (TypeError,ValueError) as e: raise LifecycleError("Worker attempt generation must be positive") from e
        aid=str(v.get("attempt_id") or ""); mode=str(v.get("mode") or ""); state=str(v.get("state") or "")
        if gen<=0 or aid!=_attempt_id(assignment_digest,gen) or mode not in _WORKER_MODES or state not in {"issued","accepted"}: raise LifecycleError("Worker attempt marker is invalid")
        parsed.append(WorkerAttempt(assignment_digest,aid,gen,mode,state))
    gen=max(x.generation for x in parsed); cur=[x for x in parsed if x.generation==gen]
    if len({x.attempt_id for x in cur})!=1: raise LifecycleError("Worker generation has conflicting attempt identities")
    accepted=[x for x in cur if x.accepted]; return accepted[-1] if accepted else cur[-1]

def _attempt_record(a,mode,state):
    return _marker(WORKER_ATTEMPT_MARKER,{"assignment_digest":a.assignment_digest,"attempt_id":a.attempt_id,"generation":a.generation,"mode":mode,"state":state})
def _prepare(surface,sid,digest,mode,replacement):
    cur=recover_worker_attempt(_records(surface,sid),digest)
    gen=(cur.generation+1) if cur and replacement else (cur.generation if cur else 1)
    a=WorkerAttempt(digest,_attempt_id(digest,gen),gen,mode,"issued")
    _ensure(surface,sid,_attempt_record(a,mode,"issued")); return a

def worker_material_authors(records,candidate):
    vals=[v for v in _payloads(records,_AUTHOR_RE) if str(v.get("candidate") or "")==candidate]
    prior:set[str]=set()
    for v in vals:
        authors=set(map(str,v.get("authors") or []))
        if not prior.issubset(authors): raise LifecycleError("Worker cumulative authorship record erased a prior material author")
        prior=authors
    return prior

def record_worker_authorship(surface,sid,candidate,attempt,prior_candidate=None):
    records=_records(surface,sid); authors=worker_material_authors(records,candidate)
    if prior_candidate: authors |= worker_material_authors(records,prior_candidate)
    authors.add(attempt.attempt_id)
    body=_marker(WORKER_AUTHORSHIP_MARKER,{"candidate":candidate,"authors":sorted(authors)})
    _ensure(surface,sid,body); return authors

def establish_worker_authorship_baseline(surface,sid,candidate):
    if any(v.get("candidate")==candidate for v in _payloads(_records(surface,sid),_AUTHOR_RE)): return
    _ensure(surface,sid,_marker(WORKER_AUTHORSHIP_MARKER,{"candidate":candidate,"authors":[]}))
def assert_worker_review_independent(records,candidate,attempt):
    matching=[v for v in _payloads(records,_AUTHOR_RE) if str(v.get("candidate") or "")==candidate]
    if not matching: raise LifecycleError("Worker review independence cannot be proven: cumulative authorship is missing")
    if attempt.attempt_id in worker_material_authors(records,candidate): raise LifecycleError("Worker attempt materially authored this candidate and cannot independently Review it")

def qualify_worker_late_action(action,*,mode,attempt_accepted,identity_current,write_fence_verified=False,authoritative_readback=False,independent=None,scoped_override=False,fresh_packet_loaded=False):
    del fresh_packet_loaded
    if action not in _WORKER_LATE: raise LifecycleError(f"unknown Worker late action: {action}")
    if mode not in _WORKER_MODES: return WorkerLateActionDecision(False,"current Worker mode is invalid")
    if not attempt_accepted: return WorkerLateActionDecision(False,"accepted Worker attempt is missing")
    if not identity_current: return WorkerLateActionDecision(False,"exact candidate identity is stale or ambiguous")
    if action=="resume_adopt": return WorkerLateActionDecision(True,"exact accepted attempt/candidate recovered")
    if action=="semantic_publication": return WorkerLateActionDecision(mode=="Implementation" and write_fence_verified,"persistent Implementation publication fence required")
    if action in {"draft_to_review_ready","final_handoff"}: return WorkerLateActionDecision(mode=="Implementation" and write_fence_verified and authoritative_readback,"Implementation fence plus authoritative readback required")
    if action=="code_review_verdict": return WorkerLateActionDecision(mode=="Code Review" and independent is True,"Code Review mode plus cumulative-authorship independence required")
    if action=="design_review_verdict": return WorkerLateActionDecision(mode=="Design Review" and independent is True,"Design Review mode plus cumulative-authorship independence required")
    return WorkerLateActionDecision(bool(scoped_override),"exact scoped Marco override required")

class WorkspaceAgentDispatcher(_BaseWorkspaceAgentDispatcher):
    """Existing Workspace Worker transport with durable R6 attempt recovery."""
    @staticmethod
    def worker_attempt_idempotency_key(*,role,phase,exact_context,attempt):
        v={"schema":"dish-worker-dispatch:v2","role":role,"phase":phase,"context":_exact_context(exact_context),"attempt_id":attempt.attempt_id,"generation":attempt.generation}
        return hashlib.sha256(_canon(v).encode()).hexdigest()
    def dispatch_worker_durable(self,*,surface,surface_id,role,phase,exact_context,replacement=False):
        if not self.access_token: raise LifecycleError("Workspace Agent access token is unavailable")
        if not self.worker_trigger_id: raise LifecycleError("published ChatGPT Worker Workspace Agent trigger is unavailable")
        role=str(role).strip(); phase=str(phase).strip(); context=_exact_context(exact_context)
        if role not in _WORKER_MODES or not phase: raise LifecycleError("Worker durable dispatch requires one supported explicit mode and phase")
        if context.get("pr") is not None and hasattr(surface,"get_comments") and int(context["pr"])!=int(surface_id): raise LifecycleError("Worker durable dispatch PR does not match exact context")
        digest=worker_assignment_digest(context); attempt=_prepare(surface,surface_id,digest,role,replacement)
        key=self.worker_attempt_idempotency_key(role=role,phase=phase,exact_context=context,attempt=attempt)
        prompt=(f"Run exactly one Dish Worker phase. Standing role: {role}. Phase: {phase}. Worker attempt_id: {attempt.attempt_id}. Generation: {attempt.generation}. Exact durable context: {json.dumps(context,sort_keys=True)}. This accepted attempt survives explicit mode switches; do not self-mint a replacement, task, role, or next phase. Worker is an execution host/profile, not a union role. Load the mapped standing role contract. HTTP 202 proves admission only. Integration landing remains outside Worker authority.")
        headers={"Authorization":f"Bearer {self.access_token}","OpenAI-Beta":WORKSPACE_RUNS_BETA,"Idempotency-Key":key,"Content-Type":"application/json"}
        status,_,value=self.http.request("POST",f"{self.api_root}/workspace_agents/{self.worker_trigger_id}/trigger",headers=headers,body={"conversation_key":f"dish-worker-{attempt.attempt_id}","input":prompt})
        if status!=202: raise LifecycleError(f"Workspace Agent Worker trigger was not accepted: HTTP {status}")
        accepted=WorkerAttempt(digest,attempt.attempt_id,attempt.generation,role,"accepted"); _ensure(surface,surface_id,_attempt_record(accepted,role,"accepted"))
        recovered=recover_worker_attempt(_records(surface,surface_id),digest)
        if recovered is None or not recovered.accepted or recovered.attempt_id!=attempt.attempt_id or recovered.mode!=role: raise LifecycleError("Worker accepted attempt did not survive authoritative recovery")
        payload=value if isinstance(value,dict) else {}
        return WorkerDispatchResult(key,True,status,attempt.attempt_id,attempt.generation,digest,role,payload.get("conversation_url"),payload.get("agent_trigger_run_id"))
