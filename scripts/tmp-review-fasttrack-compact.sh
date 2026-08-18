#!/usr/bin/env bash
set -euo pipefail
python3 <<'PY'
from pathlib import Path
p=Path('scripts/tmp-review-fasttrack-task2.sh')
s=p.read_text()
old_trigger="trigger='persistent fast-track Project override capture / use / revocation'"
new_trigger="trigger='fast-track'"
if s.count(old_trigger)!=1:
    raise SystemExit('Task 2 trigger baseline changed')
s=s.replace(old_trigger,new_trigger,1)
old_text="    'text':'A reserved `MARCO OVERRIDE — FAST-TRACK PROCESS` Project block is session-captured operator authority independent of repository grounding. Capture its exact generation/digest only at a verified new Project chat/session bootstrap or a future proven refresh; ordinary re-ground never refreshes it, while explicit current-chat Marco change/revocation is immediate. Apply only ACTIVE/unexpired exact gate ID/version entries still current with unchanged semantics in `fast-track-gates.json`; never inherit unknown/new/materially changed gates or wildcards. Each use records `GATE WAIVED BY MARCO OVERRIDE` plus overlay generation/digest + gate/version + exact task/candidate/action while raw failure stays failed. Exact identity, independent Review, Integration separation, destructive/production safeguards and genuine platform impossibilities remain outside default scope.',"
new_text="    'text':'Fast-track: read triggered Procedure.',"
if s.count(old_text)!=1:
    raise SystemExit('Task 2 direct rule baseline changed')
s=s.replace(old_text,new_text,1)
old_test="""        assert 'MARCO OVERRIDE — FAST-TRACK PROCESS' in rendered
        assert 'ordinary re-ground never refreshes it' in rendered
        assert 'persistent fast-track Project override capture / use / revocation' in rendered
        assert 'dish/docs/agents/fast-track-process.md#Procedure' in rendered
"""
new_test="""        assert 'Fast-track: read triggered Procedure.' in rendered
        assert 'fast-track' in rendered
        assert 'dish/docs/agents/fast-track-process.md#Procedure' in rendered
"""
if s.count(old_test)!=1:
    raise SystemExit('Task 2 generated-kernel test baseline changed')
s=s.replace(old_test,new_test,1)
p.write_text(s)
PY
