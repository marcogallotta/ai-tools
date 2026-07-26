# ChatGPT relay for the local dish workflow

ChatGPT does not access Asana directly. A local runner invokes `dish` and attributes the work to the ChatGPT run with `--agent gpt`. Command syntax, result codes, retries, and troubleshooting live only in `runtime-contract.md`.

1. The runner starts the exact task and stage before ChatGPT authors or verifies anything. For Verification, supply a platform run ID when available; otherwise record an explicit independence attestation.
2. Relay only the stage protocol and exact live task returned by the tool. Do not expose another stage’s protocol, the generic Asana CLI, or `dish-admin`.
3. ChatGPT returns one complete candidate when a candidate is required. The runner must not invent title fields, provenance, corrections, Evidence/Human reasons, or Material changes.
4. The runner invokes the stage boundary command against that candidate and relays the complete JSON response unchanged.
5. A tool pass establishes deterministic conformance only. ChatGPT still performs every semantic and provenance duty in the governing protocol.
6. Agent-correctable findings are corrected by ChatGPT only when the protocol assigns them to that stage; the runner then reruns the same boundary.
7. A tool error or ambiguous outcome is reported as a tooling failure. It is never converted into Evidence or Human Review merely because the tool failed.
8. On tool/protocol disagreement, preserve the live task, stop the transition, and report the defect; the protocol wins.
9. After successful Verification approval, the response names `submit` as the next action. The verifier has the runner execute it in the same pass. Signoff and movement remain separately recoverable.
10. Stop and give Marco the complete JSON result for Human action, uncertain outcome, migration requirement, or any administrative recovery need.
