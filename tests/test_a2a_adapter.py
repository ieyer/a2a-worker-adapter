from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

import httpx
from a2a.helpers import get_artifact_text, new_text_message
from a2a.types import Role, Task, TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent
from google.protobuf.json_format import MessageToDict

from server import (
    IdempotencyStore,
    RuntimeResult,
    WorkerAdapterExecutor,
    WorkerRuntimeBridge,
    _idempotency_payload_hash,
    create_app,
    parse_task_envelope,
)


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
        return RuntimeResult(True, f"RUNTIME_OK:{payload.get('message', '')}", {"mode": "fake"})

    def describe(self):
        return {"mode": "fake", "configured": True, "timeout_seconds": 1}


class AdapterExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_invokes_bridge_and_completes(self) -> None:
        bridge = FakeBridge()
        message = new_text_message(
            json.dumps({"task_id": "T-1", "worker_id": "worker-a", "message": "run"}),
            role=Role.ROLE_USER,
        )
        context = SimpleNamespace(message=message, current_task=None)
        queue = RecordingEventQueue()
        with tempfile.TemporaryDirectory() as td:
            executor = WorkerAdapterExecutor(bridge, result_store=IdempotencyStore(os.path.join(td, "idem.db")))
            await executor.execute(context, queue)
        self.assertEqual(len(bridge.calls), 1)
        self.assertIsInstance(queue.events[0], Task)
        self.assertIsInstance(queue.events[1], TaskStatusUpdateEvent)
        self.assertEqual(queue.events[1].status.state, TaskState.TASK_STATE_WORKING)
        self.assertIsInstance(queue.events[2], TaskArtifactUpdateEvent)
        self.assertIn("RUNTIME_OK:run", get_artifact_text(queue.events[2].artifact))
        self.assertEqual(queue.events[3].status.state, TaskState.TASK_STATE_COMPLETED)

    async def test_persistent_idempotency_survives_new_executor(self) -> None:
        bridge = FakeBridge()
        message = new_text_message(
            json.dumps({"task_id": "T-1", "worker_id": "worker-a", "message": "run"}),
            role=Role.ROLE_USER,
        )
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "idem.db")
            q1 = RecordingEventQueue()
            await WorkerAdapterExecutor(bridge, result_store=IdempotencyStore(db)).execute(
                SimpleNamespace(message=message, current_task=None), q1
            )
            q2 = RecordingEventQueue()
            await WorkerAdapterExecutor(bridge, result_store=IdempotencyStore(db)).execute(
                SimpleNamespace(message=message, current_task=None), q2
            )
        self.assertEqual(len(bridge.calls), 1)
        self.assertIn('"cached": true', get_artifact_text(q2.events[2].artifact).lower())

    def test_payload_hash_excludes_transport_uuid(self) -> None:
        # v2.2.1: server-generated a2a_task_id must not affect the hash.
        env_a = {"message_id": "MID-1", "message": "run", "a2a_task_id": "UUID-A"}
        env_b = {"message_id": "MID-1", "message": "run", "a2a_task_id": "UUID-B"}
        self.assertEqual(_idempotency_payload_hash(env_a), _idempotency_payload_hash(env_b))

    async def test_replay_with_new_transport_task_uuid_returns_cached_result(self) -> None:
        # v2.2.1: same stable message_id + same business envelope, but the A2A
        # server minted a new Task (new transport UUID) -> must return the
        # cached prior result, NOT IDEMPOTENCY_CONFLICT.
        bridge = FakeBridge()
        body = json.dumps({"task_id": "T-1", "worker_id": "worker-a", "message": "run"})
        message = new_text_message(body, role=Role.ROLE_USER)
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "idem.db")
            q1 = RecordingEventQueue()
            await WorkerAdapterExecutor(bridge, result_store=IdempotencyStore(db)).execute(
                SimpleNamespace(message=message, current_task=None), q1
            )
            q2 = RecordingEventQueue()
            await WorkerAdapterExecutor(bridge, result_store=IdempotencyStore(db)).execute(
                SimpleNamespace(message=message, current_task=None), q2
            )
        self.assertEqual(len(bridge.calls), 1)
        artifact = get_artifact_text(q2.events[2].artifact)
        self.assertIn('"cached": true', artifact.lower())
        self.assertNotIn("IDEMPOTENCY_CONFLICT", artifact)
        self.assertIn("RUNTIME_OK:run", artifact)

    async def test_business_field_change_still_conflicts(self) -> None:
        # v2.2.1: same message_id but a changed business field must still
        # produce IDEMPOTENCY_CONFLICT.
        bridge = FakeBridge()
        with tempfile.TemporaryDirectory() as td:
            ex = WorkerAdapterExecutor(bridge, result_store=IdempotencyStore(os.path.join(td, "idem.db")))
            env1 = {"task_id": "T-1", "message_id": "MID-1", "worker_id": "worker-a",
                    "message": "run", "a2a_task_id": "UUID-A"}
            env2 = {"task_id": "T-1", "message_id": "MID-1", "worker_id": "worker-a",
                    "message": "CHANGED", "a2a_task_id": "UUID-B"}
            r1, cached1 = await ex._execute_once("MID-1", _idempotency_payload_hash(env1), env1)
            r2, cached2 = await ex._execute_once("MID-1", _idempotency_payload_hash(env2), env2)
        self.assertFalse(cached1)
        self.assertTrue(cached2)
        self.assertEqual(r2.details.get("code"), "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(bridge.calls), 1)

    async def test_command_bridge_reads_stdin_and_returns_json(self) -> None:
        code = "import json,sys; p=json.load(sys.stdin); print(json.dumps({'ok': True, 'output': 'CMD:'+p['message']}))"
        bridge = WorkerRuntimeBridge(mode="command", command=f"{sys.executable} -c {json.dumps(code)}", timeout=5)
        result = await bridge.invoke({"message": "hello"})
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "CMD:hello")

    def test_rejects_wrong_worker(self) -> None:
        with self.assertRaises(ValueError):
            parse_task_envelope(json.dumps({"worker_id": "worker-b", "message": "x"}))

    def test_rejects_oversized_message(self) -> None:
        with self.assertRaises(ValueError):
            parse_task_envelope("x" * 20, max_message_bytes=10)

    def test_http_bridge_defaults_to_loopback_only(self) -> None:
        with self.assertRaises(ValueError):
            WorkerRuntimeBridge(mode="http", url="https://example.com/invoke")


class HttpProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bridge = FakeBridge()
        self.tmp = tempfile.TemporaryDirectory()
        app = create_app(
            "http://127.0.0.1:8000/",
            bridge=self.bridge,
            bearer_token="secret",
            allow_unauthenticated=False,
            idempotency_db=os.path.join(self.tmp.name, "idem.db"),
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.tmp.cleanup()

    async def test_agent_card_is_public_and_declares_bearer(self) -> None:
        response = await self.client.get("/.well-known/agent-card.json")
        self.assertEqual(response.status_code, 200)
        card = response.json()
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertEqual(card["skills"][0]["id"], "dispatch_assigned_task")
        self.assertIn("bearerAuth", card["securitySchemes"])
        self.assertTrue(card["securityRequirements"])

    async def test_jsonrpc_requires_bearer_token(self) -> None:
        message = MessageToDict(new_text_message("unit test", role=Role.ROLE_USER))
        response = await self.client.post(
            "/",
            json={"jsonrpc": "2.0", "id": "unit-auth", "method": "SendMessage", "params": {"message": message}},
            headers={"A2A-Version": "1.0"},
        )
        self.assertEqual(response.status_code, 401)

    async def test_jsonrpc_with_token_returns_task(self) -> None:
        message = MessageToDict(
            new_text_message(
                json.dumps({"task_id": "T-2", "worker_id": "worker-a", "message": "unit"}),
                role=Role.ROLE_USER,
            )
        )
        response = await self.client.post(
            "/",
            json={"jsonrpc": "2.0", "id": "unit-ok", "method": "SendMessage", "params": {"message": message}},
            headers={"A2A-Version": "1.0", "Authorization": "Bearer secret"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertNotIn("error", payload)
        self.assertIn("task", payload["result"])


if __name__ == "__main__":
    unittest.main()
