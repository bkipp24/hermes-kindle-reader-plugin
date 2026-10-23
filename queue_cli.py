from __future__ import annotations

import argparse
import base64
import json
import math
from pathlib import Path

from hermes_constants import get_hermes_home

try:
    from .store import KindleStore
except ImportError:  # direct script/test execution
    if __package__:
        raise
    from store import KindleStore

PART_BYTES = 192 * 1024
ACTIONS = ["queue", "audio", "state", "claim", "report-detection", "append", "fail", "migrate"]


def setup_cli(parser) -> None:
    parser.add_argument("action", nargs="?", choices=ACTIONS, default="queue")
    parser.add_argument("--id")
    parser.add_argument("--part", type=int, default=0)
    parser.add_argument("--request-id")
    parser.add_argument("--claim-token")
    parser.add_argument("--book-id")
    parser.add_argument("--source-id")
    parser.add_argument("--title")
    parser.add_argument("--author", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--cursor-hash")
    parser.add_argument("--next-cursor-hash")
    parser.add_argument("--audio-file")
    parser.add_argument("--transcript-file")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--label")
    parser.add_argument("--error")
    parser.add_argument("--json", action="store_true")


def _queue_dir() -> Path:
    return get_hermes_home() / "plugin-data" / "kindle-reader"


def _store() -> KindleStore:
    return KindleStore(_queue_dir())


def _legacy_manifest(root: Path) -> dict:
    return json.loads((root / "playlist.json").read_text(encoding="utf-8"))


def _legacy_items(root: Path, manifest: dict) -> list[dict]:
    items = []
    for raw in manifest.get("items", []):
        path = root / Path(str(raw["src"])).name
        if path.is_file():
            size = path.stat().st_size
            items.append({"id": str(raw["id"]), "label": str(raw.get("label") or raw["id"]),
                          "duration": float(raw.get("duration") or 0), "bytes": size,
                          "parts": math.ceil(size / PART_BYTES)})
    return items


def _required(args, *names: str) -> None:
    missing = [name for name in names if getattr(args, name.replace("-", "_"), None) in {None, ""}]
    if missing:
        raise ValueError("missing required options: " + ", ".join("--" + name for name in missing))


def _active_queue(store: KindleStore) -> tuple[str | None, dict]:
    state = store.state()
    if state.get("active_book"):
        book_id = state["active_book"]["id"]
        return book_id, store.queue(book_id)
    return None, _legacy_manifest(_queue_dir())


def handle_cli(args) -> None:
    try:
        root = _queue_dir()
        store = _store()
        action = args.action
        if action == "state":
            payload = {"ok": True, **store.state()}
        elif action == "claim":
            _required(args, "request-id")
            payload = {"ok": True, "request": store.claim_request(args.request_id)}
        elif action == "report-detection":
            _required(args, "request-id", "source-id", "title")
            payload = {"ok": True, **store.report_detection(
                args.request_id, source_id=args.source_id, title=args.title,
                author=args.author, source_url=args.source_url, cursor_hash=args.cursor_hash,
                claim_token=args.claim_token,
            )}
        elif action == "append":
            _required(args, "request-id", "book-id", "cursor-hash", "next-cursor-hash",
                      "audio-file", "transcript-file", "duration", "label")
            audio_path = Path(args.audio_file)
            text_path = Path(args.transcript_file)
            item = store.append_item(
                args.book_id, request_id=args.request_id,
                expected_cursor_hash=args.cursor_hash, next_cursor_hash=args.next_cursor_hash,
                audio=audio_path.read_bytes(), transcript=text_path.read_text(encoding="utf-8"),
                duration=args.duration, label=args.label, claim_token=args.claim_token,
            )
            payload = {"ok": True, "item": item}
        elif action == "fail":
            _required(args, "request-id", "error")
            payload = {"ok": True, "request": store.fail_request(args.request_id, args.error, claim_token=args.claim_token)}
        elif action == "migrate":
            _required(args, "source-id", "title")
            payload = {"ok": True, **store.migrate_legacy(source_id=args.source_id, title=args.title, author=args.author, cursor_hash=args.cursor_hash)}
        elif action == "queue":
            book_id, manifest = _active_queue(store)
            if book_id:
                payload = {"ok": True, "complete": bool(manifest.get("complete")), "book_id": book_id,
                           "items": [{**item, "parts": math.ceil(item["bytes"] / PART_BYTES)} for item in manifest.get("items", [])]}
            else:
                payload = {"ok": True, "complete": bool(manifest.get("complete")), "items": _legacy_items(root, manifest)}
        else:
            _required(args, "id")
            book_id, manifest = _active_queue(store)
            if book_id:
                path = store.audio_path(book_id, args.id)
            else:
                wanted = next((raw for raw in manifest.get("items", []) if str(raw.get("id")) == str(args.id)), None)
                if wanted is None:
                    raise ValueError(f"unknown queue item: {args.id}")
                path = root / Path(str(wanted["src"])).name
            data = path.read_bytes()
            total = math.ceil(len(data) / PART_BYTES)
            if args.part < 0 or args.part >= total:
                raise ValueError(f"part {args.part} outside 0..{total - 1}")
            start = args.part * PART_BYTES
            payload = {"ok": True, "id": str(args.id), "part": args.part, "total": total,
                       "data": base64.b64encode(data[start:start + PART_BYTES]).decode("ascii")}
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(payload, separators=(",", ":")))
