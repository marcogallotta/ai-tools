from dataclasses import replace
import pr_lifecycle_helpers_base as b
from pr_lifecycle_support import *
def _id(r): return (r.task_gid,r.owner_pr,r.check,r.main_sha,r.evidence)
def replay_external_dependency(comments):
 r=[]
 for c in comments:
  for i,f in enumerate(b._marker_fields(str(c.get("body") or ""),EXTERNAL_DEPENDENCY_MARKER)):
   m=" ".join(f"{k}={v}" for k,v in f.items())
   x=b.parse_external_dependency([{**c,"body":f"<!-- {EXTERNAL_DEPENDENCY_MARKER} {m} -->"}])
   if x is None: raise LifecycleError("external dependency marker could not be parsed")
   r.append(replace(x,marker_index=i))
 a=z=None
 for x in sorted(r,key=lambda x:(x.timestamp,x.comment_id,x.marker_index)):
  if x.action=="blocked": a,z=x,None
  elif a is not None and _id(x)==_id(a): a,z=None,x
 return a,z
latest_external_dependency_record = b.parse_external_dependency
def resolve_external_dependency(comments): return replay_external_dependency(comments)[0]
# Backward-compatible alias; canonical lifecycle-state API is resolve_external_dependency.
parse_external_dependency = resolve_external_dependency
