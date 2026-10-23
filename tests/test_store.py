from __future__ import annotations

import asyncio
import argparse
import importlib
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

from store import ConflictError, KindleStore


def make_store(tmp_path: Path) -> KindleStore:
    return KindleStore(tmp_path / "kindle-reader")


def test_concurrent_first_constructors_initialize_catalog_once_under_lock(
    tmp_path: Path, monkeypatch,
):
    root = tmp_path / "kindle-reader"
    original_write = KindleStore._write_json
    first_write_entered = threading.Event()
    release_first_write = threading.Event()
    competing_write_entered = threading.Event()
    writes_lock = threading.Lock()
    catalog_writes = 0
    stores = []
    errors = []

    def coordinated_write(path, value):
        nonlocal catalog_writes
        if path == root / "catalog.json":
            with writes_lock:
                catalog_writes += 1
                write_number = catalog_writes
            if write_number == 1:
                first_write_entered.set()
                if not release_first_write.wait(5):
                    raise TimeoutError("test did not release first catalog write")
            else:
                competing_write_entered.set()
        original_write(path, value)

    monkeypatch.setattr(KindleStore, "_write_json", staticmethod(coordinated_write))

    def construct():
        try:
            stores.append(KindleStore(root))
        except BaseException as exc:  # preserve thread failures for the main test
            errors.append(exc)

    first = threading.Thread(target=construct)
    second = threading.Thread(target=construct)
    first.start()
    assert first_write_entered.wait(5)
    second.start()
    raced = competing_write_entered.wait(1)
    release_first_write.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert raced is False, "the second constructor initialized catalog before the first released .store.lock"
    assert len(stores) == 2
    assert catalog_writes == 1


def activate_book(store: KindleStore, source_id: str = "B001", title: str = "Book One") -> str:
    request = store.create_request("detect", controller_session_id="session-1")
    result = store.report_detection(
        request["id"], source_id=source_id, title=title, author="Author",
        source_url=f"https://read.amazon.com/?asin={source_id}", cursor_hash="sha256:screen-0",
    )
    assert result["status"] == "activated"
    return result["book_id"]


def append_chunk(store: KindleStore, book_id: str, sequence: int, cursor: str, next_cursor: str):
    request = store.create_request(
        "fill", book_id=book_id, controller_session_id="session-1",
        expected_cursor_hash=cursor, target_screens=5,
    )
    return store.append_item(
        book_id, request_id=request["id"], expected_cursor_hash=cursor,
        next_cursor_hash=next_cursor, audio=b"ID3audio" + bytes([sequence]),
        transcript=f"chunk {sequence}", duration=60.0 + sequence,
        label=f"Continuation {sequence}",
    )


def test_first_detection_activates_stable_source_id(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    state = store.state()
    assert book_id == "kindle:B001"
    assert state["active_book"]["title"] == "Book One"
    assert state["active_book"]["source_id"] == "B001"
    assert state["pending_switch"] is None


def test_detection_of_different_book_requires_confirmation(tmp_path: Path):
    store = make_store(tmp_path)
    original_id = activate_book(store)
    request = store.create_request("detect", controller_session_id="session-1")
    result = store.report_detection(
        request["id"], source_id="B002", title="Book Two", author="Other",
        source_url="https://read.amazon.com/?asin=B002", cursor_hash="sha256:two-0",
    )
    assert result["status"] == "confirmation_required"
    assert store.state()["active_book"]["id"] == original_id
    assert store.state()["pending_switch"]["target_book_id"] == "kindle:B002"
    store.confirm_switch("kindle:B002", restart=False)
    assert store.state()["active_book"]["title"] == "Book Two"


def test_append_is_idempotent_and_rejects_stale_cursor(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request(
        "fill", book_id=book_id, controller_session_id="session-1",
        expected_cursor_hash="sha256:screen-0", target_screens=5,
    )
    kwargs = dict(
        request_id=request["id"], expected_cursor_hash="sha256:screen-0",
        next_cursor_hash="sha256:screen-5", audio=b"ID3payload",
        transcript="new text", duration=90.5, label="Continuation 1",
    )
    first = store.append_item(book_id, **kwargs)
    duplicate = store.append_item(book_id, **kwargs)
    assert duplicate["id"] == first["id"]
    assert len(store.queue(book_id)["items"]) == 1
    stale_request = store.create_request(
        "fill", book_id=book_id, controller_session_id="session-1",
        expected_cursor_hash="sha256:screen-0", target_screens=5,
    )
    with pytest.raises(ConflictError, match="cursor"):
        store.append_item(book_id, request_id=stale_request["id"], **{k: v for k, v in kwargs.items() if k != "request_id"})


@pytest.mark.parametrize("duration", [float("nan"), float("inf"), float("-inf")])
def test_append_rejects_non_finite_duration(tmp_path: Path, duration: float):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
    )

    with pytest.raises(ValueError, match="finite positive duration"):
        store.append_item(
            book_id, request_id=request["id"], expected_cursor_hash="sha256:screen-0",
            next_cursor_hash="sha256:screen-5", audio=b"ID3payload", transcript="text",
            duration=duration, label="Invalid duration",
        )

    assert store.queue(book_id)["items"] == []


def test_append_replay_returns_durable_item_after_queue_pruning(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    cursor = "sha256:screen-0"
    items = []
    kwargs_by_request = {}
    for sequence in range(1, 5):
        request = store.create_request("fill", book_id=book_id, expected_cursor_hash=cursor)
        next_cursor = f"sha256:screen-{sequence}"
        kwargs = dict(
            request_id=request["id"], expected_cursor_hash=cursor,
            next_cursor_hash=next_cursor, audio=b"ID3" + bytes([sequence]),
            transcript=f"text {sequence}", duration=sequence, label=f"Item {sequence}",
        )
        items.append(store.append_item(book_id, **kwargs))
        kwargs_by_request[request["id"]] = kwargs
        cursor = next_cursor
    for item in items:
        store.complete_item(book_id, item["id"])
    assert items[0]["id"] not in {item["id"] for item in store.queue(book_id)["items"]}

    replay = store.append_item(book_id, **kwargs_by_request[items[0]["request_id"]])

    assert replay == items[0]


@pytest.mark.parametrize("request_id", [
    "../catalog", "../../outside", "not-a-uuid",
    "00000000-0000-0000-0000-000000000000/../../outside",
    "550E8400-E29B-41D4-A716-446655440000",
])
def test_request_ids_must_be_canonical_uuids(tmp_path: Path, request_id: str):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="canonical UUID"):
        store.get_request(request_id)


def test_request_path_rejects_uuid_symlink_escape(tmp_path: Path):
    store = make_store(tmp_path)
    request_id = "550e8400-e29b-41d4-a716-446655440000"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (store.requests_root / f"{request_id}.json").symlink_to(outside)
    with pytest.raises(ValueError, match="outside requests root"):
        store.get_request(request_id)


def test_stop_request_durably_cancels_running_fill(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    fill = store.create_request(
        "fill", book_id=book_id, controller_session_id="session-1",
        expected_cursor_hash="sha256:screen-0",
    )
    store.claim_request(fill["id"])

    stop = store.create_request("stop", book_id=book_id, controller_session_id="session-1")

    assert stop["status"] == "completed"
    assert stop["result"] == {"cancelled_request_ids": [fill["id"]]}
    assert store.get_request(fill["id"])["status"] == "cancelled"
    with pytest.raises(ConflictError, match="cancelled|not appendable"):
        store.append_item(
            book_id, request_id=fill["id"], expected_cursor_hash="sha256:screen-0",
            next_cursor_hash="sha256:screen-5", audio=b"ID3payload", transcript="text",
            duration=10, label="Cancelled",
        )


def test_stop_without_controller_session_durably_fences_backend(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    fill = store.create_request("fill", book_id=book_id, expected_cursor_hash="sha256:screen-0")

    stop = KindleStore(store.root).create_request("stop", book_id=book_id)

    reloaded = KindleStore(store.root)
    assert stop["status"] == "completed"
    assert reloaded.queue(book_id)["stopped"] is True
    assert reloaded.get_request(fill["id"])["status"] == "cancelled"


@pytest.mark.parametrize("crash_after_write", [1, 2, 3])
def test_stop_is_safe_after_every_persistence_boundary(
    tmp_path: Path, monkeypatch, crash_after_write: int,
):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    fill = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
    )
    initial_generation = store.queue(book_id)["control_generation"]
    original_write = store._write_json
    writes = 0

    def crash_after_durable_write(path, value):
        nonlocal writes
        original_write(path, value)
        writes += 1
        if writes == crash_after_write:
            raise OSError(f"simulated crash after write {writes}")

    monkeypatch.setattr(store, "_write_json", crash_after_durable_write)
    with pytest.raises(OSError, match="simulated crash"):
        store.create_request("stop", book_id=book_id)
    monkeypatch.setattr(store, "_write_json", original_write)

    recovered = KindleStore(store.root)
    recovered_queue = recovered.queue(book_id)
    assert recovered_queue["stopped"] is True
    assert recovered_queue["control_generation"] > initial_generation
    if crash_after_write >= 2:
        assert recovered.get_request(fill["id"])["status"] == "cancelled"
    with pytest.raises(ConflictError, match="stopped|stale|cancelled|not appendable"):
        recovered.append_item(
            book_id, request_id=fill["id"], expected_cursor_hash="sha256:screen-0",
            next_cursor_hash="sha256:screen-5", audio=b"ID3payload", transcript="text",
            duration=10, label="Must remain fenced",
        )


def test_expired_claim_is_requeued_and_deduplicated(tmp_path: Path, monkeypatch):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request("fill", book_id=book_id)
    monkeypatch.setattr("store.time.time", lambda: 100.0)
    store.claim_request(request["id"], lease_seconds=5)
    monkeypatch.setattr("store.time.time", lambda: 106.0)

    state = store.state()
    retried = store.create_request("fill", book_id=book_id)

    assert state["request"]["id"] == request["id"]
    assert state["request"]["status"] == "pending"
    assert state["request"]["lease_expired_at"] == 106.0
    assert retried["id"] == request["id"]
    reclaimed = store.claim_request(request["id"])
    assert reclaimed["status"] == "claimed"
    assert reclaimed["lease_expired_at"] is None


def test_append_transaction_recovers_after_partial_state_commit(tmp_path: Path, monkeypatch):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
    )
    original_write = store._write_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path == store._cursor_path(book_id) and not failed:
            failed = True
            raise OSError("simulated crash")
        return original_write(path, value)

    monkeypatch.setattr(store, "_write_json", fail_once)
    kwargs = dict(
        request_id=request["id"], expected_cursor_hash="sha256:screen-0",
        next_cursor_hash="sha256:screen-5", audio=b"ID3payload", transcript="new text",
        duration=90.5, label="Continuation 1",
    )
    with pytest.raises(OSError, match="simulated crash"):
        store.append_item(book_id, **kwargs)

    recovered = KindleStore(store.root)
    item = recovered.append_item(book_id, **kwargs)
    assert len(recovered.queue(book_id)["items"]) == 1
    assert recovered.state()["cursor"]["screen_hash"] == "sha256:screen-5"
    assert recovered.get_request(request["id"])["status"] == "completed"
    assert item["id"] == recovered.queue(book_id)["items"][0]["id"]
    assert list(recovered.transactions_root.glob("*.json")) == []


def test_restart_recovers_append_journal_before_deleting_chunks(tmp_path: Path, monkeypatch):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
    )
    original_write = store._write_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path == store._cursor_path(book_id) and not failed:
            failed = True
            raise OSError("simulated crash")
        return original_write(path, value)

    monkeypatch.setattr(store, "_write_json", fail_once)
    with pytest.raises(OSError, match="simulated crash"):
        store.append_item(
            book_id, request_id=request["id"], expected_cursor_hash="sha256:screen-0",
            next_cursor_hash="sha256:screen-5", audio=b"ID3journalled",
            transcript="journalled", duration=30, label="Journalled",
        )
    monkeypatch.setattr(store, "_write_json", original_write)
    journal = store._read_json(store._transaction_path(request["id"]))
    item_id = journal["queue"]["items"][0]["id"]

    store.confirm_switch(book_id, restart=True)

    assert list(store.transactions_root.glob("*.json")) == []
    assert store.queue(book_id)["items"] == []
    assert not store.audio_path(book_id, item_id).exists()
    assert not (store._book_dir(book_id) / "chunks" / f"{item_id}.txt").exists()


def plant_pending_append_journal(store: KindleStore, book_id: str) -> str:
    item = append_chunk(store, book_id, 1, "sha256:screen-0", "sha256:screen-5")
    request = store.get_request(item["request_id"])
    committed_queue = store._read_json(store._queue_path(book_id))
    committed_cursor = store._read_json(store._cursor_path(book_id))
    store._write_json(store._transaction_path(request["id"]), {
        "schema_version": 1, "request_id": request["id"], "book_id": book_id,
        "queue": committed_queue, "cursor": committed_cursor, "request": request,
    })
    stale_queue = dict(committed_queue, items=[], next_sequence=1)
    stale_cursor = dict(committed_cursor, screen_hash="sha256:screen-0", recent_screen_hashes=["sha256:screen-0"])
    store._write_json(store._queue_path(book_id), stale_queue)
    store._write_json(store._cursor_path(book_id), stale_cursor)
    return item["id"]


@pytest.mark.parametrize("mutation", ["resume", "cancel_switch", "migrate_legacy", "migrate_legacy_missing"])
def test_locked_mutations_recover_pending_append_journal_first(tmp_path: Path, mutation: str):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    item_id = plant_pending_append_journal(store, book_id)

    if mutation == "resume":
        result = store.resume(book_id)
        assert [item["id"] for item in result["items"]] == [item_id]
    elif mutation == "cancel_switch":
        store.cancel_switch()
    else:
        if mutation == "migrate_legacy":
            (store.root / "legacy.mp3").write_bytes(b"ID3legacy")
            (store.root / "playlist.json").write_text(json.dumps({
                "items": [{"id": "legacy", "src": "legacy.mp3", "duration": 1}],
            }), encoding="utf-8")
        store.migrate_legacy(source_id="LEGACY", title="Legacy")

    assert list(store.transactions_root.glob("*.json")) == []
    raw_queue = store._read_json(store._queue_path(book_id))
    assert [item["id"] for item in raw_queue["items"]] == [item_id]
    assert store._read_json(store._cursor_path(book_id))["screen_hash"] == "sha256:screen-5"


def test_resume_checkpoint_is_per_book(tmp_path: Path):
    store = make_store(tmp_path)
    first = activate_book(store, "B001", "Book One")
    first_item = append_chunk(store, first, 1, "sha256:screen-0", "sha256:screen-5")
    store.checkpoint(first, first_item["id"], 31.25, "paused")
    detect = store.create_request("detect", controller_session_id="session-1")
    store.report_detection(detect["id"], source_id="B002", title="Book Two", author="Other", source_url="u", cursor_hash="sha256:two-0")
    store.confirm_switch("kindle:B002", restart=False)
    assert store.state()["playback"]["current_item_id"] is None
    store.confirm_switch(first, restart=False)
    assert store.state()["playback"]["current_item_id"] == first_item["id"]
    assert store.state()["playback"]["offset"] == 31.25


def test_completion_prunes_old_audio_but_keeps_two_recent_items(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    cursor = "sha256:screen-0"
    items = []
    for sequence in range(1, 6):
        next_cursor = f"sha256:screen-{sequence * 5}"
        items.append(append_chunk(store, book_id, sequence, cursor, next_cursor))
        cursor = next_cursor
    for item in items[:4]:
        store.complete_item(book_id, item["id"])
    queue = store.queue(book_id)
    assert [item["state"] for item in queue["items"]] == ["completed", "completed", "ready"]
    assert [item["id"] for item in queue["items"]] == [items[2]["id"], items[3]["id"], items[4]["id"]]
    assert not store.audio_path(book_id, items[0]["id"]).exists()
    assert store.audio_path(book_id, items[2]["id"]).exists()


def test_completion_intent_recovers_across_store_instances(tmp_path: Path, monkeypatch):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    item = append_chunk(store, book_id, 1, "sha256:screen-0", "sha256:screen-5")
    original_write = store._write_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if path == store._queue_path(book_id) and not failed:
            failed = True
            raise OSError("simulated completion crash")
        return original_write(path, value)

    monkeypatch.setattr(store, "_write_json", fail_once)
    with pytest.raises(OSError, match="simulated completion crash"):
        store.complete_item(book_id, item["id"])

    recovered = KindleStore(store.root)
    assert recovered.queue(book_id)["items"][0]["state"] == "completed"
    assert recovered.state()["playback"]["last_completed_item_id"] == item["id"]
    assert list(recovered.completions_root.glob("*.json")) == []


@pytest.mark.parametrize("missing", ["queue", "cursor", "playback"])
def test_existing_book_repairs_missing_state_files(tmp_path: Path, missing: str):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    getattr(store, f"_{missing}_path")(book_id).unlink()

    detection = store.create_request("detect")
    store.report_detection(
        detection["id"], source_id="B001", title="Book One",
        cursor_hash="sha256:recovered",
    )

    repaired = KindleStore(store.root).state()
    assert repaired["queue"]["items"] == []
    expected_cursor = "sha256:recovered" if missing == "cursor" else "sha256:screen-0"
    assert repaired["cursor"]["screen_hash"] == expected_cursor
    assert repaired["playback"]["status"] == "stopped"


def test_existing_book_repairs_partial_state_documents(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    store._write_json(store._queue_path(book_id), {"items": []})
    store._write_json(store._cursor_path(book_id), {})
    store._write_json(store._playback_path(book_id), {"offset": 7})

    detection = store.create_request("detect")
    store.report_detection(
        detection["id"], source_id="B001", title="Book One",
        cursor_hash="sha256:recovered",
    )

    state = store.state()
    assert state["queue"]["control_generation"] == 0
    assert state["queue"]["stopped"] is False
    assert state["queue"]["next_sequence"] == 1
    assert state["cursor"]["screen_hash"] == "sha256:recovered"
    assert state["playback"]["offset"] == 7
    assert state["playback"]["status"] == "stopped"


def test_legacy_playlist_migrates_without_deleting_source(tmp_path: Path):
    root = tmp_path / "kindle-reader"
    root.mkdir()
    (root / "chunk-004.mp3").write_bytes(b"ID3legacy")
    (root / "playlist.json").write_text(json.dumps({
        "complete": False,
        "items": [{"id": "chunk-004", "src": "chunk-004.mp3", "label": "Continuation 4", "duration": 12.0}],
    }))
    store = KindleStore(root)
    result = store.migrate_legacy(source_id="B0H2GWG3Y8", title="Co-Existence", author="Ethan Mollick")
    assert result["migrated"] == 1
    assert (root / "chunk-004.mp3").exists()
    book_id = store.state()["active_book"]["id"]
    assert store.audio_path(book_id, "chunk-004").read_bytes() == b"ID3legacy"



def test_append_requires_request_and_supplied_cursor_to_match(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:different-screen",
    )
    with pytest.raises(ConflictError, match="request cursor"):
        store.append_item(
            book_id, request_id=request["id"], expected_cursor_hash="sha256:screen-0",
            next_cursor_hash="sha256:screen-5", audio=b"ID3payload", transcript="text",
            duration=10, label="Mismatched request",
        )


def test_claim_token_fences_expired_worker_completion(tmp_path: Path, monkeypatch):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
    )
    monkeypatch.setattr("store.time.time", lambda: 100.0)
    first_claim = store.claim_request(request["id"], lease_seconds=5)
    assert first_claim["claim_token"]
    with pytest.raises(ConflictError, match="claim token"):
        store.append_item(
            book_id, request_id=request["id"], expected_cursor_hash="sha256:screen-0",
            next_cursor_hash="sha256:screen-5", audio=b"ID3payload", transcript="text",
            duration=10, label="Missing token",
        )
    monkeypatch.setattr("store.time.time", lambda: 106.0)
    store.state()
    second_claim = store.claim_request(request["id"], lease_seconds=5)
    assert second_claim["claim_token"] != first_claim["claim_token"]
    with pytest.raises(ConflictError, match="claim token"):
        store.append_item(
            book_id, request_id=request["id"], claim_token=first_claim["claim_token"],
            expected_cursor_hash="sha256:screen-0", next_cursor_hash="sha256:screen-5",
            audio=b"ID3payload", transcript="text", duration=10, label="Stale worker",
        )
    item = store.append_item(
        book_id, request_id=request["id"], claim_token=second_claim["claim_token"],
        expected_cursor_hash="sha256:screen-0", next_cursor_hash="sha256:screen-5",
        audio=b"ID3payload", transcript="text", duration=10, label="Current worker",
    )
    duplicate = store.append_item(
        book_id, request_id=request["id"], claim_token=second_claim["claim_token"],
        expected_cursor_hash="sha256:screen-0", next_cursor_hash="sha256:screen-5",
        audio=b"ID3payload", transcript="text", duration=10, label="Current worker",
    )
    assert duplicate["id"] == item["id"]


def test_detection_claim_token_fences_stale_worker(tmp_path: Path, monkeypatch):
    store = make_store(tmp_path)
    request = store.create_request("detect")
    monkeypatch.setattr("store.time.time", lambda: 100.0)
    first = store.claim_request(request["id"], lease_seconds=5)
    monkeypatch.setattr("store.time.time", lambda: 106.0)
    store.state()
    second = store.claim_request(request["id"], lease_seconds=5)
    with pytest.raises(ConflictError, match="claim token"):
        store.report_detection(
            request["id"], claim_token=first["claim_token"], source_id="STALE",
            title="Stale", cursor_hash="stale",
        )
    result = store.report_detection(
        request["id"], claim_token=second["claim_token"], source_id="CURRENT",
        title="Current", cursor_hash="current",
    )
    assert result["book_id"] == "kindle:CURRENT"


def test_stale_resume_after_stop_cannot_clear_durable_stop(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    stale_generation = store.queue(book_id)["control_generation"]

    stopped = store.create_request("stop", book_id=book_id)
    stopped_queue = store.queue(book_id)
    assert stopped["status"] == "completed"
    assert stopped_queue["stopped"] is True

    with pytest.raises(ConflictError, match="generation"):
        store.resume(book_id, expected_control_generation=stale_generation)

    still_stopped = KindleStore(store.root).queue(book_id)
    assert still_stopped["stopped"] is True
    assert still_stopped["control_generation"] == stopped_queue["control_generation"]

    resumed = store.resume(
        book_id, expected_control_generation=stopped_queue["control_generation"],
    )
    assert resumed["stopped"] is False
    assert resumed["control_generation"] > stopped_queue["control_generation"]


def test_stop_generation_fences_late_fill_until_resume(tmp_path: Path):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    resumed = store.resume(
        book_id, expected_control_generation=store.queue(book_id)["control_generation"],
    )
    generation = resumed["control_generation"]
    running = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
        control_generation=generation,
    )
    assert running["status"] == "pending"
    store.create_request("stop", book_id=book_id)
    late = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
        control_generation=generation,
    )
    fenced = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
        control_generation=generation + 1,
    )
    assert store.get_request(running["id"])["status"] == "cancelled"
    assert late["status"] == "cancelled"
    assert fenced["status"] == "cancelled"
    stopped_queue = store.queue(book_id)
    next_run = store.resume(
        book_id, expected_control_generation=stopped_queue["control_generation"],
    )
    assert next_run["stopped"] is False
    assert next_run["control_generation"] > generation
    fresh = store.create_request(
        "fill", book_id=book_id, expected_cursor_hash="sha256:screen-0",
        control_generation=next_run["control_generation"],
    )
    assert fresh["status"] == "pending"


def test_terminal_request_results_are_immutable_and_idempotent(tmp_path: Path):
    store = make_store(tmp_path)
    detection = store.create_request("detect")
    original = store.report_detection(
        detection["id"], source_id="B001", title="Original", cursor_hash="cursor",
    )
    replay = store.report_detection(
        detection["id"], source_id="B002", title="Mutated", cursor_hash="other",
    )
    assert replay == original
    assert [book["id"] for book in store.state()["books"]] == ["kindle:B001"]
    completed = store.get_request(detection["id"])
    assert store.fail_request(detection["id"], "late failure") == completed
    assert store.get_request(detection["id"]) == completed
    cancelled = store.create_request("fill", book_id="kindle:B001")
    store.create_request("stop", book_id="kindle:B001")
    before = store.get_request(cancelled["id"])
    assert store.fail_request(cancelled["id"], "late failure") == before
    assert store.get_request(cancelled["id"]) == before

    failed_detection = store.create_request("detect")
    store.fail_request(failed_detection["id"], "detection failed")
    with pytest.raises(ConflictError, match="terminal"):
        store.report_detection(
            failed_detection["id"], source_id="B003", title="Must not appear",
        )
    assert [book["id"] for book in store.state()["books"]] == ["kindle:B001"]



def test_fail_requires_current_claim_token_and_is_idempotent(tmp_path: Path, monkeypatch):
    store = make_store(tmp_path)
    book_id = activate_book(store)
    request = store.create_request("fill", book_id=book_id)
    monkeypatch.setattr("store.time.time", lambda: 100.0)
    first = store.claim_request(request["id"], lease_seconds=5)
    monkeypatch.setattr("store.time.time", lambda: 106.0)
    store.state()
    second = store.claim_request(request["id"], lease_seconds=5)
    with pytest.raises(ConflictError, match="claim token"):
        store.fail_request(request["id"], "stale", claim_token=first["claim_token"])
    failed = store.fail_request(request["id"], "current", claim_token=second["claim_token"])
    assert failed["status"] == "failed"
    assert store.fail_request(
        request["id"], "different", claim_token=second["claim_token"],
    ) == failed


def test_plugin_api_loads_plugin_store_by_unique_identity(tmp_path: Path, monkeypatch):
    import importlib.util
    import types

    class Router:
        def get(self, *_args, **_kwargs):
            return lambda function: function

        post = patch = get

    unrelated = types.ModuleType("store")
    unrelated.KindleStore = type("UnrelatedStore", (), {})
    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = Router
    fastapi.HTTPException = type("HTTPException", (Exception,), {})
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object
    pydantic.Field = lambda default=None, **_kwargs: default
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "store", unrelated)
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "pydantic", pydantic)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)

    api_path = Path(__file__).parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kindle_reader", api_path)
    assert spec is not None and spec.loader is not None
    plugin_api = importlib.util.module_from_spec(spec)
    before_path = list(sys.path)
    spec.loader.exec_module(plugin_api)

    assert plugin_api.KindleStore is not unrelated.KindleStore
    assert plugin_api.KindleStore.__module__ != "store"
    loaded_store = sys.modules[plugin_api.KindleStore.__module__]
    assert Path(loaded_store.__file__).resolve() == api_path.parents[1] / "store.py"
    assert sys.modules["store"] is unrelated
    assert sys.path == before_path


def test_legacy_api_payloads_include_ok(tmp_path: Path, monkeypatch):
    import importlib
    import sys
    import types

    class Router:
        def get(self, *_args, **_kwargs):
            return lambda function: function

        post = patch = get

    class HTTPException(Exception):
        def __init__(self, **kwargs):
            super().__init__(kwargs.get("detail"))

    fastapi = types.ModuleType("fastapi")
    fastapi.APIRouter = Router
    fastapi.HTTPException = HTTPException
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object
    pydantic.Field = lambda default=None, **_kwargs: default
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)
    monkeypatch.setitem(sys.modules, "pydantic", pydantic)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    sys.modules.pop("dashboard.plugin_api", None)
    plugin_api = importlib.import_module("dashboard.plugin_api")

    class LegacyStore:
        def state(self):
            return {"active_book": None}

        def _read_json(self, path):
            return json.loads(path.read_text(encoding="utf-8"))

    (tmp_path / "playlist.json").write_text(
        json.dumps({"items": [], "complete": False}), encoding="utf-8",
    )
    (tmp_path / "legacy.mp3").write_bytes(b"ID3legacy")
    monkeypatch.setattr(plugin_api, "_store", lambda: LegacyStore())
    monkeypatch.setattr(plugin_api, "_root", lambda: tmp_path)

    queue_payload = asyncio.run(plugin_api.queue())
    audio_payload = asyncio.run(plugin_api.audio("legacy"))
    assert queue_payload["ok"] is True
    assert audio_payload["ok"] is True


def test_cli_imports_work_in_isolated_package_context(tmp_path: Path, monkeypatch, capsys):
    package = tmp_path / "isolated_plugin"
    package.mkdir()
    root = Path(__file__).parents[1]
    for filename in ("__init__.py", "queue_cli.py", "store.py"):
        shutil.copy2(root / filename, package / filename)
    monkeypatch.syspath_prepend(tmp_path)
    constants = type(sys)("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    for name in ("isolated_plugin", "isolated_plugin.queue_cli", "isolated_plugin.store"):
        sys.modules.pop(name, None)

    plugin = importlib.import_module("isolated_plugin")
    parser = argparse.ArgumentParser()
    plugin.setup_cli(parser)
    queue_module = importlib.import_module("isolated_plugin.queue_cli")

    args = parser.parse_args(["state"])
    assert args.action == "state"
    assert queue_module.KindleStore.__module__ == "isolated_plugin.store"
    monkeypatch.setattr(queue_module, "_store", lambda: type("Store", (), {"state": lambda self: {}})())
    plugin.handle_cli(args)
    assert json.loads(capsys.readouterr().out) == {"ok": True}
