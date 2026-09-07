"""Selective checkpoint publication, never a worktree-wide copy."""
from __future__ import annotations
import hashlib, sqlite3, uuid
from pathlib import Path
from .evidence import EvidenceClient, EvidenceUnavailable

class CheckpointRetention:
    def __init__(self, db: Path, client: EvidenceClient, execution_id: str):
        self.db, self.client, self.execution_id = db, client, execution_id
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS task_artifacts (operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, epoch INTEGER NOT NULL, digest TEXT NOT NULL, occurrence_id TEXT, local_path TEXT NOT NULL)")
    def retain(self, task_id: str, epoch: int, path: Path, operation_id: str | None = None) -> str:
        """Replay one logical operation using its persisted identity."""
        body = path.read_bytes(); digest = hashlib.sha256(body).hexdigest()
        with sqlite3.connect(self.db) as conn:
            operation_id = operation_id or str(uuid.uuid4())
            row = conn.execute("SELECT occurrence_id FROM task_artifacts WHERE operation_id=?", (operation_id,)).fetchone()
            if row and row[0]: return str(row[0])
            conn.execute("INSERT OR IGNORE INTO task_artifacts(operation_id,task_id,epoch,digest,local_path) VALUES(?,?,?,?,?)", (operation_id, task_id, epoch, digest, str(path)))
            conn.commit()
        receipt = self.client.artifact(self.execution_id, publication_key=f"checkpoint:{operation_id}", name=path.name, body=body, original_path=str(path), producer_ref=f"{task_id}:{epoch}")
        publication_key = f"checkpoint:{operation_id}"
        if (
            receipt.get("durability") != "confirmed"
            or receipt.get("execution_id") != self.execution_id
            or receipt.get("publication_key") != publication_key
            or receipt.get("producer_ref") != f"{task_id}:{epoch}"
            or receipt.get("original_path") != str(path)
            or receipt.get("sha256") != digest
            or int(receipt.get("size_bytes", -1)) != len(body)
        ):
            raise EvidenceUnavailable("native artifact receipt is not a confirmed byte match")
        with sqlite3.connect(self.db) as conn:
            # The task-side reference is separately durable from the native
            # receipt.  Do not infer this policy from some other connection.
            conn.execute("PRAGMA synchronous=FULL")
            if int(conn.execute("PRAGMA synchronous").fetchone()[0]) < 2:
                raise EvidenceUnavailable("cannot establish durable task reference policy")
            conn.execute("UPDATE task_artifacts SET occurrence_id=? WHERE operation_id=?", (receipt["id"], operation_id)); conn.commit()
        return str(receipt["id"])
    def read(self, operation_id: str, local: Path) -> bytes:
        if local.exists(): return local.read_bytes()
        with sqlite3.connect(self.db) as conn:
            row = conn.execute("SELECT occurrence_id FROM task_artifacts WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None or row[0] is None: raise EvidenceUnavailable("checkpoint has no confirmed retained occurrence")
        return self.client.artifact_bytes(self.execution_id, str(row[0]))
