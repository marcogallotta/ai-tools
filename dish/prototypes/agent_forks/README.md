# Agent forks prototype

Disposable local UI for testing direct interaction with several agent conversations and forks.
It deliberately uses only Python's standard library and an append-only JSONL file.

```sh
cd dish/prototypes/agent_forks
python3 app.py
```

Open <http://127.0.0.1:8765>. Data is restored from `data/events.jsonl` after restart.

The built-in adapter is a delayed deterministic stub so running/stopped, stop, redirect, fork,
comparison, and restart behavior can be tested without provider setup. To use a real local agent,
set one command that reads this JSON from stdin:

```json
{"prompt":"optional system prompt","messages":[{"role":"user","text":"..."}]}
```

The command must print the assistant response to stdout. Example:

```sh
FORK_AGENT_COMMAND='./my-agent-adapter' python3 app.py
```

Interaction model:

- **New conversation** creates a directly addressable root agent.
- **fork** on any message creates a child with history only through that message.
- Parent and child append to separate histories after the fork.
- **Stop** invalidates the selected branch's in-flight reply.
- **Redirect** invalidates an in-flight reply, adds a new instruction, and starts a replacement reply.
- Check branches in the right panel to compare their latest assistant outputs.
- The left tree shows ancestry and a green dot while a branch is running.

Run the focused acceptance tests with:

```sh
python3 -m unittest -v test_app.py
```

This is intentionally not integrated with Dish service state, authentication, deployment,
workflow engines, databases, analytics, monitoring, or production agent infrastructure.
