#!/usr/bin/env python3
import json
import re
from pathlib import Path

TASK_BASE_VERSION = 'd3a070d57fb2'


def replace_section(path: str, heading: str, body: str) -> None:
    p = Path(path)
    text = p.read_text()
    pattern = re.compile(rf'^{re.escape(heading)}\n.*?(?=^## |\Z)', re.M | re.S)
    if len(pattern.findall(text)) != 1:
        raise SystemExit(f'section mismatch: {path} {heading}')
    p.write_text(pattern.sub(heading + '\n\n' + body.strip() + '\n\n', text, count=1))


replace_section(
    'dish/docs/agents/contributor-base.md',
    '## Repository freshness',
    '''At initial task/branch creation, establish one exact fresh authoring base and pin it for that implementation attempt. Re-observing GitHub/origin later may update current-state knowledge, but it does not silently replace the pinned authoring base.

During active authoring, unrelated `main` movement is informational, not an action trigger. Do not repeatedly poll/fetch `origin/main`, and do not reset/rebase/merge/synchronize the owned branch merely because other work landed. Multiple unrelated target movements should normally cause zero branch mutations.

Reconcile the authoring base only for a concrete base-sensitive reason: a required semantic dependency landed and the task actually consumes it; an explicit Coordinator/Review/Integration or other lifecycle boundary requires reconciliation; a real conflict or base-sensitive validation condition is proven; final PR/Integration preparation requires current-target reconciliation; or Marco explicitly asks to sync/rebase/merge.

Starting or resuming after substantial interruption still requires a freshness re-observation, but that read does not itself mutate the pinned base. If one justified reconciliation occurs, finish from that reconciled base; later unrelated `main` movement does not restart the synchronization loop. Current-target conflict handling remains an Integration/publication concern unless one of the concrete triggers above makes it an authoring concern.''',
)

replace_section(
    'dish/docs/agents/implementation.md',
    '## Repository freshness',
    '''Pin one exact, freshly verified authoring base at task/branch creation and finish ordinary authoring against it. `main moved` is informational, not an action trigger: unrelated B/C/D target movements during one implementation attempt should cause zero fetch/rebase/merge/reset mutations solely for freshness.

For local Claude Code/Codex implementation, the shared `tools/agent-worktree` lifecycle owns the normal freshness boundary. First creation verifies the supplied exact base ref + SHA against authoritative `origin` before it creates the owned branch/worktree. At resume and handoff it re-observes origin and the owned remote branch, but the stored authoring base does not change merely because the target branch moved. Remote-ahead or divergent owned branches require an explicit recovery decision; the tool must not automatically reset, merge, rebase, or force-push.

Reconcile during authoring only for a named base-sensitive trigger: a required semantic dependency that landed and is actually consumed; an explicit lifecycle/Coordinator/Review/Integration boundary; a proven conflict or base-sensitive validation condition; final PR/Integration preparation; or Marco's explicit sync/rebase/merge instruction. A generic desire to be current is not enough.

Starting or resuming after interruption still requires a freshness read, but observation alone does not mutate the pinned base. If one justified reconciliation occurs, continue from that reconciled base; later unrelated target movement does not trigger a second loop. Implementation owns the candidate against its stable authoring base; current-target reconciliation stays at the appropriate publication/Review/Integration boundary unless one of the named triggers makes it necessary earlier.

Do not update task state or narrate repeated synchronization merely because unrelated commits appear on GitHub.''',
)

dw = Path('dish/docs/agents/development-workflow.md')
text = dw.read_text()
block = '''## Stable authoring base discipline

Development Workflow treats one exact fresh task/branch base as the implementation authoring base, not as a lease that expires whenever `main` advances. Re-observation at startup/resume may update current-state knowledge, but unrelated target movement must not cause automatic fetch/rebase/merge/reset or repeated sync narration. Reconcile only for a named semantic dependency, explicit lifecycle/Review/Integration boundary, proven conflict/base-sensitive validation, final PR/Integration preparation, or Marco's explicit sync instruction. After one justified reconciliation, later unrelated movement remains informational and does not restart the loop. Preserve stale-start checks, exact-head Review/Integration identity, and final conflict handling.

'''
anchor = '## Governed decision-context preload\n'
if block not in text:
    if text.count(anchor) != 1:
        raise SystemExit('development-workflow insertion anchor mismatch')
    dw.write_text(text.replace(anchor, block + anchor, 1))

source_path = Path('dish/docs/chatgpt-projects/source.json')
source = json.loads(source_path.read_text())
rule = {
    'id': 'implementation-stable-authoring-base',
    'text': 'Pin one exact fresh authoring base per implementation attempt. Unrelated main movement is informational: do not poll/fetch/rebase/merge/reset just to be current. Reconcile only for a consumed semantic dependency, explicit lifecycle/Review/Integration boundary, proven conflict/base-sensitive validation, final PR/Integration preparation, or Marco explicit sync. After one justified reconciliation, later unrelated main movement does not restart the loop.',
    'impact': 'additive',
    'surface': 'lifecycle',
    'action_boundaries': ['role-critical-write', 'handoff'],
    'delivery': {'mode': 'DIRECT_ALWAYS_ON'},
}
rules = source['roles']['implementation']['rules']
if any(x.get('id') == rule['id'] for x in rules):
    raise SystemExit('stable-base rule unexpectedly exists')
rules.append(rule)
source_path.write_text(json.dumps(source, indent=2, ensure_ascii=False) + '\n')

evals_path = Path('dish/docs/chatgpt-projects/evals.json')
payload = json.loads(evals_path.read_text())
additions = [
    {
        'id': 'implementation-stable-base-unrelated-main',
        'roles': ['implementation'],
        'required_rules': ['implementation-stable-authoring-base'],
        'prompt': 'Implementation starts from exact fresh base A. While it edits and tests, unrelated PRs advance main through B, C, and D. No dependency, conflict, lifecycle handoff, or base-sensitive validation requires those commits. What should the agent do?',
        'expected_outcome': 'continue_authoring_on_pinned_base_without_main_chasing',
        'required_actions': ['pin-exact-authoring-base', 'treat-unrelated-main-movement-as-informational', 'continue-authoring-to-pr-handoff'],
        'forbidden_actions': ['poll-fetch-main-for-freshness', 'rebase-merge-reset-for-unrelated-main', 'narrate-sync-loop-as-progress'],
    },
    {
        'id': 'implementation-stable-base-semantic-trigger',
        'roles': ['implementation'],
        'required_rules': ['implementation-stable-authoring-base'],
        'prompt': 'Implementation is authoring on pinned base A. A named required semantic dependency lands on C and the task actually consumes that dependency. No other reconciliation has occurred. What should the agent do?',
        'expected_outcome': 'perform_one_deliberate_dependency_reconciliation',
        'required_actions': ['name-concrete-reconciliation-trigger', 'reconcile-authoring-base-once', 'continue-from-reconciled-base'],
        'forbidden_actions': ['reconcile-for-generic-up-to-date-desire', 'start-continuous-main-polling'],
    },
    {
        'id': 'implementation-stable-base-no-restart-after-reconcile',
        'roles': ['implementation'],
        'required_rules': ['implementation-stable-authoring-base'],
        'prompt': 'Implementation already performed one justified reconciliation because a required dependency landed on C. While it finishes, main advances to D only for unrelated work. What should the agent do?',
        'expected_outcome': 'finish_from_reconciled_base_without_second_sync_loop',
        'required_actions': ['retain-reconciled-authoring-base', 'treat-later-unrelated-main-movement-as-informational', 'continue-authoring'],
        'forbidden_actions': ['reconcile-again-for-unrelated-main', 'restart-authoring-freshness-loop'],
    },
]
known = {x.get('id') for x in payload['scenarios']}
if any(x['id'] in known for x in additions):
    raise SystemExit('Task 2 eval already exists')
payload['scenarios'].extend(additions)
evals_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n')

generator_path = Path('dish/scripts/chatgpt_project_kernels.py')
generator = generator_path.read_text()
match = re.search(r"REQUIRED_EVAL_IDS=\{[^\n]*\}", generator)
if not match:
    raise SystemExit('REQUIRED_EVAL_IDS not found')
segment = match.group(0)[:-1] + ', ' + ', '.join(repr(x['id']) for x in additions) + '}'
generator_path.write_text(generator[:match.start()] + segment + generator[match.end():])

test_path = Path('dish/tests/test_chatgpt_project_kernels.py')
test_text = test_path.read_text()
old = "'bfaeef68aed9']}"
new = f"'bfaeef68aed9','{TASK_BASE_VERSION}']}}"
if test_text.count(old) != 1 or test_text.count('len(versions)==18') != 1:
    raise SystemExit('required-version fixture mismatch')
test_path.write_text(test_text.replace(old, new, 1).replace('len(versions)==18', 'len(versions)==19', 1))
