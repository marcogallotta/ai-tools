#!/usr/bin/env python3
"""Estimate GitHub Actions billed minutes and monthly allowance overage."""
from __future__ import annotations
import argparse,json,math,os,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path

REPORT_SCHEMA="dish-github-actions-cost-report-v1"; CONFIG_SCHEMA="dish-github-actions-billing-config-v1"
class CostReportError(ValueError): pass

def _ts(v,n):
    try:d=datetime.fromisoformat(str(v).replace("Z","+00:00"))
    except ValueError as e:raise CostReportError(f"invalid {n}: {v!r}") from e
    if d.tzinfo is None:raise CostReportError(f"{n} must include a timezone")
    return d.astimezone(timezone.utc)

def runtime_seconds(j):
    if j.get("conclusion")=="skipped" or not j.get("runner_id"):return 0
    n=int((_ts(j.get("completed_at"),"completed_at")-_ts(j.get("started_at"),"started_at")).total_seconds())
    if n<0:raise CostReportError(f"job {j.get('id')} completed before it started")
    return n

def billed_minutes(j):
    n=runtime_seconds(j)
    return (0 if j.get("conclusion")=="skipped" or not j.get("runner_id") else 1) if n==0 else max(1,math.ceil(n/60))

def load_billing_config(path):
    try:c=json.loads(Path(path).read_text())
    except (OSError,json.JSONDecodeError) as e:raise CostReportError(f"cannot read billing config {path}: {e}") from e
    if not isinstance(c,dict) or c.get("schema")!=CONFIG_SCHEMA:raise CostReportError(f"billing config schema must be {CONFIG_SCHEMA!r}")
    if c.get("job_rounding")!="ceil_each_started_job_minute":raise CostReportError("unsupported job_rounding configuration")
    rates=c.get("rates_usd_per_billed_minute"); allowance=c.get("included_minutes_per_month")
    if not isinstance(rates,dict) or not rates:raise CostReportError("billing config must define rates_usd_per_billed_minute")
    if any(not isinstance(k,str) or isinstance(v,bool) or not isinstance(v,(int,float)) or v<0 for k,v in rates.items()):
        raise CostReportError("billing rates must map runner labels to non-negative numbers")
    if isinstance(allowance,bool) or not isinstance(allowance,int) or allowance<0:raise CostReportError("included_minutes_per_month must be a non-negative integer")
    return c

def _rate(j,c):
    if billed_minutes(j)==0:return 0.0
    labels=j.get("labels"); rates=c["rates_usd_per_billed_minute"]
    if not isinstance(labels,list):raise CostReportError(f"job {j.get('id')} is missing runner labels")
    x=[float(rates[k]) for k in labels if k in rates]
    if len(x)!=1:raise CostReportError(f"job {j.get('id')} must match exactly one configured billing label; labels={labels!r}")
    return x[0]

def _period(since,until,prior):
    if (since is None)!=(until is None):raise CostReportError("since and until must be supplied together")
    if prior is not None and (isinstance(prior,bool) or not isinstance(prior,int) or prior<0):raise CostReportError("monthly_billed_minutes_before_period must be a non-negative integer")
    if since is None:return {"since":None,"until":None,"billing_month_utc":None},prior or 0,None,None
    s,e=_ts(since,"since"),_ts(until,"until")
    if e<s:raise CostReportError("until must not precede since")
    if (s.year,s.month)!=(e.year,e.month):raise CostReportError("cost report period must stay within one UTC calendar month")
    if s!=s.replace(day=1,hour=0,minute=0,second=0,microsecond=0) and prior is None:raise CostReportError("non-month-start periods require monthly_billed_minutes_before_period")
    return {"since":since,"until":until,"billing_month_utc":f"{s.year:04d}-{s.month:02d}"},prior or 0,s,e

def build_report(jobs,*,config,since=None,until=None,monthly_billed_minutes_before_period=None):
    period,prior,start,end=_period(since,until,monthly_billed_minutes_before_period); allowance=config["included_minutes_per_month"]
    rows=[];seen=set()
    for j in jobs:
        if j.get("id") in seen:continue
        seen.add(j.get("id")); mins=billed_minutes(j); st=None if mins==0 else _ts(j.get("started_at"),"started_at")
        if st and start and not start<=st<=end:raise CostReportError(f"billable job {j.get('id')} falls outside declared report period")
        rate=_rate(j,config); rows.append([j,st,runtime_seconds(j),mins,rate,0])
    remaining=max(0,allowance-prior)
    for r in sorted((x for x in rows if x[3]),key=lambda x:(x[1],str(x[0].get("id")))):
        used=min(r[3],remaining);remaining-=used;r[5]=r[3]-used
    keys=("jobs","runtime_seconds","billed_minutes","cancelled_billed_minutes","gross_equivalent_cost_usd","overage_billed_minutes","approximate_overage_cost_usd")
    def blank():return {k:0.0 if k.endswith("_usd") else 0 for k in keys}
    totals=blank();by={}
    for j,_,sec,mins,rate,over in rows:
        m={"jobs":1,"runtime_seconds":sec,"billed_minutes":mins,"cancelled_billed_minutes":mins if j.get("conclusion")=="cancelled" else 0,
           "gross_equivalent_cost_usd":mins*rate,"overage_billed_minutes":over,"approximate_overage_cost_usd":over*rate}
        w=by.setdefault(str(j.get("workflow_name") or "<unknown workflow>"),blank())
        for k in keys:totals[k]+=m[k];w[k]+=m[k]
    for d in [totals,*by.values()]:
        for k in ("gross_equivalent_cost_usd","approximate_overage_cost_usd"):d[k]=round(float(d[k]),6)
    return {"schema":REPORT_SCHEMA,"period":period,"billing_config":{"job_rounding":config["job_rounding"],"included_minutes_per_month":allowance,
            "rates_usd_per_billed_minute":config["rates_usd_per_billed_minute"]},"allowance":{"monthly_billed_minutes_before_period":prior,
            "included_minutes_remaining_before_period":max(0,allowance-prior),"included_minutes_remaining_after_period":max(0,allowance-prior-totals["billed_minutes"]),
            "allocation_rule":"started_at_then_job_id"},"totals":totals,"by_workflow":dict(sorted(by.items()))}

def _gh(path):
    p=subprocess.run(["gh","api","--paginate",path],text=True,capture_output=True,check=False)
    if p.returncode:raise CostReportError("gh api failed: "+(p.stderr.strip() or p.stdout.strip()))
    values=[]
    for raw in p.stdout.splitlines():
        if not raw.strip():continue
        x=json.loads(raw); values.append(x)
    return values

def query_jobs(repo,since,until):
    runs=[]
    for x in _gh(f"repos/{repo}/actions/runs?status=completed&created={since}..{until}&per_page=100"):
        runs.extend(x.get("workflow_runs",[]))
    jobs=[]
    for r in runs:
        for x in _gh(f"repos/{repo}/actions/runs/{r.get('id')}/jobs?filter=all&per_page=100"):
            jobs.extend(x.get("jobs",[]))
    return jobs

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument("--repo",default=os.getenv("GITHUB_REPOSITORY"));p.add_argument("--since",required=True);p.add_argument("--until",required=True)
    p.add_argument("--monthly-billed-before-period",type=int);p.add_argument("--config",type=Path,default=Path("ci/actions-billing.json"));p.add_argument("--output",type=Path);a=p.parse_args(argv)
    try:
        report=build_report(query_jobs(a.repo or "",a.since,a.until),config=load_billing_config(a.config),since=a.since,until=a.until,monthly_billed_minutes_before_period=a.monthly_billed_before_period)
        text=json.dumps(report,indent=2,sort_keys=True)+"\n"
        if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text)
        sys.stdout.write(text);return 0
    except (CostReportError,json.JSONDecodeError) as e:print(f"actions_cost_report: {e}",file=sys.stderr);return 2
if __name__=="__main__":raise SystemExit(main())
