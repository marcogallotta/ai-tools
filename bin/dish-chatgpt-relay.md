# ChatGPT dish relay

ChatGPT cannot run the local `dish` CLI. A local runner performs the commands while attributing the
work to the agent that actually authored or verified it.

1. Before ChatGPT authors anything, run the appropriate `dish start ... --agent gpt` command.
2. Give ChatGPT the complete frozen release returned by `start`, including every returned protocol
   and manifest. Do not bind an already-authored file retroactively.
3. ChatGPT returns one complete candidate file. Except for planning, it includes its own
   `Self-verified: gpt, <date>` line; the local runner must not add or backfill that line.
4. Run `dish prepare <submission-id> --agent gpt --file <candidate-file>`.
5. For initial or large work, relay the exact complete file to a Claude-family verifier, who uses the
   normal `dish approve` or `dish reject` workflow.
6. When the submission is `ready`, run
   `dish submit <submission-id> --file <approved-complete-candidate-file>` exactly once.

Use `dish inspect <submission-id> --agent gpt` to relay state, the frozen bundle, routing, and legal
next actions. Stop on Human-action, uncertain, or terminal outcomes and give Marco the complete JSON
result.
