from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

from a2a.helpers import new_text_message
from a2a.types import Role

import hooks
from server import IdempotencyStore, RuntimeResult, WorkerAdapterExecutor


class RecordingEventQueue:
    def __init__(self) -> None:
        self.events = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


class FakeBridge:
    def __init__(self) -> None:
        self.calls = []

    async def invoke(self, payload):
        self.calls.append(payload)
        return RuntimeResult(True, "RUNTIME_OK", {"mode": "fake"})

    def describe(self):
        return {"mode": "fake", "configured": True, "timeout_seconds": 1}


class HooksRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hooks.clear()

    def tearDown(self) -> None:
        hooks.clear()

    async def test_register_and_emit_sync_and_async_handlers(self) -> None:
        calls: list[tuple[str, dict]] = []

        def sync_handler(**payload):
            calls.append(("sync", payload))

        async def async_handler(**payload):
            calls.append(("async", payload))

        hooks.register("on_task_created", sync_handler)
        hooks.register("on_task_created", async_handler)
        await hooks.emit("on_task_created", task_id="T-1", context_id="C-1")
        self.assertEqual([name for name, _ in calls], ["sync", "async"])
        self.assertEqual(calls[0][1]["task_id"], "T-1")

    async def test_decorator_registration_and_dedupe(self) -> None:
        @hooks.register("before_dispatch")
        def handler(**payload):
            pass

        hooks.register("before_dispatch", handler)  # same fn again: no dupe
        self.assertEqual(len(hooks.registered("before_dispatch")), 1)

    def test_register_unknown_event_raises(self) -> None:
        with self.assertRaises(ValueError):
            hooks.register("on_nonsense", lambda **_: None)

    async def test_emit_unknown_event_is_safe(self) -> None:
        await hooks.emit("on_nonsense", anything=1)  # must not raise

    async def test_handler_exception_is_swallowed_and_others_still_run(self) -> None:
        calls: list[str] = []

        def bad(**payload):
            raise RuntimeError("boom")

        def good(**payload):
            calls.append("good")

        hooks.register("after_dispatch", bad)
        hooks.register("after_dispatch", good)
        with self.assertLogs("a2a_adapter.hooks", level="ERROR"):
            await hooks.emit("after_dispatch", envelope={}, result=None)
        self.assertEqual(calls, ["good"])

    async def test_unregister_and_clear(self) -> None:
        def handler(**payload):
            pass

        hooks.register("on_task_completed", handler)
        self.assertTrue(hooks.unregister("on_task_completed", handler))
        self.assertFalse(hooks.unregister("on_task_completed", handler))
        hooks.register("on_task_completed", handler)
        hooks.clear("on_task_completed")
        self.assertEqual(hooks.registered("on_task_completed"), ())

    async def test_load_module_from_file_auto_registers(self) -> None:
        source = (
            "CALLS = []\n"
            "def on_task_completed(task_id, ok, details, **_):\n"
            "    CALLS.append(task_id)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "my_hooks.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)
            module = hooks.load_module(path)
            self.assertEqual(len(hooks.registered("on_task_completed")), 1)
            await hooks.emit("on_task_completed", task_id="T-9", ok=True, details={})
            self.assertEqual(module.CALLS, ["T-9"])

    async def test_load_from_env(self) -> None:
        source = "def on_task_created(task_id, context_id, **_):\n    pass\n"
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "env_hooks.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)
            old = os.environ.get("HOOKS_MODULE")
            os.environ["HOOKS_MODULE"] = path
            try:
                self.assertTrue(hooks.load_from_env())
            finally:
                if old is None:
                    os.environ.pop("HOOKS_MODULE", None)
                else:
                    os.environ["HOOKS_MODULE"] = old
        self.assertEqual(len(hooks.registered("on_task_created")), 1)

    async def test_load_from_env_bad_module_does_not_raise(self) -> None:
        old = os.environ.get("HOOKS_MODULE")
        os.environ["HOOKS_MODULE"] = os.path.join(tempfile.gettempdir(), "definitely_missing_hooks.py")
        try:
            self.assertFalse(hooks.load_from_env())
        finally:
            if old is None:
                os.environ.pop("HOOKS_MODULE", None)
            else:
                os.environ["HOOKS_MODULE"] = old


class HooksIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hooks.clear()
        self.fired: list[str] = []
        for event in hooks.EVENTS:
            hooks.register(event, self._recorder(event))

    def tearDown(self) -> None:
        hooks.clear()

    def _recorder(self, event: str):
        def _record(**payload):
            self.fired.append(event)

        return _record

    async def test_executor_fires_full_hook_sequence(self) -> None:
        bridge = FakeBridge()
        message = new_text_message(
            json.dumps({"task_id": "T-1", "worker_id": "worker-a", "message": "run"}),
            role=Role.ROLE_USER,
        )
        context = SimpleNamespace(message=message, current_task=None)
        with tempfile.TemporaryDirectory() as td:
            executor = WorkerAdapterExecutor(
                bridge, result_store=IdempotencyStore(os.path.join(td, "idem.db"))
            )
            await executor.execute(context, RecordingEventQueue())
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(
            self.fired,
            [
                "on_task_created",
                "on_message_received",
                "on_task_state_changed",  # WORKING
                "before_dispatch",
                "after_dispatch",
                "on_task_state_changed",  # COMPLETED
                "on_task_completed",
            ],
        )

    async def test_broken_hook_does_not_break_execution(self) -> None:
        def boom(**payload):
            raise RuntimeError("hook exploded")

        hooks.register("before_dispatch", boom)
        bridge = FakeBridge()
        message = new_text_message(
            json.dumps({"task_id": "T-2", "worker_id": "worker-a", "message": "run"}),
            role=Role.ROLE_USER,
        )
        context = SimpleNamespace(message=message, current_task=None)
        queue = RecordingEventQueue()
        with tempfile.TemporaryDirectory() as td:
            executor = WorkerAdapterExecutor(
                bridge, result_store=IdempotencyStore(os.path.join(td, "idem.db"))
            )
            await executor.execute(context, queue)
        self.assertEqual(len(bridge.calls), 1)
        self.assertIn("on_task_completed", self.fired)


if __name__ == "__main__":
    unittest.main()
