"""Selective native checkpoint retention; TaskStore owns the only reference.

This module performs no database writes and never deletes working copies.
The SDK publishes outside its task fence, then commits the verified occurrence
with its checkpoint transition under the TaskStore's writing-connection policy.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing
from typing import TYPE_CHECKING, Any

from .evidence import (
    EvidenceClient,
    EvidenceUnavailable,
    receipt_facts,
    verified_chunks,
)

if TYPE_CHECKING:
    from .task_store import CheckpointOperation


def verified_receipt(
    operation: CheckpointOperation, receipt: dict[str, Any]
) -> dict[str, Any]:
    facts = operation.facts
    result = {
        **receipt_facts(receipt, facts["execution_id"]),
        "durability": "confirmed",
    }
    expected = {
        "execution_id": facts["execution_id"],
        "publication_key": f"checkpoint:{operation.operation_id}",
        "producer_ref": f"{operation.task_id}:{operation.epoch}",
        "original_path": facts["original_path"],
        "original_name": facts["original_name"],
        "sha256": facts["sha256"],
        "size_bytes": facts["size_bytes"],
        "durability": "confirmed",
    }
    if any(result[key] != value for key, value in expected.items()):
        raise EvidenceUnavailable(
            "checkpoint receipt is not a confirmed immutable fact match"
        )
    for key in ("id", "attachment_id", "execution_id"):
        try:
            if str(uuid.UUID(receipt[key])) != receipt[key]:
                raise ValueError("not canonical")
        except (ValueError, TypeError, AttributeError) as exc:
            raise EvidenceUnavailable("invalid native checkpoint identity") from exc
    if len(json.dumps(result).encode()) > 8192:
        raise EvidenceUnavailable("checkpoint receipt exceeds metadata bound")
    if operation.receipt is not None and operation.receipt != result:
        raise EvidenceUnavailable("checkpoint occurrence changed during replay")
    return result


class CheckpointRetention:
    def __init__(self, client: EvidenceClient):
        self.client = client

    def publish(self, operation: CheckpointOperation, body: bytes) -> dict[str, Any]:
        self._verify_bytes(operation, body)
        if operation.receipt is None:
            receipt = self.client.artifact(
                operation.facts["execution_id"],
                publication_key=f"checkpoint:{operation.operation_id}",
                name=operation.facts["original_name"],
                body=body,
                original_path=operation.facts["original_path"],
                producer_ref=f"{operation.task_id}:{operation.epoch}",
            )
        else:
            receipt = self.client.artifact_receipt(
                operation.facts["execution_id"], operation.receipt["id"]
            )
        return verified_receipt(operation, receipt)

    def read(self, operation: CheckpointOperation) -> bytes:
        if operation.receipt is None or not operation.durable_commit:
            raise EvidenceUnavailable(
                "checkpoint has no confirmed task occurrence reference"
            )
        receipt = verified_receipt(
            operation,
            self.client.artifact_receipt(
                operation.facts["execution_id"],
                operation.receipt["id"],
            ),
        )
        with closing(verified_chunks(self.client, receipt)) as chunks:
            return b"".join(chunks)

    @staticmethod
    def _verify_bytes(operation: CheckpointOperation, body: bytes) -> None:
        if (
            len(body) != operation.facts["size_bytes"]
            or hashlib.sha256(body).hexdigest() != operation.facts["sha256"]
        ):
            raise EvidenceUnavailable(
                "checkpoint bytes changed from the saved operation"
            )
