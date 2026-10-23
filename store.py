from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


class ConflictError(RuntimeError):
    pass


class NotFoundError(RuntimeError):
    pass


class KindleStore:
    SCHEMA_VERSION = 2
    KEEP_COMPLETED = 2

    def __init__(self, root: Path):
        self.root = Path(root)
        self.books_root = self.root / "books"
        self.requests_root = self.root / "requests"
        self.transactions_root = self.root / "transactions"
        self.completions_root = self.root / "completions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.books_root.mkdir(exist_ok=True)
        self.requests_root.mkdir(exist_ok=True)
        self.transactions_root.mkdir(exist_ok=True)
        self.completions_root.mkdir(exist_ok=True)
        self.lock_path = self.root / ".store.lock"
        with self._lock():
            if not self._catalog_path().exists():
                self._write_json(self._catalog_path(), {
                    "schema_version": self.SCHEMA_VERSION,
                    "revision": 0,
                    "active_book_id": None,
                    "pending_switch": None,
                    "books": {},
                })
            self._recover_transactions_locked()

    @contextmanager
    def _lock(self):
        with self.lock_path.open("a+") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _read_json(path: Path, default: Any | None = None) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if default is not None:
                return default
            raise NotFoundError(f"Missing state file: {path.name}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Corrupt state file: {path.name}: {exc}") from exc

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _catalog_path(self) -> Path:
        return self.root / "catalog.json"

    def _catalog(self) -> dict:
        return self._read_json(self._catalog_path())

    @staticmethod
    def _book_key(book_id: str) -> str:
        return hashlib.sha256(book_id.encode("utf-8")).hexdigest()[:20]

    def _book_dir(self, book_id: str) -> Path:
        return self.books_root / self._book_key(book_id)

    def _metadata_path(self, book_id: str) -> Path:
        return self._book_dir(book_id) / "metadata.json"

    def _queue_path(self, book_id: str) -> Path:
        return self._book_dir(book_id) / "queue.json"

    def _cursor_path(self, book_id: str) -> Path:
        return self._book_dir(book_id) / "cursor.json"

    def _playback_path(self, book_id: str) -> Path:
        return self._book_dir(book_id) / "playback.json"

    @staticmethod
    def _canonical_uuid(value: str) -> str:
        try:
            canonical = str(uuid.UUID(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("request id must be a canonical UUID") from exc
        if canonical != value:
            raise ValueError("request id must be a canonical UUID")
        return canonical

    def _safe_child(self, root: Path, filename: str) -> Path:
        resolved_root = root.resolve()
        candidate = (root / filename).resolve()
        if not candidate.is_relative_to(resolved_root):
            raise ValueError(f"resolved path is outside {root.name} root")
        return candidate

    def _request_path(self, request_id: str) -> Path:
        request_id = self._canonical_uuid(request_id)
        return self._safe_child(self.requests_root, f"{request_id}.json")

    def _transaction_path(self, request_id: str) -> Path:
        request_id = self._canonical_uuid(request_id)
        return self._safe_child(self.transactions_root, f"{request_id}.json")

    def _recover_transactions_locked(self) -> None:
        for path in self.transactions_root.glob("*.json"):
            journal = self._read_json(path)
            request_id = self._canonical_uuid(str(journal.get("request_id", "")))
            book_id = str(journal["book_id"])
            self._write_json(self._queue_path(book_id), journal["queue"])
            self._write_json(self._cursor_path(book_id), journal["cursor"])
            self._write_json(self._request_path(request_id), journal["request"])
            path.unlink()
        self._recover_completions_locked()

    def _completion_path(self, book_id: str, item_id: str) -> Path:
        key = hashlib.sha256(f"{book_id}\0{item_id}".encode("utf-8")).hexdigest()
        return self.completions_root / f"{key}.json"

    def _apply_completion_locked(self, path: Path, journal: dict) -> None:
        book_id = str(journal["book_id"])
        self._write_json(self._queue_path(book_id), journal["queue"])
        self._write_json(self._playback_path(book_id), journal["playback"])
        for old_id in journal.get("remove_ids", []):
            self.audio_path(book_id, old_id).unlink(missing_ok=True)
            (self._book_dir(book_id) / "chunks" / f"{old_id}.txt").unlink(missing_ok=True)
        path.unlink()

    def _recover_completions_locked(self) -> None:
        for path in self.completions_root.glob("*.json"):
            self._apply_completion_locked(path, self._read_json(path))

    def _requeue_expired_claims_locked(self, now: float) -> None:
        for path in self.requests_root.glob("*.json"):
            request = self._read_json(path)
            if (request.get("status") == "claimed" and
                    (request.get("lease_expires_at") or 0) <= now):
                request.update(
                    status="pending", lease_expires_at=None, claim_token=None,
                    claim_generation=max(1, int(request.get("claim_generation") or 0)),
                    lease_expired_at=now, updated_at=now,
                )
                self._write_json(path, request)

    @staticmethod
    def _validate_completion_claim(request: dict, claim_token: str | None, now: float) -> None:
        generation = int(request.get("claim_generation") or 0)
        if generation == 0:
            if request.get("status") == "claimed":
                raise ConflictError("claimed request has no valid claim token; reclaim after lease expiry")
            if claim_token is not None:
                raise ConflictError("claim token does not match request")
            return
        status = request.get("status")
        if status == "cancelled":
            raise ConflictError("request was cancelled; claim token is stale")
        if status in {"completed", "failed"}:
            if not claim_token or claim_token != request.get("claim_token"):
                raise ConflictError("claim token does not match request")
            return
        if (status != "claimed" or
                (request.get("lease_expires_at") or 0) <= now or
                not claim_token or claim_token != request.get("claim_token")):
            raise ConflictError("claim token is stale or request is not actively claimed")

    def _ensure_book(self, *, book_id: str, source_id: str, title: str, author: str,
                     source_url: str, cursor_hash: str | None) -> None:
        directory = self._book_dir(book_id)
        (directory / "chunks").mkdir(parents=True, exist_ok=True)
        now = time.time()
        metadata_path = self._metadata_path(book_id)
        if not metadata_path.exists():
            self._write_json(metadata_path, {
                "id": book_id, "source_id": source_id, "title": title,
                "author": author, "source_url": source_url,
                "created_at": now, "updated_at": now,
            })

        queue_path = self._queue_path(book_id)
        queue = self._read_json(queue_path) if queue_path.exists() else {}
        queue_defaults = {
            "schema_version": self.SCHEMA_VERSION, "revision": 0,
            "generation_epoch": 1, "control_generation": 0,
            "stopped": False, "complete": False, "items": [],
        }
        changed = False
        for key, value in queue_defaults.items():
            if key not in queue:
                queue[key] = value
                changed = True
        if "next_sequence" not in queue:
            queue["next_sequence"] = max(
                [int(item.get("sequence", 0)) for item in queue.get("items", [])] or [0]
            ) + 1
            changed = True
        if changed or not queue_path.exists():
            self._write_json(queue_path, queue)

        cursor_path = self._cursor_path(book_id)
        cursor = self._read_json(cursor_path) if cursor_path.exists() else {}
        changed = False
        if "screen_hash" not in cursor or not cursor.get("screen_hash"):
            if cursor.get("screen_hash") != cursor_hash:
                cursor["screen_hash"] = cursor_hash
                changed = True
        if "recent_screen_hashes" not in cursor:
            cursor["recent_screen_hashes"] = [cursor_hash] if cursor_hash else []
            changed = True
        if "transcript_tail" not in cursor:
            cursor["transcript_tail"] = ""
            changed = True
        if "updated_at" not in cursor:
            cursor["updated_at"] = now
            changed = True
        if changed or not cursor_path.exists():
            self._write_json(cursor_path, cursor)

        playback_path = self._playback_path(book_id)
        playback = self._read_json(playback_path) if playback_path.exists() else {}
        playback_defaults = {
            "revision": 0, "current_item_id": None, "offset": 0.0,
            "status": "stopped", "last_completed_item_id": None,
            "updated_at": now,
        }
        changed = False
        for key, value in playback_defaults.items():
            if key not in playback:
                playback[key] = value
                changed = True
        if changed or not playback_path.exists():
            self._write_json(playback_path, playback)

    def _summary(self, book_id: str) -> dict:
        return self._read_json(self._metadata_path(book_id))

    def state(self) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            self._requeue_expired_claims_locked(time.time())
            catalog = self._catalog()
            active_id = catalog.get("active_book_id")
            active = self._summary(active_id) if active_id else None
            queue = self._read_json(self._queue_path(active_id)) if active_id else None
            cursor = self._read_json(self._cursor_path(active_id)) if active_id else None
            playback = self._read_json(self._playback_path(active_id)) if active_id else None
            requests = []
            for path in self.requests_root.glob("*.json"):
                request = self._read_json(path)
                if request.get("status") in {"pending", "claimed"}:
                    requests.append(request)
            requests.sort(key=lambda item: item.get("created_at", 0))
            return {
                "schema_version": self.SCHEMA_VERSION,
                "revision": catalog.get("revision", 0),
                "active_book": active,
                "pending_switch": catalog.get("pending_switch"),
                "queue": queue,
                "cursor": cursor,
                "playback": playback,
                "request": requests[0] if requests else None,
                "requests": requests,
                "books": list(catalog.get("books", {}).values()),
            }

    def create_request(self, action: str, *, book_id: str | None = None,
                       controller_session_id: str | None = None,
                       controller_stored_session_id: str | None = None,
                       expected_cursor_hash: str | None = None,
                       target_screens: int = 5,
                       control_generation: int | None = None) -> dict:
        if action not in {"detect", "fill", "recover", "stop"}:
            raise ValueError(f"Unsupported request action: {action}")
        with self._lock():
            now = time.time()
            self._recover_transactions_locked()
            self._requeue_expired_claims_locked(now)
            if action != "detect" and not book_id:
                raise ValueError("book_id is required")
            queue = None
            current_generation = None
            fenced = False
            if action in {"fill", "recover", "stop"}:
                queue = self._read_json(self._queue_path(book_id))
                current_generation = int(queue.get("control_generation", 0))
            if action in {"fill", "recover"}:
                fenced = bool(queue.get("stopped")) or (
                    control_generation is None and current_generation != 0
                ) or (
                    control_generation is not None and int(control_generation) != current_generation
                )
                if not fenced:
                    for path in self.requests_root.glob("*.json"):
                        existing = self._read_json(path)
                        if (existing.get("status") in {"pending", "claimed"} and
                                existing.get("action") == action and
                                existing.get("book_id") == book_id and
                                int(existing.get("control_generation", 0)) == current_generation):
                            return existing
            request = {
                "id": str(uuid.uuid4()), "action": action,
                "status": "cancelled" if fenced else "pending",
                "book_id": book_id, "controller_session_id": controller_session_id,
                "controller_stored_session_id": controller_stored_session_id,
                "expected_cursor_hash": expected_cursor_hash,
                "target_screens": int(target_screens), "created_at": now,
                "updated_at": now, "lease_expires_at": None,
                "lease_expired_at": None, "claim_token": None,
                "claim_generation": 0, "control_generation": current_generation,
                "result": None,
                "error": "reader is stopped or generation is stale" if fenced else None,
            }
            if action == "stop":
                # Fence every worker first. A crash after this write can leave a
                # request uncancelled, but it cannot append against the stopped
                # queue or its advanced control generation.
                queue["control_generation"] = current_generation + 1
                queue["stopped"] = True
                queue["revision"] = queue.get("revision", 0) + 1
                self._write_json(self._queue_path(book_id), queue)
                cancelled = []
                for path in self.requests_root.glob("*.json"):
                    existing = self._read_json(path)
                    if (existing.get("action") in {"fill", "recover"} and
                            existing.get("book_id") == book_id and
                            existing.get("status") in {"pending", "claimed"}):
                        existing.update(
                            status="cancelled", error="stopped by user", updated_at=now,
                            lease_expires_at=None, stop_request_id=request["id"],
                        )
                        self._write_json(path, existing)
                        cancelled.append(existing["id"])
                request.update(
                    status="completed", result={"cancelled_request_ids": cancelled},
                    updated_at=now,
                )
            self._write_json(self._request_path(request["id"]), request)
            return request

    def get_request(self, request_id: str) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            return self._read_json(self._request_path(request_id))

    def resume(self, book_id: str, *, expected_control_generation: int | None = None) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            queue = self._read_json(self._queue_path(book_id))
            changed = False
            if "control_generation" not in queue:
                queue["control_generation"] = 0
                changed = True
            if "stopped" not in queue:
                queue["stopped"] = False
                changed = True
            current_generation = int(queue["control_generation"])
            # A legacy generation-less resume is safe only before any stop has
            # advanced the fence. It must never clear a durable stop.
            expected = 0 if expected_control_generation is None else int(expected_control_generation)
            if expected != current_generation:
                raise ConflictError("resume control generation is stale")
            if queue.get("stopped"):
                queue["control_generation"] = current_generation + 1
                queue["stopped"] = False
                changed = True
            if changed:
                queue["revision"] = queue.get("revision", 0) + 1
                self._write_json(self._queue_path(book_id), queue)
            return queue

    def cancel_request(self, request_id: str, reason: str = "cancelled by client") -> dict:
        with self._lock():
            self._recover_transactions_locked()
            request = self._read_json(self._request_path(request_id))
            if request.get("status") in {"pending", "claimed"}:
                request.update(
                    status="cancelled", error=reason, updated_at=time.time(),
                    lease_expires_at=None,
                )
                self._write_json(self._request_path(request_id), request)
            return request

    def claim_request(self, request_id: str, lease_seconds: int = 300) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            now = time.time()
            self._requeue_expired_claims_locked(now)
            request = self._read_json(self._request_path(request_id))
            if request["status"] == "claimed" and (request.get("lease_expires_at") or 0) > now:
                raise ConflictError("request already claimed")
            if request["status"] not in {"pending", "claimed"}:
                return request
            request["status"] = "claimed"
            request["lease_expires_at"] = now + lease_seconds
            request["lease_expired_at"] = None
            request["claim_token"] = str(uuid.uuid4())
            request["claim_generation"] = int(request.get("claim_generation") or 0) + 1
            request["updated_at"] = now
            self._write_json(self._request_path(request_id), request)
            return request

    def fail_request(self, request_id: str, error: str, *, claim_token: str | None = None) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            now = time.time()
            self._requeue_expired_claims_locked(now)
            request = self._read_json(self._request_path(request_id))
            self._validate_completion_claim(request, claim_token, now)
            if request.get("status") in {"completed", "failed", "cancelled"}:
                return request
            request.update(status="failed", error=str(error), updated_at=now, lease_expires_at=None)
            self._write_json(self._request_path(request_id), request)
            return request

    def report_detection(self, request_id: str, *, source_id: str, title: str,
                         author: str = "", source_url: str = "",
                         cursor_hash: str | None = None,
                         claim_token: str | None = None) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            now = time.time()
            self._requeue_expired_claims_locked(now)
            request = self._read_json(self._request_path(request_id))
            if request.get("action") != "detect":
                raise ConflictError("request is not a detection request")
            self._validate_completion_claim(request, claim_token, now)
            if request.get("status") == "completed":
                return dict(request.get("result") or {})
            if request.get("status") in {"failed", "cancelled"}:
                raise ConflictError("request is terminal: {}".format(request.get("status")))
            if not source_id:
                material = f"{title.strip().lower()}\0{author.strip().lower()}"
                source_id = "meta-" + hashlib.sha256(material.encode()).hexdigest()[:16]
            book_id = f"kindle:{source_id}"
            self._ensure_book(book_id=book_id, source_id=source_id, title=title.strip() or "Unknown book",
                              author=author.strip(), source_url=source_url, cursor_hash=cursor_hash)
            catalog = self._catalog()
            catalog["books"][book_id] = self._summary(book_id)
            if not catalog.get("active_book_id") or catalog.get("active_book_id") == book_id:
                catalog["active_book_id"] = book_id
                catalog["pending_switch"] = None
                status = "activated"
            else:
                catalog["pending_switch"] = {
                    "source_book_id": catalog["active_book_id"], "target_book_id": book_id,
                    "detected_at": now,
                }
                status = "confirmation_required"
            catalog["revision"] = catalog.get("revision", 0) + 1
            self._write_json(self._catalog_path(), catalog)
            result = {"book_id": book_id, "status": status}
            request.update(status="completed", result=result, updated_at=now, lease_expires_at=None)
            self._write_json(self._request_path(request_id), request)
            return result

    def confirm_switch(self, target_book_id: str, *, restart: bool = False) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            catalog = self._catalog()
            if target_book_id not in catalog.get("books", {}):
                raise NotFoundError("unknown target book")
            if restart:
                queue = self._read_json(self._queue_path(target_book_id))
                for item in queue.get("items", []):
                    for suffix in (".mp3", ".txt"):
                        (self._book_dir(target_book_id) / "chunks" / f"{item['id']}{suffix}").unlink(missing_ok=True)
                queue.update(items=[], revision=queue.get("revision", 0) + 1,
                             generation_epoch=queue.get("generation_epoch", 1) + 1, next_sequence=1)
                self._write_json(self._queue_path(target_book_id), queue)
                self._write_json(self._playback_path(target_book_id), {
                    "revision": 0, "current_item_id": None, "offset": 0.0,
                    "status": "stopped", "last_completed_item_id": None, "updated_at": time.time(),
                })
            catalog["active_book_id"] = target_book_id
            catalog["pending_switch"] = None
            catalog["revision"] = catalog.get("revision", 0) + 1
            self._write_json(self._catalog_path(), catalog)
            return self._summary(target_book_id)

    def cancel_switch(self) -> None:
        with self._lock():
            self._recover_transactions_locked()
            catalog = self._catalog()
            catalog["pending_switch"] = None
            catalog["revision"] = catalog.get("revision", 0) + 1
            self._write_json(self._catalog_path(), catalog)

    def queue(self, book_id: str) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            return self._read_json(self._queue_path(book_id))

    def audio_path(self, book_id: str, item_id: str) -> Path:
        return self._book_dir(book_id) / "chunks" / f"{Path(item_id).name}.mp3"

    def append_item(self, book_id: str, *, request_id: str, expected_cursor_hash: str,
                    next_cursor_hash: str, audio: bytes, transcript: str,
                    duration: float, label: str, claim_token: str | None = None) -> dict:
        duration = float(duration)
        if not audio or not math.isfinite(duration) or duration <= 0:
            raise ValueError("audio and finite positive duration are required")
        with self._lock():
            self._recover_transactions_locked()
            now = time.time()
            self._requeue_expired_claims_locked(now)
            request = self._read_json(self._request_path(request_id))
            if request.get("book_id") != book_id or request.get("action") not in {"fill", "recover"}:
                raise ConflictError("request does not match book")
            self._validate_completion_claim(request, claim_token, now)
            if request.get("expected_cursor_hash") != expected_cursor_hash:
                raise ConflictError("request cursor does not match supplied cursor")
            if request.get("status") == "completed" and request.get("result", {}).get("item_id"):
                result = request["result"]
                if result.get("item"):
                    return dict(result["item"])
                item_id = result["item_id"]
                item = next((value for value in self._read_json(self._queue_path(book_id))["items"]
                             if value["id"] == item_id), None)
                if item is None:
                    raise ConflictError("completed append result predates durable item metadata")
                return item
            if request.get("status") not in {"pending", "claimed"}:
                raise ConflictError("request is not appendable: {}".format(request.get("status")))
            queue = self._read_json(self._queue_path(book_id))
            if (queue.get("stopped") or
                    int(request.get("control_generation", 0)) != int(queue.get("control_generation", 0))):
                raise ConflictError("request generation is stopped or stale")
            cursor = self._read_json(self._cursor_path(book_id))
            if cursor.get("screen_hash") != expected_cursor_hash:
                raise ConflictError("cursor changed; refusing stale append")
            sequence = int(queue.get("next_sequence", 1))
            item_id = "{:04d}-{:08d}".format(queue.get("generation_epoch", 1), sequence)
            self._write_bytes(self.audio_path(book_id, item_id), audio)
            self._write_bytes(self._book_dir(book_id) / "chunks" / f"{item_id}.txt", transcript.encode("utf-8"))
            item = {
                "id": item_id, "sequence": sequence, "label": label,
                "duration": float(duration), "bytes": len(audio), "state": "ready",
                "request_id": request_id, "created_at": now,
            }
            queue["items"].append(item)
            queue["next_sequence"] = sequence + 1
            queue["revision"] = queue.get("revision", 0) + 1
            recent = [value for value in cursor.get("recent_screen_hashes", []) if value]
            recent.append(next_cursor_hash)
            cursor.update(screen_hash=next_cursor_hash, recent_screen_hashes=recent[-12:],
                          transcript_tail=transcript[-500:], updated_at=now)
            request.update(
                status="completed", result={"item_id": item_id, "item": item}, updated_at=now,
                lease_expires_at=None, lease_expired_at=None,
            )
            transaction_path = self._transaction_path(request_id)
            self._write_json(transaction_path, {
                "schema_version": 1, "request_id": request_id, "book_id": book_id,
                "queue": queue, "cursor": cursor, "request": request,
            })
            self._write_json(self._queue_path(book_id), queue)
            self._write_json(self._cursor_path(book_id), cursor)
            self._write_json(self._request_path(request_id), request)
            transaction_path.unlink()
            return item

    def checkpoint(self, book_id: str, item_id: str | None, offset: float, status: str) -> dict:
        if status not in {"playing", "paused", "stopped"}:
            raise ValueError("invalid playback status")
        with self._lock():
            self._recover_transactions_locked()
            queue = self._read_json(self._queue_path(book_id))
            if item_id is not None and not any(item["id"] == item_id for item in queue.get("items", [])):
                raise NotFoundError("queue item not found")
            playback = self._read_json(self._playback_path(book_id))
            playback.update(current_item_id=item_id, offset=max(0.0, float(offset)), status=status,
                            revision=playback.get("revision", 0) + 1, updated_at=time.time())
            self._write_json(self._playback_path(book_id), playback)
            return playback

    def complete_item(self, book_id: str, item_id: str) -> dict:
        with self._lock():
            self._recover_transactions_locked()
            queue = self._read_json(self._queue_path(book_id))
            found = False
            for item in queue.get("items", []):
                if item["id"] == item_id:
                    item["state"] = "completed"
                    found = True
                    break
            if not found:
                return queue
            playback = self._read_json(self._playback_path(book_id))
            playback["last_completed_item_id"] = item_id
            playback["updated_at"] = time.time()
            completed = [item for item in queue["items"] if item.get("state") == "completed"]
            remove_ids = {item["id"] for item in completed[:-self.KEEP_COMPLETED]}
            if playback.get("current_item_id") in remove_ids:
                remove_ids.remove(playback["current_item_id"])
            queue["items"] = [item for item in queue["items"] if item["id"] not in remove_ids]
            queue["revision"] = queue.get("revision", 0) + 1
            path = self._completion_path(book_id, item_id)
            journal = {
                "schema_version": 1, "book_id": book_id, "item_id": item_id,
                "queue": queue, "playback": playback,
                "remove_ids": sorted(remove_ids),
            }
            self._write_json(path, journal)
            self._apply_completion_locked(path, journal)
            return queue

    def migrate_legacy(self, *, source_id: str, title: str, author: str = "", cursor_hash: str | None = None) -> dict:
        legacy = self.root / "playlist.json"
        book_id = f"kindle:{source_id}"
        with self._lock():
            self._recover_transactions_locked()
            if not legacy.exists():
                return {"migrated": 0}
            catalog = self._catalog()
            if book_id in catalog.get("books", {}):
                return {"migrated": 0, "book_id": book_id}
            self._ensure_book(book_id=book_id, source_id=source_id, title=title, author=author,
                              source_url=f"https://read.amazon.com/?asin={source_id}", cursor_hash=cursor_hash)
            legacy_data = self._read_json(legacy)
            queue = self._read_json(self._queue_path(book_id))
            migrated = 0
            for raw in legacy_data.get("items", []):
                source = self.root / Path(str(raw.get("src", ""))).name
                if not source.is_file():
                    continue
                item_id = Path(str(raw.get("id") or f"legacy-{migrated + 1}")).name
                target = self.audio_path(book_id, item_id)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
                queue["items"].append({
                    "id": item_id, "sequence": migrated + 1,
                    "label": str(raw.get("label") or item_id),
                    "duration": float(raw.get("duration") or 0),
                    "bytes": target.stat().st_size, "state": "ready",
                    "request_id": None, "created_at": time.time(),
                })
                migrated += 1
            queue.update(revision=1, next_sequence=migrated + 1,
                         complete=bool(legacy_data.get("complete")))
            self._write_json(self._queue_path(book_id), queue)
            catalog["books"][book_id] = self._summary(book_id)
            catalog["active_book_id"] = book_id
            catalog["revision"] = catalog.get("revision", 0) + 1
            self._write_json(self._catalog_path(), catalog)
            return {"migrated": migrated, "book_id": book_id}
