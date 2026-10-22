# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-10-22

### Added

- Event hooks system with six events — `on_message_received`, `on_task_created`, `on_task_state_changed`, `on_task_completed`, `before_dispatch`, and `after_dispatch`. Hooks are lightweight extension points for logging, metrics, webhooks, and audit; a failing hook is logged and swallowed and never breaks task execution. Additional hook modules can be loaded via the `HOOKS_MODULE` environment variable (module name or `.py` file path), with functions named after the events registered automatically.
- Deterministic A2A 1.0 dispatch built on the official `a2a-sdk`: A2A JSON-RPC `SendMessage` handling with a public Agent Card and stable `messageId` derivation for retry-safe dispatch.
- SQLite-backed persistent idempotency keyed by `(worker_id, messageId)`: replays with the same stable `messageId` return the cached result even across restarts and even when the A2A transport mints a new Task UUID; the same `messageId` with a different business payload is rejected with `IDEMPOTENCY_CONFLICT`.
- In-flight duplicate suppression: concurrent duplicates of one `messageId` execute the runtime exactly once.
- Three runtime wake bridges (runtime bridge modes): `command` (fixed executable, no shell, task JSON on stdin / result JSON on stdout), `http` (POST to a local runtime endpoint, loopback-only unless explicitly allowed), and `spool` (atomic file drop in an inbox directory with result pickup from an outbox).

### Changed

- Bearer authentication is now enforced on every route except the public Agent Card (`/.well-known/agent-card.json`) and `/healthz`; the Agent Card declares the Bearer security scheme.
- Addressing safety: tasks addressed to a different `worker_id` are rejected, and message size is capped via `A2A_MAX_MESSAGE_BYTES`.
