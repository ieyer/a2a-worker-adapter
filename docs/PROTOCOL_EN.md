# 07_A2A Real-time Communication and Agent Onboarding Protocol | V1.0

> This document is the English translation of the Chinese version in [docs/PROTOCOL.md](PROTOCOL.md); the two versions have an identical structure.

Status: APPROVED / Owner: A2A Fleet Maintainers
Applies to: All APPROVED/ACTIVE Workers, and all Workers added in the future

# 1. Goals and Principles

This protocol upgrades the multi-Worker system from "robots polling a Google Sheet on a timer" to a real-time collaboration architecture of "A2A proactive push + task ledger records + file delivery".
Primary real-time channel: A2A.
Secondary control and tool channels: MCP / Drive / Sheets / Docs.
Fallback channel: a zero-Token deterministic Poller, used only when A2A is unavailable or a Worker does not have real-time wake capability.
Google Sheets is the System of Record, and is no longer used as a high-frequency message queue.
No idle listener may call an LLM; an Agent may only be woken after a valid task has been received and successfully claimed.

# 2. Role Model

Commander: Responsible for task decomposition, Worker selection, A2A routing, acceptance, and final aggregation.
A2A Gateway: Responsible for discovering the Agent Card, protocol version negotiation, sending tasks, receiving status/Artifact, retries, authentication, and audit.
Worker A2A Adapter: The local adaptation layer for each Worker, responsible for converting an A2A Task into that Worker's real wake/invoke call.
Worker Agent: Actually performs work such as research, browser, code, documents, and automation.
Task Dashboard: The authoritative ledger for tasks and Worker status.
Google Drive: The authoritative file storage for Context, Working, Deliverables, Evidence, and Worker Memory.
EVENTS: The end-to-end event log.

# 3. Minimum Requirements for A2A Onboarding

Every Worker must register:
A2A_ENABLED
AGENT_CARD_URL
A2A_ENDPOINT
A2A_BINDING
A2A_VERSION
WAKE_MODE
A2A_STATUS
The Agent Card must declare at least identity, supportedInterfaces, capabilities, skills, and version.
Production interfaces should prefer HTTPS.
The protocol baseline uses A2A 1.0; client requests must carry A2A-Version: 1.0.
Unauthenticated public execution interfaces are prohibited in production; a Demo may be unauthenticated, but must not carry sensitive information or real tasks.

# 4. Unified Task Dispatch Flow

1. Commander creates an authoritative task row in TASKS, writing TASK_ID, ASSIGNED_WORKER, STATUS=READY, Context/input links, and DONE_CRITERIA.
2. Router reads the A2A status from WORKERS.
3. If A2A_STATUS=READY/PRODUCTION, Router confirms the endpoint, binding, version, and skills via the Agent Card.
4. Router sends an A2A SendMessage operation. messageId must be unique, and must carry the minimum necessary metadata such as TASK_ID, CONTEXT_VERSION, CLASSIFICATION, INPUT_LINK, CONTEXT_LINK, and DONE_CRITERIA.
5. After receiving the request, the Worker Adapter must re-read the corresponding row in TASKS and confirm that the task is still READY and ASSIGNED_WORKER is itself.
6. After the Worker successfully claims the task, it updates TASKS: READY→RUNNING, ATTEMPTS+1, HEARTBEAT=now, CHECKPOINT=STARTED, and writes EVENTS: A2A_ACK / TASK_STARTED.
7. Only then may the Adapter call the real Worker runtime; it is prohibited for the A2A Server to merely return "received" without waking the real Agent.
8. During execution, the Worker returns status using streaming or push notification; key statuses are also written to EVENTS at the same time.
9. Results should be delivered as an Artifact/file, with OUTPUT_LINK/EVIDENCE_LINK written back to TASKS; after execution completes, STATUS=VERIFYING.
10. After Commander acceptance passes, STATUS=DONE.

# 5. Mapping Between A2A Status and TASKS Status

A2A request accepted: TASKS remains READY, until the Worker completes the claim.
A2A task working: TASKS=RUNNING.
A2A input/auth required: TASKS=BLOCKED, CHECKPOINT=WAITING_EXTERNAL.
A2A completed: After the Worker writes OUTPUT/EVIDENCE, TASKS=VERIFYING; it must not write DONE itself.
A2A failed/rejected/canceled: Mapped to FAILED/BLOCKED/CANCELLED, and Error, Context, and Evidence are retained.
An A2A Artifact is the result carrier; an ordinary Message is not the sole formal delivery.

# 6. Real-time Updates and Reliability

Short tasks should prefer synchronous SendMessage.
Long tasks should prefer asynchronous Task + streaming or push notification.
All requests use a unique messageId, and the Worker should perform idempotency deduplication to avoid repeating irreversible actions.
After a network interruption, it must not be assumed that a message definitely arrived; the Router must check TASKS/EVENTS/A2A task state before deciding whether to resend.
Key results must land in TASKS/Drive/EVENTS, and must not exist only in a temporary stream or a private session.
Only when the A2A channel is abnormal does it enter FALLBACK_POLLER; A2A and the Poller must not contend for the same READY Task at the same time.

# 7. Fallback Poller

The Fallback Poller must be a deterministic script and must not call an LLM.
The default frequency is determined by the Worker's own infrastructure capability, and is no longer forced to 5 minutes for all Workers.
If a Worker platform's timer would directly wake an LLM, then that timer does not belong to the Zero-Token Poller.
Recommended: system-level cron/systemd/hook/sidecar may be 1–5 minutes; Workers that can only use a vendor Agent Timer may be relaxed to 15 minutes or a lower frequency.
Once A2A real-time wake is verified, that Worker's Poller is downgraded to health-check / disaster fallback and does not participate in normal task contention.

# 8. Security and Information Classification

P0/P1 may be sent over an authorized A2A channel with the minimum necessary Context.
P2 must be explicitly authorized, and should prefer sending references/short-lived credentials rather than copying full content.
P3 must never go through a shared A2A, Sheet, Drive, or public endpoint.
A production Agent Card should not expose secrets.
An A2A endpoint must implement authentication, authorization, request origin validation, rate limiting, audit, and replay protection.
A public Demo endpoint may only be used for connectivity testing.

# 9. Worker Go-live Acceptance

A Worker's A2A_STATUS may be set to PRODUCTION only when all of the following conditions are met at the same time:
1. The Agent Card is accessible from the control plane.
2. The protocol version and binding are negotiable.
3. An external SendMessage can reach that Worker.
4. The Adapter can really wake/invoke the Worker runtime.
5. It can complete a Canary Task: READY→RUNNING→VERIFYING.
6. It can return an Artifact or OUTPUT_LINK.
7. EVENTS contains A2A_DISPATCHED, A2A_ACK, TASK_STARTED, OUTPUT_READY.
8. Authentication is enabled; a public unauthenticated Demo must not be marked PRODUCTION.
9. A repeated messageId will not repeat side effects.
10. Fallback behavior when A2A is unavailable is explicit.

# 10. Current Migration Strategy

Phase A: The first Worker (worker-a) Echo Demo serves only as an A2A connectivity proof, with status marked DEMO_OK / REAL_WAKE_PENDING.
Phase B: The worker-a Adapter connects to a real runtime and completes the first real Canary.
Phase C: The remaining Workers each build an Adapter and register an Agent Card.
Phase D: The Commander Router prefers A2A Dispatch; the Poller is downgraded to fallback.
Phase E: Non-ACTIVE Workers, when converted to active, must complete the A2A self-reported fields or explicitly mark A2A_UNAVAILABLE.

# 11. Unified Execution Instructions

All ACTIVE Workers immediately execute their respective A2A onboarding task.
They must not modify another Worker's Agent Card, Endpoint, or identity on their own.
They must not claim to be "already connected to A2A" with only an Echo; they must distinguish DISCOVERY_OK, DEMO_OK, REAL_WAKE_OK, PRODUCTION.
If a Worker cannot directly wake its own Agent runtime, it must write the limitation into A2A_STATUS and Evidence, and propose an executable Adapter plan.
All go-live evidence goes into the corresponding Task Folder / 30_Evidence.

# 12. Completion Criteria

The final system should satisfy: after a task is created, the Commander proactively pushes via A2A; the normal path does not depend on a 5/15-minute LLM Timer; Workers receive tasks within seconds; the real Agent is woken to execute; status and Artifact are automatically written back; Sheet/Drive maintain authoritative records and audit; the zero-Token fallback is enabled only when A2A fails.
