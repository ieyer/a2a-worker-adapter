"""Authenticated A2A 1.0 client for A2A worker adapters."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.helpers import new_text_message
from a2a.types import Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict


def proto_json(message: Any) -> dict[str, Any]:
    return MessageToDict(message, preserving_proto_field_name=True)


def build_message(args: argparse.Namespace) -> str:
    if args.raw:
        return args.message
    if not args.task_id:
        return args.message
    envelope: dict[str, Any] = {
        "task_id": args.task_id,
        "worker_id": args.worker_id,
        "message": args.message,
    }
    for key in ("context_link", "input_link", "done_criteria", "classification", "context_version"):
        value = getattr(args, key)
        if value:
            envelope[key] = value
    return json.dumps(envelope, ensure_ascii=False)


def stable_message_id(*, worker_id: str, task_id: str | None, context_version: str | None, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if not task_id:
        return None
    version = context_version or "1"
    return f"a2a:{worker_id}:{task_id}:v{version}"


async def send_message(
    base_url: str,
    text: str,
    *,
    token: str | None = None,
    streaming: bool = True,
    timeout: float = 30.0,
    message_id: str | None = None,
) -> list[dict[str, Any]]:
    headers = {"A2A-Version": "1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    http_client = httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False)
    client = None
    try:
        card = await A2ACardResolver(http_client, base_url).get_agent_card()
        print(
            json.dumps(
                {
                    "kind": "agent_card",
                    "name": card.name,
                    "version": card.version,
                    "supported_interfaces": [
                        {
                            "protocol_binding": item.protocol_binding,
                            "protocol_version": item.protocol_version,
                            "url": item.url,
                        }
                        for item in card.supported_interfaces
                    ],
                },
                ensure_ascii=False,
            )
        )
        factory = ClientFactory(
            ClientConfig(
                httpx_client=http_client,
                streaming=streaming,
                supported_protocol_bindings=["JSONRPC"],
            )
        )
        client = factory.create(card)
        message = new_text_message(text, role=Role.ROLE_USER)
        if message_id:
            message.message_id = message_id
        request = SendMessageRequest(message=message)
        events: list[dict[str, Any]] = []
        async for response in client.send_message(request):
            event_kind = response.WhichOneof("payload")
            event = {"kind": event_kind, **proto_json(response)}
            events.append(event)
            print(json.dumps(event, ensure_ascii=False))
        return events
    finally:
        if client is not None:
            await client.close()
        else:
            await http_client.aclose()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Discover an A2A Agent Card and send a worker task.")
    parser.add_argument("--url", default="http://127.0.0.1:8000/")
    parser.add_argument("--token", default=os.getenv("A2A_BEARER_TOKEN"))
    parser.add_argument("--message", default="status check")
    parser.add_argument("--task-id")
    parser.add_argument("--worker-id", default=os.getenv("A2A_WORKER_ID", "worker-a"))
    parser.add_argument("--context-link")
    parser.add_argument("--input-link")
    parser.add_argument("--done-criteria")
    parser.add_argument("--classification")
    parser.add_argument("--context-version")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--message-id", help="Explicit A2A messageId. Defaults to a stable ID derived from worker/task/context version.")
    parser.add_argument("--non-streaming", action="store_true")
    args = parser.parse_args()
    await send_message(
        args.url,
        build_message(args),
        token=args.token,
        streaming=not args.non_streaming,
        message_id=stable_message_id(
            worker_id=args.worker_id,
            task_id=args.task_id,
            context_version=args.context_version,
            explicit=args.message_id,
        ),
    )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
