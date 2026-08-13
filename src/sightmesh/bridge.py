from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets

from . import leases
from .cdesktop import CdesktopClient, CdesktopError
from .delivery import (
    DeliveryCapacityError,
    DeliveryRecord,
    DeliveryStore,
    DeliveryStoreError,
    make_record,
)
from .routing import (
    clear_peer_identity,
    enabled_workspaces,
    peer_identity,
    set_peer_identity,
)

LOGGER = logging.getLogger("sightmesh.bridge")


def _peer_name(workspace: dict[str, Any], session: dict[str, Any]) -> str:
    raw = f"cd-{workspace.get('name') or 'workspace'}-{session['id'][:6]}"
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-.")
    normalized = re.sub(r"-+", "-", normalized)
    return normalized[:48] or f"cd-session-{session['id'][:6]}"


def _backend(executor: str | None) -> str:
    return "claude-code" if executor == "CLAUDE_CODE" else "codex"


def _repo_path(client: CdesktopClient, workspace: dict[str, Any]) -> str:
    repos = client.workspace_repos(workspace["id"])
    if not repos:
        raise CdesktopError(f"Workspace {workspace['id']} has no repository")
    repo = repos[0]
    if workspace.get("use_worktree"):
        container = workspace.get("container_ref")
        if not container:
            raise CdesktopError(f"Workspace {workspace['id']} has no container path")
        return str(Path(container) / repo["name"])
    return str(Path(repo["path"]).expanduser().resolve())


@dataclass(frozen=True)
class BridgedSession:
    workspace: dict[str, Any]
    session: dict[str, Any]
    path: str


class RepowireSessionBridge:
    def __init__(
        self,
        client: CdesktopClient,
        bridged: BridgedSession,
        repowire_url: str,
        delivery_store: DeliveryStore | None = None,
    ) -> None:
        self.client = client
        self.bridged = bridged
        self.repowire_url = repowire_url
        self.delivery_store = delivery_store or DeliveryStore()
        self.assigned_name = _peer_name(bridged.workspace, bridged.session)

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connection()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("Bridge %s disconnected: %s", self.assigned_name, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)

    async def _connection(self) -> None:
        async with websockets.connect(self.repowire_url, ping_interval=20, ping_timeout=20) as ws:
            connect: dict[str, Any] = {
                "type": "connect",
                "display_name": self.assigned_name,
                "circle": "default",
                "backend": _backend(self.bridged.session.get("executor")),
                "path": self.bridged.path,
                "role": "agent",
                "hook_version": 1,
                "capabilities": ["cdesktop_followup_bridge"],
            }
            existing_peer_id = peer_identity(self.bridged.session["id"])
            if existing_peer_id:
                connect["peer_id"] = existing_peer_id
            auth_token = os.environ.get("REPOWIRE_AUTH_TOKEN")
            if auth_token:
                connect["auth_token"] = auth_token
            await ws.send(json.dumps(connect))
            connected = json.loads(await ws.recv())
            if connected.get("code") == "peer_retired" and existing_peer_id:
                clear_peer_identity(self.bridged.session["id"])
            if connected.get("type") != "connected":
                raise RuntimeError(str(connected))
            set_peer_identity(self.bridged.session["id"], connected["session_id"])
            self.assigned_name = connected.get("display_name") or self.assigned_name
            await ws.send(json.dumps({"type": "status", "status": "online", "turn_state": "idle"}))
            LOGGER.info(
                "Bridged %s to cdesktop session %s",
                self.assigned_name,
                self.bridged.session["id"],
            )
            retry_task = asyncio.create_task(self._retry_loop(ws), name=f"retry-{self.assigned_name}")
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    await self._handle(ws, message)
            finally:
                retry_task.cancel()
                try:
                    await retry_task
                except asyncio.CancelledError:
                    pass

    async def _handle(self, ws: Any, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            return
        if message_type not in {"ask", "notify", "broadcast"}:
            if message_type == "query":
                await ws.send(
                    json.dumps(
                        {
                            "type": "error",
                            "correlation_id": message.get("correlation_id"),
                            "error": "Legacy query is unsupported by the cdesktop bridge; use ask",
                        }
                    )
                )
            return

        from_peer = message.get("from_peer", "unknown")
        text = message.get("text", "")
        correlation_id = message.get("correlation_id")
        if message_type == "ask":
            question_flag = " --question" if message.get("question") else ""
            prompt = (
                f"## Request from @{from_peer}\n\n{text}\n\n"
                "When complete, send a concise result back with:\n\n"
                f"sightmesh bridge-reply {correlation_id} --from-peer {self.assigned_name}"
                f"{question_flag} --message 'RESULT'\n\n"
                "Do not delegate to hidden or native subagents."
            )
        else:
            prompt = (
                f"## {message_type.title()} from @{from_peer}\n\n{text}\n\n"
                "Do not delegate to hidden or native subagents."
            )

        record = make_record(
            session_id=self.bridged.session["id"],
            message_type=message_type,
            prompt=prompt,
            delivery_id=message.get("delivery_id"),
            correlation_id=correlation_id,
            from_peer=from_peer,
            text=text,
        )
        try:
            stored = await asyncio.to_thread(self.delivery_store.enqueue, record)
        except (DeliveryCapacityError, DeliveryStoreError) as exc:
            await self._send_delivery_failure(ws, message, str(exc))
            return
        if stored.status == "injected":
            await self._send_delivery_ack(ws, stored, "injected")
            return
        if stored.status == "inflight":
            return
        if stored.status == "dead":
            await self._send_delivery_ack(ws, stored, "dead")
            if stored.correlation_id:
                await self._send_error(ws, stored.correlation_id, stored.last_error or "dead-lettered")
            return
        claimed = await asyncio.to_thread(
            self.delivery_store.claim,
            stored.idempotency_key,
        )
        if not claimed:
            return
        await self._process_record(ws, claimed)

    async def _retry_loop(self, ws: Any) -> None:
        while True:
            await self._process_due(ws)
            await asyncio.sleep(1)

    async def _process_due(self, ws: Any) -> None:
        try:
            record = await asyncio.to_thread(
                self.delivery_store.claim_due,
                None,
                self.bridged.session["id"],
            )
        except DeliveryStoreError as exc:
            LOGGER.error("Cannot read delivery retry queue: %s", exc)
            return
        if record:
            await self._process_record(ws, record)

    async def _process_record(self, ws: Any, record: DeliveryRecord) -> None:
        if record.status != "inflight":
            await self._send_delivery_ack(ws, record, record.status)
            return
        if not record.claim_token:
            await self._send_delivery_ack(ws, record, "failed")
            if record.correlation_id:
                await self._send_error(ws, record.correlation_id, "Inflight delivery has no claim token")
            return
        if record.prompt is None:
            failed = await asyncio.to_thread(
                self.delivery_store.mark_failed,
                record.idempotency_key,
                record.claim_token,
                "Pending delivery has no retained prompt",
            )
            await self._send_delivery_ack(ws, failed, failed.status)
            return
        try:
            await asyncio.to_thread(
                self.client.send,
                record.session_id,
                record.prompt,
                None,
            )
            injected = await asyncio.to_thread(
                self.delivery_store.mark_injected,
                record.idempotency_key,
                record.claim_token,
            )
            await self._send_delivery_ack(ws, injected, "injected")
        except Exception as exc:
            failed = await asyncio.to_thread(
                self.delivery_store.mark_failed,
                record.idempotency_key,
                record.claim_token,
                str(exc),
            )
            await self._send_delivery_ack(ws, failed, failed.status)
            if failed.status == "dead" and failed.correlation_id:
                await self._send_error(ws, failed.correlation_id, failed.last_error or str(exc))

    async def _send_delivery_ack(
        self, ws: Any, record: DeliveryRecord, status: str
    ) -> None:
        if not record.delivery_id:
            return
        payload: dict[str, Any] = {
            "type": "delivery_ack",
            "delivery_id": record.delivery_id,
            "message_type": record.message_type,
            "status": "injected" if status == "injected" else "failed",
        }
        if status != "injected":
            payload["detail"] = record.last_error or status
        await ws.send(json.dumps(payload))

    async def _send_delivery_failure(
        self, ws: Any, message: dict[str, Any], detail: str
    ) -> None:
        delivery_id = message.get("delivery_id")
        if delivery_id:
            await ws.send(
                json.dumps(
                    {
                        "type": "delivery_ack",
                        "delivery_id": delivery_id,
                        "message_type": message.get("type"),
                        "status": "failed",
                        "detail": detail,
                    }
                )
            )
        correlation_id = message.get("correlation_id")
        if correlation_id:
            await self._send_error(ws, correlation_id, detail)

    async def _send_error(self, ws: Any, correlation_id: str, error: str) -> None:
        await ws.send(json.dumps({"type": "error", "correlation_id": correlation_id, "error": error}))


class BridgeSupervisor:
    def __init__(
        self,
        client: CdesktopClient,
        repowire_url: str,
        delivery_store: DeliveryStore | None = None,
    ) -> None:
        self.client = client
        self.repowire_url = repowire_url
        self.delivery_store = delivery_store or DeliveryStore()
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self) -> None:
        while True:
            await self.reconcile()
            await asyncio.sleep(2)

    async def reconcile(self) -> None:
        enabled = enabled_workspaces()
        desired: dict[str, BridgedSession] = {}
        try:
            await asyncio.to_thread(leases.sync_active_workspaces, self.client)
            workspaces = await asyncio.to_thread(self.client.workspaces)
            for workspace in workspaces:
                if workspace["id"] not in enabled or workspace.get("archived"):
                    continue
                try:
                    path = await asyncio.to_thread(_repo_path, self.client, workspace)
                    sessions = await asyncio.to_thread(self.client.sessions, workspace["id"])
                except CdesktopError as exc:
                    LOGGER.warning("Cannot bridge workspace %s: %s", workspace["id"], exc)
                    continue
                for session in sessions:
                    desired[session["id"]] = BridgedSession(workspace, session, path)
        except (CdesktopError, leases.LeaseError) as exc:
            LOGGER.warning("Cannot reconcile cdesktop ownership: %s", exc)
            return

        for session_id in set(self.tasks) - set(desired):
            self.tasks.pop(session_id).cancel()
        for session_id, bridged in desired.items():
            task = self.tasks.get(session_id)
            if task is None or task.done():
                self.tasks[session_id] = asyncio.create_task(
                    RepowireSessionBridge(
                        self.client,
                        bridged,
                        self.repowire_url,
                        self.delivery_store,
                    ).run(),
                    name=f"bridge-{session_id}",
                )


async def run_bridge(cdesktop_url: str | None, repowire_url: str) -> None:
    client = CdesktopClient(cdesktop_url)
    await BridgeSupervisor(client, repowire_url).run()
