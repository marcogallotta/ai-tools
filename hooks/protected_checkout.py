"""Protocol-neutral classifier for the protected primary ai-tools checkout.

The classifier returns a human-readable denial reason or ``None``.  Host
adapters own their hook wire formats.  This is intentionally a command-text
guardrail, not a process sandbox: it handles direct and visible nested Git
invocations, but cannot inspect arbitrary opaque child-process behavior.
"""

import os
import re
import shlex
import subprocess


DEFAULT_PROTECTED_CHECKOUT_ROOT = os.path.realpath(os.path.expanduser("~/ai-tools"))
GIT_ENV_LOCATION_OVERRIDE_RE = re.compile(r"^(GIT_DIR|GIT_WORK_TREE|GIT_COMMON_DIR)=(.*)$")
SHELL_EXPANSION_CHARS = "$`*?["
GIT_CONFIG_VALUE_OPTS = ("-c", "--config-env")
GIT_LOCATION_VALUE_OPTS = ("-C", "--git-dir", "--work-tree")
SHELL_COMMANDS = {"bash", "dash", "fish", "ksh", "sh", "zsh"}
COMMAND_WRAPPERS = {"command", "exec", "nohup", "sudo"}
CONTROL_PREFIXES = {"!", "do", "elif", "else", "if", "then", "time", "until", "while"}


def split_segments(command):
    """Split top-level shell commands without splitting quoted payloads."""
    segments = []
    segment = []
    in_single = in_double = False
    i = 0
    while i < len(command):
        char = command[i]
        if char == "'" and not in_double:
            in_single = not in_single
            segment.append(char)
        elif char == '"' and not in_single:
            in_double = not in_double
            segment.append(char)
        elif not in_single and not in_double and char == "\\" and i + 1 < len(command) and command[i + 1] == "\n":
            i += 2
            continue
        elif not in_single and not in_double and char in ";|&\n":
            segments.append("".join(segment))
            segment = []
        else:
            segment.append(char)
        i += 1
    segments.append("".join(segment))
    return segments


def basename_token(token):
    value = token
    while value and value[0] in "(`{":
        value = value[1:]
    while value and value[-1] in ")`}":
        value = value[:-1]
    return value.rsplit("/", 1)[-1] if value else value


def _classify_tokens(segment):
    """Return quote-removed tokens plus expansion-eligible characters."""
    tokens = []
    text = []
    active = set()
    in_single = in_double = escaped = started = False
    i = 0
    while i < len(segment):
        char = segment[i]
        if escaped:
            text.append(char)
            escaped = False
            started = True
            i += 1
            continue
        if char == "\\" and not in_single:
            escaped = True
            started = True
            i += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            started = True
            i += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            started = True
            i += 1
            continue
        if not in_single and not in_double and char.isspace():
            if started:
                tokens.append(("".join(text), active))
                text, active, started = [], set(), False
            i += 1
            continue
        if in_double and char in "$`":
            active.add(char)
        elif not in_single and not in_double and char in SHELL_EXPANSION_CHARS:
            active.add(char)
        text.append(char)
        started = True
        i += 1
    if started:
        tokens.append(("".join(text), active))
    return tokens


def _reject_shell_expansion(text, active_chars, chars_to_check=SHELL_EXPANSION_CHARS):
    if active_chars & set(chars_to_check):
        return None
    return os.path.expanduser(text)


def _env_location_overrides(pairs):
    extra_env = {}
    ambiguous = False
    for text, active in pairs:
        match = GIT_ENV_LOCATION_OVERRIDE_RE.match(text)
        if match is None:
            continue
        name, value = match.group(1), match.group(2)
        resolved = _reject_shell_expansion(value, active, chars_to_check="$`") if value else None
        if resolved is None:
            ambiguous = True
        else:
            extra_env[name] = resolved
    return extra_env, ambiguous


def _command_environment(pairs):
    """Resolve visible command-prefix assignments for Git probe subprocesses."""
    environment = {}
    ambiguous_names = set()
    for text, active in pairs:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", text)
        if match is None:
            continue
        name, value = match.group(1), match.group(2)
        resolved = _reject_shell_expansion(value, active, chars_to_check="$`")
        if resolved is None:
            ambiguous_names.add(name)
        else:
            environment[name] = resolved
    return environment, ambiguous_names


def _alias_environment_is_ambiguous(global_args, ambiguous_names, subcommand):
    if any(name.startswith("GIT_CONFIG_") for name in ambiguous_names):
        return True
    candidates = []
    index = 0
    while index < len(global_args):
        argument = global_args[index]
        if argument == "--config-env" and index + 1 < len(global_args):
            candidates.append(global_args[index + 1])
            index += 2
            continue
        if argument.startswith("--config-env="):
            candidates.append(argument.partition("=")[2])
        index += 1
    prefix = f"alias.{subcommand}="
    return any(
        candidate.startswith(prefix) and candidate[len(prefix) :] in ambiguous_names
        for candidate in candidates
    )


def _resolve_git_invocation(pairs, git_idx):
    """Return location/global args, subcommand index, and ambiguities."""
    location_args = []
    global_args = []
    ambiguous = False
    alias_config_ambiguous = False
    i = git_idx + 1
    while i < len(pairs):
        token, _active = pairs[i]
        if token in GIT_LOCATION_VALUE_OPTS:
            if i + 1 >= len(pairs):
                return location_args, global_args, None, True, alias_config_ambiguous
            value_text, value_active = pairs[i + 1]
            resolved = _reject_shell_expansion(value_text, value_active)
            if resolved is None:
                ambiguous = True
            else:
                location_args += [token, resolved]
                global_args += [token, resolved]
            i += 2
            continue
        if any(token.startswith(option + "=") for option in GIT_LOCATION_VALUE_OPTS):
            flag, _, value = token.partition("=")
            resolved = _reject_shell_expansion(value, pairs[i][1])
            if resolved is None:
                ambiguous = True
            else:
                argument = f"{flag}={resolved}"
                location_args.append(argument)
                global_args.append(argument)
            i += 1
            continue
        if token in GIT_CONFIG_VALUE_OPTS:
            if i + 1 >= len(pairs):
                return location_args, global_args, None, True, alias_config_ambiguous
            value, active = pairs[i + 1]
            if value.startswith("alias.") and active:
                alias_config_ambiguous = True
            global_args += [token, value]
            i += 2
            continue
        if any(token.startswith(option + "=") for option in GIT_CONFIG_VALUE_OPTS):
            if "alias." in token and pairs[i][1]:
                alias_config_ambiguous = True
            global_args.append(token)
            i += 1
            continue
        if token.startswith("-"):
            global_args.append(token)
            i += 1
            continue
        return location_args, global_args, i, ambiguous, alias_config_ambiguous
    return location_args, global_args, None, ambiguous, alias_config_ambiguous


def _branch_change_kind(args, subcommand):
    if subcommand == "checkout":
        if "--" in args:
            index = args.index("--")
            before, after = args[:index], args[index + 1 :]
            if after:
                return None
            args = before
        if any(arg in ("-b", "-B") for arg in args) or any(
            re.match(r"^-[^-]*[bB].+", arg) for arg in args
        ):
            return "checkout -b/-B (create + switch branch)"
        if any(arg == "--orphan" or arg.startswith("--orphan=") for arg in args):
            return "checkout --orphan (create + switch branch)"
        if any(
            arg == "--detach"
            or arg.startswith("--detach=")
            or (arg.startswith("-") and not arg.startswith("--") and "d" in arg[1:])
            for arg in args
        ):
            return "checkout --detach (detach HEAD)"
        if any(arg in ("-h", "--help") for arg in args):
            return None
        if any(not arg.startswith("-") for arg in args):
            return "checkout <ref> (branch attach/detach)"
        return None
    if "--" in args:
        before = args[: args.index("--")]
        if any(arg in ("-h", "--help") for arg in before):
            return None
        return "switch"
    if any(arg in ("-h", "--help") for arg in args):
        return None
    return "switch"


def _run_git(args, extra_env, cwd):
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5, env=env, cwd=cwd
        )
    except OSError:
        return None


def _git_rev_parse(location_args, extra_env, cwd, argument):
    result = _run_git(
        [*location_args, "rev-parse", "--path-format=absolute", argument], extra_env, cwd
    )
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _resolve_repo_identity(location_args, extra_env, cwd):
    toplevel = _git_rev_parse(location_args, extra_env, cwd, "--show-toplevel")
    git_dir = _git_rev_parse(location_args, extra_env, cwd, "--git-dir")
    common_dir = _git_rev_parse(location_args, extra_env, cwd, "--git-common-dir")
    if None in (toplevel, git_dir, common_dir):
        return None
    return toplevel, git_dir, common_dir


def _is_protected_primary(identity, protected_root):
    if identity is None:
        return False
    _toplevel, git_dir, common_dir = identity
    protected_git_dir = os.path.realpath(os.path.join(protected_root, ".git"))
    return (
        os.path.realpath(common_dir) == protected_git_dir
        and os.path.realpath(git_dir) == os.path.realpath(common_dir)
    )


def _alias_value(global_args, extra_env, cwd, name):
    result = _run_git([*global_args, "config", "--get", f"alias.{name}"], extra_env, cwd)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def _deny_branch(subcommand, kind, protected_root):
    return (
        f"[protected-checkout] Refusing '{subcommand}' branch change ({kind}) in the "
        f"primary {protected_root} checkout - this is the shared operator checkout, "
        "not an isolated agent worktree. Create/enter an owned linked worktree and "
        "retry the branch change there."
    )


def _deny_ambiguous(subcommand):
    return (
        f"[protected-checkout] This git {subcommand} branch change has an unresolvable "
        "repository-location override. Refusing to guess whether it targets the "
        "protected shared checkout. Create/enter an owned linked worktree and retry."
    )


def _deny_alias_ambiguous(subcommand):
    return (
        f"[protected-checkout] Git alias '{subcommand}' is supplied through shell-expanded "
        "configuration, so its operation cannot be resolved safely. Refusing to guess "
        "whether it changes the protected shared checkout. Create/enter an owned linked "
        "worktree and retry."
    )


def _classify_git(
    pairs, git_idx, cwd, protected_root, depth, seen_aliases, inherited_env
):
    location_args, global_args, sub_idx, ambiguous, alias_config_ambiguous = (
        _resolve_git_invocation(pairs, git_idx)
    )
    if sub_idx is None:
        return None
    subcommand = basename_token(pairs[sub_idx][0])
    args = [token for token, _active in pairs[sub_idx + 1 :]]
    prefix_pairs = pairs[:git_idx]
    command_env, ambiguous_env_names = _command_environment(prefix_pairs)
    location_env, env_ambiguous = _env_location_overrides(prefix_pairs)
    extra_env = {**inherited_env, **command_env, **location_env}

    if subcommand in ("checkout", "switch"):
        kind = _branch_change_kind(args, subcommand)
        if kind is None:
            return None
        if ambiguous or env_ambiguous:
            return _deny_ambiguous(subcommand)
        identity = _resolve_repo_identity(location_args, extra_env, cwd)
        if _is_protected_primary(identity, protected_root):
            return _deny_branch(subcommand, kind, protected_root)
        return None

    if depth >= 6 or subcommand in seen_aliases:
        return None
    identity = _resolve_repo_identity(location_args, extra_env, cwd)
    alias_env_ambiguous = _alias_environment_is_ambiguous(
        global_args, ambiguous_env_names, subcommand
    )
    if (alias_config_ambiguous or alias_env_ambiguous) and _is_protected_primary(identity, protected_root):
        return _deny_alias_ambiguous(subcommand)
    alias = _alias_value(global_args, extra_env, cwd, subcommand)
    if alias is None:
        return None
    alias_cwd = identity[0] if identity is not None else cwd
    if alias.startswith("!"):
        return _classify_command(
            alias[1:],
            alias_cwd,
            protected_root,
            depth + 1,
            seen_aliases | {subcommand},
            extra_env,
        )
    try:
        expanded = shlex.split(alias, posix=True) + args
    except ValueError:
        if _is_protected_primary(identity, protected_root):
            return _deny_alias_ambiguous(subcommand)
        return None
    synthetic = [(token, set()) for token in ["git", *global_args, *expanded]]
    return _classify_git(
        synthetic,
        0,
        cwd,
        protected_root,
        depth + 1,
        seen_aliases | {subcommand},
        extra_env,
    )


def _command_index(pairs):
    index = 0
    while index < len(pairs) and basename_token(pairs[index][0]) in CONTROL_PREFIXES:
        index += 1
    while index < len(pairs) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", pairs[index][0]):
        index += 1
    while index < len(pairs) and basename_token(pairs[index][0]) in COMMAND_WRAPPERS:
        wrapper = basename_token(pairs[index][0])
        index += 1
        if wrapper == "sudo":
            while index < len(pairs) and pairs[index][0].startswith("-"):
                index += 1
        while index < len(pairs) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", pairs[index][0]):
            index += 1
    if index < len(pairs) and basename_token(pairs[index][0]) == "env":
        index += 1
        while index < len(pairs) and (
            pairs[index][0].startswith("-")
            or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", pairs[index][0])
        ):
            index += 1
    return index if index < len(pairs) else None


def _literal_cd_target(segment, cwd):
    pairs = _classify_tokens(segment)
    command_idx = _command_index(pairs)
    if command_idx is None or basename_token(pairs[command_idx][0]) != "cd":
        return None
    args = pairs[command_idx + 1 :]
    target = next(((text, active) for text, active in args if not text.startswith("-")), None)
    if target is None:
        return os.path.expanduser("~")
    text, active = target
    resolved = _reject_shell_expansion(text, active)
    if resolved is None or resolved == "-":
        return None
    if not os.path.isabs(resolved):
        resolved = os.path.join(cwd, resolved)
    return os.path.realpath(resolved)


def _shell_payload(tokens, shell_idx):
    args = tokens[shell_idx + 1 :]
    for index, arg in enumerate(args):
        if arg == "-c" or (arg.startswith("-") and "c" in arg[1:]):
            return args[index + 1] if index + 1 < len(args) else None
        if not arg.startswith("-"):
            return None
    return None


def _is_persistent_shell(tokens, shell_idx):
    args = tokens[shell_idx + 1 :]
    if any(arg == "-c" or (arg.startswith("-") and "c" in arg[1:]) for arg in args):
        return False
    return not any(not arg.startswith("-") for arg in args)


def _classify_segment(
    segment, cwd, protected_root, depth, seen_aliases, inherited_env
):
    pairs = _classify_tokens(segment)
    command_idx = _command_index(pairs)
    if command_idx is None:
        return None
    command = basename_token(pairs[command_idx][0])
    if command == "git":
        return _classify_git(
            pairs,
            command_idx,
            cwd,
            protected_root,
            depth,
            seen_aliases,
            inherited_env,
        )
    if command not in SHELL_COMMANDS:
        return None
    tokens = [token for token, _active in pairs]
    payload = _shell_payload(tokens, command_idx)
    if payload is not None and depth < 6:
        return _classify_command(
            payload,
            cwd,
            protected_root,
            depth + 1,
            seen_aliases,
            inherited_env,
        )
    if _is_persistent_shell(tokens, command_idx):
        identity = _resolve_repo_identity([], inherited_env, cwd)
        if _is_protected_primary(identity, protected_root):
            return (
                f"[protected-checkout] Refusing persistent/interactive {command} launch from "
                f"the primary {protected_root} checkout. An already-running shell would let "
                "later write_stdin input bypass PreToolUse. Start the shell in an owned linked "
                "worktree or unrelated repository instead."
            )
    return None


def _classify_command(
    command, cwd, protected_root, depth, seen_aliases, inherited_env
):
    current_cwd = cwd
    for segment in split_segments(command):
        if not segment.strip():
            continue
        reason = _classify_segment(
            segment,
            current_cwd,
            protected_root,
            depth,
            seen_aliases,
            inherited_env,
        )
        if reason:
            return reason
        changed_cwd = _literal_cd_target(segment, current_cwd)
        if changed_cwd is not None:
            current_cwd = changed_cwd
    return None


def classify(command, cwd, protected_root=None):
    """Return a hard-deny reason for a classifiable violation, else ``None``."""
    if protected_root is None:
        protected_root = DEFAULT_PROTECTED_CHECKOUT_ROOT
    root = os.path.realpath(os.path.expanduser(protected_root))
    base_dir = cwd or root
    return _classify_command(command, base_dir, root, 0, set(), {})
