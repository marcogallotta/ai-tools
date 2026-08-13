from dataclasses import replace
import pr_lifecycle_helpers_base as b
from pr_lifecycle_support import *
def _id(r): return (r.task_gid,r.owner_pr,r.check,r.main_sha,r.evidence)
def parse_external_dependency(comments):
 r=[]
 for c in comments:
  for i,f in enumerate(b._marker_fields(str(c.get("body") or ""),EXTERNAL_DEPENDENCY_MARKER)):
   m=" ".join(f"{k}={v}" for k,v in f.items())
   x=b.parse_external_dependency([{**c,"body":f"<!-- {EXTERNAL_DEPENDENCY_MARKER} {m} -->"}])
   if x is None: raise LifecycleError("external dependency marker could not be parsed")
   r.append(replace(x,marker_index=i))
 a=None
 for x in sorted(r,key=lambda x:(x.timestamp,x.comment_id,x.marker_index)):
  if x.action=="blocked": a=x
  elif a is not None and _id(x)==_id(a): a=None
 return a
