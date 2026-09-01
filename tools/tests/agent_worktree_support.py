from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1]
SCRIPT = TOOLS_DIR / "agent-worktree"
GIT = shutil.which("git") or "git"
CANONICAL_ORIGIN = "git@github.com:marcogallotta/ai-tools.git"


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(cmd, cwd=cwd, env=env, text=True, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if check and completed.returncode != 0:
        raise AssertionError(f"command failed {completed.returncode}: {cmd}\nstdout={completed.stdout}\nstderr={completed.stderr}")
    return completed


def git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    return run([GIT, "-C", str(cwd), *args], env=env, check=check, input=input)


def git_out(cwd: Path, *args: str, env: dict[str, str] | None = None, input: str | None = None) -> str:
    return git(cwd, *args, env=env, input=input).stdout.strip()


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.primary = root / "primary"
        self.home = root / "home"
        self.worktree_root = root / "worktrees"
        self.ssh = root / "fake-ssh"
        run([GIT, "init", "--bare", str(self.origin)])
        run([GIT, "init", "-b", "main", str(self.seed)])
        self._identity(self.seed)
        (self.seed / "tracked.txt").write_text("base\n", encoding="utf-8")
        tools = self.seed / "tools"
        tools.mkdir()
        (tools / ".gitignore").write_text("__pycache__/\n.venv/\n", encoding="utf-8")
        # The synthetic repository has no third-party tool dependencies; an empty
        # requirements file exercises the real worktree-local venv lifecycle without
        # turning every Git lifecycle regression into a package-download test.
        (tools / "requirements.txt").write_text("", encoding="utf-8")
        git(self.seed, "add", "tracked.txt", "tools/.gitignore", "tools/requirements.txt")
        git(self.seed, "commit", "-m", "base")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "push", "-u", "origin", "main")
        run([GIT, f"--git-dir={self.origin}", "symbolic-ref", "HEAD", "refs/heads/main"])
        run([GIT, "clone", "-b", "main", str(self.origin), str(self.primary)])
        self._identity(self.primary)
        git(self.primary, "remote", "set-url", "origin", CANONICAL_ORIGIN)
        self.ssh.write_text(
            "#!/usr/bin/env python3\n"
            "import os, shlex, sys\n"
            "cmd = sys.argv[-1]\n"
            "parts = shlex.split(cmd)\n"
            "if not parts or parts[0] not in ('git-upload-pack', 'git-receive-pack'):\n"
            "    raise SystemExit(f'unexpected ssh command: {cmd}')\n"
            "os.execvp(parts[0], [parts[0], os.environ['TEST_BARE_ORIGIN']])\n",
            encoding="utf-8",
        )
        self.ssh.chmod(0o755)
        self.home.mkdir()
        self.worktree_root.mkdir()
        asana = self.home / ".local/bin/asana"
        asana.parent.mkdir(parents=True)
        self.asana_sections = self.home / "asana-task-sections.json"
        self.asana_sections.write_text("{}\n", encoding="utf-8")
        self.asana_projects = self.home / "asana-task-projects.json"
        self.asana_projects.write_text("{}\n", encoding="utf-8")
        self.asana_project_sections = self.home / "asana-project-sections.json"
        self.asana_project_sections.write_text("{}\n", encoding="utf-8")
        self.asana_log = self.home / "asana-calls.jsonl"
        self.asana_log.write_text("", encoding="utf-8")
        self.github_reviews = self.home / "github-reviews.json"
        self.github_reviews.write_text("{}\n", encoding="utf-8")
        asana.write_text(
            "#!/usr/bin/env python3\n"
            "import datetime, hashlib, json, os, pathlib, sys\n"
            "path = sys.argv[-1]\n"
            "home = pathlib.Path.home()\n"
            "with (home/'asana-calls.jsonl').open('a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "v2 = ['Needs Processing','Needs Research','Needs Agentic Review','Needs Human Review','Waiting on Dependency','Ready','Under Development','Needs Post-Merge Rollout','Done']\n"
            "names = {'1217419962189616':'Dish — Development Workflow v2','1217404747383060':'Dish — PostgreSQL / Dark Launch v2','1217382473444945':'Dish — Coordinator v2'}\n"
            "if '/projects/' in path and '/sections' in path:\n"
            "  project = path.split('/projects/',1)[1].split('/',1)[0]\n"
            "  configured = json.loads((home/'asana-project-sections.json').read_text())\n"
            "  print(json.dumps([{'gid':f's-{i}','name':name} for i,name in enumerate(configured.get(project,v2))])); raise SystemExit\n"
            "task = path.split('/tasks/',1)[1].split('/',1)[0].split('?',1)[0]\n"
            "if '/stories' in path:\n"
            "  if os.environ.get('TEST_ASANA_NO_HANDOFF') == '1': print('[]'); raise SystemExit\n"
            "  branch=os.environ.get('TEST_ASANA_BRANCH',os.environ['DISH_HANDOFF_EXPECTED_BRANCH']); base_ref=os.environ.get('TEST_ASANA_BASE_REF',os.environ['DISH_HANDOFF_EXPECTED_BASE_REF']); base=os.environ.get('TEST_ASANA_BASE',os.environ['DISH_HANDOFF_EXPECTED_BASE']); pr=os.environ.get('TEST_ASANA_PR',os.environ.get('DISH_HANDOFF_EXPECTED_PR')); head=os.environ.get('TEST_ASANA_HEAD',os.environ.get('DISH_HANDOFF_EXPECTED_HEAD'))\n"
            "  source=f'dish-prelaunch:v1 repository=marcogallotta/ai-tools task={task} assignment=implementation host=local branch={branch} base_ref={base_ref} base_sha={base} existing_pr=' + (f'{pr} expected_head={head}' if pr else 'none')\n"
            "  at='2026-09-01T10:00:00+00:00'; raw=f'{task}\\0Implementation\\0{at}\\0{source}'; hid=hashlib.sha256(raw.encode()).hexdigest()[:16]\n"
            "  text='\\n'.join([f'<!-- dish-implementation-handoff:v1 handoff={hid} task={task} role=Implementation at={at} -->','AUTHORIZED IMPLEMENTATION HANDOFF',f'Task: {task}','Target role: Implementation',f'Handoff time: {at}',f'Source: {source}',f'Branch: {branch}',f'Base: {base}',f'PR: {pr if pr else \"not yet known\"}',f'Head: {head if head else \"not yet known\"}','— Dish Agent: Development Workflow | repository control plane'])\n"
            "  if os.environ.get('TEST_ASANA_TAMPER_HANDOFF') == '1': text=text.replace(f'Branch: {branch}', 'Branch: agent/tampered')\n"
            "  stories=[{'gid':'story-1','created_at':at,'text':text,'resource_subtype':'comment_added'}]\n"
            "  if os.environ.get('TEST_ASANA_DUPLICATE_HANDOFF') == '1': stories.append({'gid':'story-2','created_at':at,'text':text,'resource_subtype':'comment_added'})\n"
            "  print(json.dumps(stories)); raise SystemExit\n"
            "sections = json.loads((home/'asana-task-sections.json').read_text())\n"
            "projects = json.loads((home/'asana-task-projects.json').read_text())\n"
            "project = projects.get(task, {'gid':'1217419962189616','name':'Dish — Development Workflow v2'})\n"
            "section = sections.get(task, 'Under Development')\n"
            "memberships=[{'project': item, 'section': {'gid': 'fixture-section', 'name': section}} for item in (project if isinstance(project,list) else [project])]\n"
            "print(json.dumps({'gid': task, 'completed': False, 'memberships': memberships}))\n",
            encoding="utf-8",
        )
        asana.chmod(0o755)
        gh = self.home / ".local/bin/gh"
        gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "home=pathlib.Path.home(); path=sys.argv[-1]; data=json.loads((home/'github-reviews.json').read_text())\n"
            "parts=path.strip('/').split('/'); pr=parts[4]\n"
            "entry=data.get(pr)\n"
            "if entry is None: print('not found', file=sys.stderr); raise SystemExit(1)\n"
            "if 'reviews' in parts:\n"
            "  review=entry.get('reviews',{}).get(parts[-1])\n"
            "  if review is None: print('not found', file=sys.stderr); raise SystemExit(1)\n"
            "  print(json.dumps(review)); raise SystemExit\n"
            "print(json.dumps(entry['pr']))\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)
        self.env = os.environ.copy()
        for key in list(self.env):
            if key in {
                "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_NAMESPACE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CEILING_DIRECTORIES",
                "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_SHALLOW_FILE",
                "GIT_IMPLICIT_WORK_TREE",
            } or key.startswith("GIT_CONFIG_") or key.startswith("DISH_AGENT_"):
                self.env.pop(key, None)
        self.env.update(
            HOME=str(self.home),
            PATH=f"{self.home / '.local/bin'}:{self.env.get('PATH', '')}",
            DISH_WORKTREE_ROOT=str(self.worktree_root),
            GIT_SSH_COMMAND=str(self.ssh),
            GIT_SSH_VARIANT="ssh",
            TEST_BARE_ORIGIN=str(self.origin),
        )

    def set_block_review(self, *, task: str, pr: int, branch: str, head: str, review_id: str, verdict: str = "BLOCK") -> None:
        data = json.loads(self.github_reviews.read_text(encoding="utf-8"))
        data[str(pr)] = {
            "pr": {"state": "open", "body": f"Implements Asana task {task}.", "head": {"ref": branch, "sha": head}},
            "reviews": {str(review_id): {"id": int(review_id), "commit_id": head, "body": f"VERDICT: {verdict}\n\nfixture"}},
        }
        self.github_reviews.write_text(json.dumps(data) + "\n", encoding="utf-8")

    @staticmethod
    def _identity(repo: Path) -> None:
        git(repo, "config", "user.name", "Fixture")
        git(repo, "config", "user.email", "fixture@example.invalid")

    @property
    def base(self) -> str:
        return git_out(self.primary, "rev-parse", "HEAD")

    def current_remote_main(self) -> str:
        return git_out(self.origin, "rev-parse", "refs/heads/main")

    def agent_file(self, agent: str, **extra: object) -> Path:
        path = self.home / ".local/state/dish/agents" / f"{agent}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"agent_id": agent, "role": "implementation", "workspace": "legacy", "notes": "keep"}
        payload.update(extra)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def set_task_section(self, task: str, section: str) -> None:
        sections = json.loads(self.asana_sections.read_text(encoding="utf-8"))
        sections[task] = section
        self.asana_sections.write_text(json.dumps(sections) + "\n", encoding="utf-8")

    def set_task_project(self, task: str, gid: str, name: str) -> None:
        projects = json.loads(self.asana_projects.read_text(encoding="utf-8"))
        projects[task] = {"gid": gid, "name": name}
        self.asana_projects.write_text(json.dumps(projects) + "\n", encoding="utf-8")

    def set_task_projects(self, task: str, projects_value: list[dict[str, str]]) -> None:
        projects = json.loads(self.asana_projects.read_text(encoding="utf-8"))
        projects[task] = projects_value
        self.asana_projects.write_text(json.dumps(projects) + "\n", encoding="utf-8")

    def set_project_sections(self, gid: str, sections: list[str]) -> None:
        projects = json.loads(self.asana_project_sections.read_text(encoding="utf-8"))
        projects[gid] = sections
        self.asana_project_sections.write_text(json.dumps(projects) + "\n", encoding="utf-8")

    @staticmethod
    def _option(args: tuple[str, ...] | list[str], name: str) -> str | None:
        try:
            index = list(args).index(name)
        except ValueError:
            return None
        if index + 1 >= len(args):
            return None
        return str(args[index + 1])

    def raw_tool(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        actual_env = self.env.copy()
        if env:
            actual_env.update(env)
        if args and args[0] == "claim":
            values = list(args)
            branch = self._option(values, "--branch")
            stored: dict[str, object] | None = None
            if branch is not None:
                actual_env.setdefault("TEST_ASANA_BRANCH", branch)
                for candidate in self.state_paths(self._option(values, "--task") or ""):
                    candidate_state = json.loads(candidate.read_text(encoding="utf-8"))
                    if candidate_state.get("branch") == branch:
                        stored = candidate_state
                        break
            actual_env.setdefault(
                "TEST_ASANA_BASE_REF",
                self._option(values, "--base-ref") or str(stored.get("base_ref") if stored else "refs/heads/main"),
            )
            actual_env.setdefault(
                "TEST_ASANA_BASE",
                self._option(values, "--base") or str(stored.get("base_sha") if stored else self.current_remote_main()),
            )
            pr = self._option(values, "--pr-number")
            head = self._option(values, "--pr-head")
            if pr is not None:
                actual_env.setdefault("TEST_ASANA_PR", pr)
            if head is not None:
                actual_env.setdefault("TEST_ASANA_HEAD", head)
            if pr is not None and head is not None and actual_env.get("TEST_GITHUB_PRESERVE_PR") != "1":
                data = json.loads(self.github_reviews.read_text(encoding="utf-8"))
                entry = data.setdefault(str(pr), {"reviews": {}})
                entry["pr"] = {
                    "state": "open",
                    "body": f"Implements Asana task {self._option(values, '--task')}.",
                    "head": {"ref": branch, "sha": head},
                }
                self.github_reviews.write_text(json.dumps(data) + "\n", encoding="utf-8")
        return run(["python3", str(SCRIPT), *args], cwd=self.primary, env=actual_env, check=check)

    def tool(self, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        if not args or args[0] not in {"start", "adopt", "resume", "commit", "publish", "verify-handoff", "exec"}:
            return self.raw_tool(*args, check=check, env=env)

        child = list(args)
        task = self._option(child, "--task")
        if task is None:
            raise AssertionError(f"writer command is missing --task: {child}")
        branch = self._option(child, "--branch")
        state: dict[str, object] | None = None
        if branch is None and self.state_path(task).exists():
            state = self.state(task)
            branch = str(state["branch"])
        if branch is None:
            raise AssertionError(f"could not resolve branch for claimed writer command: {child}")

        agent = self._option(child, "--agent-id")
        if agent is None and state is not None:
            owner = state.get("owner")
            if isinstance(owner, dict) and owner.get("agent_id") is not None:
                agent = str(owner["agent_id"])
        if agent is None:
            agent = "fixture-agent"
        agent_path = self.home / ".local/state/dish/agents" / f"{agent}.json"
        if not agent_path.exists():
            self.agent_file(agent, owning_task_gid=task)
        else:
            identity = json.loads(agent_path.read_text(encoding="utf-8"))
            identity["owning_task_gid"] = task
            agent_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
        if child[0] in {"start", "adopt", "resume"} and "--agent-id" not in child:
            child.extend(["--agent-id", agent])

        claim = ["claim", "--task", task, "--branch", branch, "--agent-id", agent]
        claim_files = list((self.home / ".local/state/dish/worktrees/claims").glob(f"*/{task}*.json"))
        prior: dict[str, object] | None = None
        if len(claim_files) == 1:
            prior = json.loads(claim_files[0].read_text(encoding="utf-8"))
        if child[0] == "resume" and "--takeover" in child:
            claim.append("--takeover")
            expected = str(prior["token"]) if prior is not None else "legacy-unclaimed"
            claim.extend(["--expected-claim", expected])
        if child[0] == "adopt":
            expected = self._option(child, "--expected-head")
            assert expected is not None
            # Default PR number is derived from the task gid (rather than a fixed
            # constant) so two independent real adopts for different tasks in the
            # same test don't collide on one hard-coded PR identity.
            claim.extend(["--pr-number", str(int(task) % 1_000_000_000 or 42), "--pr-head", expected, "--pr-lease-state", "none"])
        elif prior is not None:
            pr = prior.get("pr")
            if isinstance(pr, dict):
                claim.extend([
                    "--pr-number", str(pr["number"]),
                    "--pr-head", str(pr["head"]),
                    "--pr-lease-state", str(pr["lease_state"]),
                ])
                if pr.get("lease_id") is not None:
                    claim.extend(["--pr-lease-id", str(pr["lease_id"])])
        claim.extend(["--", "python3", str(SCRIPT), *child])
        return self.raw_tool(*claim, check=check, env=env)

    def start(self, task: str = "1001", branch: str = "agent/fixture", base: str | None = None, agent: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        args = ["start", "--task", task, "--branch", branch, "--base-ref", "refs/heads/main", "--base", base or self.current_remote_main(), "--json"]
        if agent:
            args.extend(["--agent-id", agent])
        return self.tool(*args, check=check)

    def state_paths(self, task: str = "1001") -> list[Path]:
        root = self.home / ".local/state/dish/worktrees"
        paths = []
        legacy = root / f"{task}.json"
        if legacy.exists():
            paths.append(legacy)
        directory = root / task
        if directory.is_dir():
            paths.extend(sorted(directory.glob("*.json")))
        return paths

    def state_path(self, task: str = "1001", branch: str | None = None) -> Path:
        paths = self.state_paths(task)
        if branch is not None:
            matches = [p for p in paths if json.loads(p.read_text(encoding="utf-8")).get("branch") == branch]
            if len(matches) != 1:
                raise AssertionError(f"expected one state for task={task} branch={branch}, found {matches}")
            return matches[0]
        if len(paths) == 1:
            return paths[0]
        if not paths:
            return self.home / ".local/state/dish/worktrees" / f"{task}.json"
        raise AssertionError(f"task {task} has multiple lineage states: {paths}")

    def state(self, task: str = "1001", branch: str | None = None) -> dict[str, object]:
        return json.loads(self.state_path(task, branch).read_text(encoding="utf-8"))

    def wt(self, task: str = "1001", branch: str | None = None) -> Path:
        paths = self.state_paths(task)
        if paths:
            return Path(str(self.state(task, branch)["worktree_path"]))
        return self.worktree_root / task

    def commit_local(self, task: str = "1001", text: str = "local") -> str:
        wt = self.wt(task)
        self._identity(wt)
        with (wt / "tracked.txt").open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        git(wt, "add", "tracked.txt")
        git(wt, "commit", "-m", text)
        return git_out(wt, "rev-parse", "HEAD")

    def advance_main(self, text: str = "main advance") -> str:
        # Seed uses the bare path directly and remains the remote-main author fixture.
        git(self.seed, "fetch", "origin", "main")
        git(self.seed, "reset", "--hard", "origin/main")
        with (self.seed / "tracked.txt").open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        git(self.seed, "add", "tracked.txt")
        git(self.seed, "commit", "-m", text)
        git(self.seed, "push", "origin", "main")
        return git_out(self.seed, "rev-parse", "HEAD")

    def remote_branch_commit(self, branch: str, text: str, *, start: str | None = None) -> str:
        clone = self.root / f"remote-author-{len(list(self.root.glob('remote-author-*')))}"
        run([GIT, "clone", str(self.origin), str(clone)])
        self._identity(clone)
        if start:
            git(clone, "checkout", "--detach", start)
            git(clone, "switch", "-c", branch)
        else:
            git(clone, "checkout", branch)
        p = clone / f"remote-{text.replace(' ', '-')}.txt"
        p.write_text(text + "\n", encoding="utf-8")
        git(clone, "add", p.name)
        git(clone, "commit", "-m", text)
        git(clone, "push", "origin", f"HEAD:refs/heads/{branch}")
        return git_out(clone, "rev-parse", "HEAD")


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)


def assert_error(result: subprocess.CompletedProcess[str], code: str) -> None:
    assert result.returncode != 0
    assert f"ERROR {code}:" in result.stderr
