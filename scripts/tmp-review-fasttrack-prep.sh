#!/usr/bin/env bash
set -euo pipefail
python3 <<'PY'
from pathlib import Path

# Task 1 owns its new Review regression classification.
p=Path('scripts/tmp-review-fasttrack-task1.sh')
s=p.read_text()
anchor="cp dish/docs/chatgpt-projects/manifest.json /tmp/task1-base-manifest.json\n"
insert="""cp dish/docs/chatgpt-projects/manifest.json /tmp/task1-base-manifest.json
python3 - <<'PY1'
from pathlib import Path
p=Path('dish/test_selection/ownership.csv')
row='tests/test_review_bundle_consistency.py,test,1,4,isolated-test,,,none,,exact changed test/module,,\\n'
s=p.read_text()
if not any(line.startswith('tests/test_review_bundle_consistency.py,') for line in s.splitlines()): p.write_text(s+row)
PY1
"""
if s.count(anchor)!=1: raise SystemExit('Task 1 ownership insertion anchor changed')
s=s.replace(anchor,insert,1)
old="git add CLAUDE.md dish/docs/agents/standing-invariants.json dish/docs/chatgpt-projects dish/scripts/chatgpt_project_kernels.py dish/tests/test_chatgpt_project_kernels.py dish/tests/test_review_bundle_consistency.py"
new=old+" dish/test_selection/ownership.csv"
if s.count(old)!=1: raise SystemExit('Task 1 git-add baseline changed')
p.write_text(s.replace(old,new,1))

# Task 2 owns its new policy/registry/test classifications.
p=Path('scripts/tmp-review-fasttrack-task2.sh')
s=p.read_text()
anchor="cp dish/docs/chatgpt-projects/manifest.json /tmp/task2-base-manifest.json\n"
insert="""cp dish/docs/chatgpt-projects/manifest.json /tmp/task2-base-manifest.json
python3 - <<'PY2'
from pathlib import Path
p=Path('dish/test_selection/ownership.csv')
s=p.read_text()
rows=[
'docs/agents/fast-track-process.md,documentation,1,1,agent-policy; documentation,tests/test_fast_track_overlay.py,tests/test_fast_track_overlay.py,none,,exact changed test/module,,\\n',
'docs/chatgpt-projects/fast-track-gates.json,config-or-runner,1,4,agent-policy; config,tests/test_fast_track_overlay.py,tests/test_fast_track_overlay.py,none,,exact changed test/module,,\\n',
'tests/test_fast_track_overlay.py,test,1,4,isolated-test,,,none,,exact changed test/module,,\\n',
]
for row in rows:
    path=row.split(',',1)[0]
    if not any(line.startswith(path+',') for line in s.splitlines()): s+=row
p.write_text(s)
PY2
"""
if s.count(anchor)!=1: raise SystemExit('Task 2 ownership insertion anchor changed')
s=s.replace(anchor,insert,1)
old="git add CLAUDE.md dish/docs/agents/fast-track-process.md dish/docs/chatgpt-projects dish/scripts/chatgpt_project_kernels.py dish/tests/test_chatgpt_project_kernels.py dish/tests/test_fast_track_overlay.py"
new=old+" dish/test_selection/ownership.csv"
if s.count(old)!=1: raise SystemExit('Task 2 git-add baseline changed')
p.write_text(s.replace(old,new,1))
PY
