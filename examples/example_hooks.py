"""Example hooks module for the A2A worker adapter.

Activate it by pointing the adapter at this file:

    HOOKS_MODULE=examples/example_hooks.py python server.py ...

What it does:

* Appends one JSON line per task event to a log file
  (``A2A_HOOK_LOG``, default ``./a2a-hook-events.log``).
* Optionally forwards task completions to a webhook
  (``EXAMPLE_HOOK_WEBHOOK_URL``; left empty by default, so nothing is
  sent anywhere unless you configure it).

Functions named after hook events are registered automatically; the
``register`` calls at the bottom show the explicit alternative.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

LOG_PATH = Path(os.getenv("A2A_HOOK_LOG", "./a2a-hook-events.log"))
WEBHOOK_URL = os.getenv("EXAMPLE_HOOK_WEBHOOK_URL", "")  # e.g. https://example.com/hook


def _append_log(event: str, payload: dict[str, Any]) -> None:
    record = {"ts": time.time(), "event": event, **payload}
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def on_task_created(task_id: str, context_id: str, **_: Any) -> None:
    _append_log("on_task_created", {"task_id": task_id, "context_id": context_id})


def on_task_state_changed(task_id: str, state: str, cached: bool = False, **_: Any) -> None:
    _append_log(
        "on_task_state_changed",
        {"task_id": task_id, "state": state, "cached": cached},
    )


def on_task_completed(task_id: str, ok: bool, details: dict[str, Any], **_: Any) -> None:
    _append_log("on_task_completed", {"task_id": task_id, "ok": ok, "details": details})
    if WEBHOOK_URL:
        try:
            httpx.post(
                WEBHOOK_URL,
                json={"task_id": task_id, "ok": ok, "details": details},
                timeout=5.0,
            )
        except Exception:
            # Example code only: the adapter also isolates hook failures,
            # but a well-behaved hook should not rely on that.
            pass
