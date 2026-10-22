from __future__ import annotations
import argparse, base64, json, math
from pathlib import Path
from hermes_constants import get_hermes_home

PART_BYTES = 192 * 1024

def setup_cli(parser) -> None:
    parser.add_argument("action", nargs="?", choices=["queue", "audio"], default="queue")
    parser.add_argument("--id")
    parser.add_argument("--part", type=int, default=0)
    parser.add_argument("--json", action="store_true")

def _queue_dir() -> Path:
    return get_hermes_home() / "plugin-data" / "kindle-reader"

def _manifest(root: Path) -> dict:
    return json.loads((root / "playlist.json").read_text(encoding="utf-8"))

def _items(root: Path, manifest: dict) -> list[dict]:
    items = []
    for raw in manifest.get("items", []):
        path = root / Path(str(raw["src"])).name
        if not path.is_file():
            continue
        size = path.stat().st_size
        items.append({
            "id": str(raw["id"]),
            "label": str(raw.get("label") or raw["id"]),
            "duration": float(raw.get("duration") or 0),
            "bytes": size,
            "parts": math.ceil(size / PART_BYTES),
        })
    return items

def handle_cli(args) -> None:
    root = _queue_dir()
    try:
        manifest = _manifest(root)
        if args.action == "queue":
            payload = {"ok": True, "complete": bool(manifest.get("complete")), "items": _items(root, manifest)}
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
            payload = {
                "ok": True, "id": str(args.id), "part": args.part, "total": total,
                "data": base64.b64encode(data[start:start + PART_BYTES]).decode("ascii"),
            }
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(payload, separators=(",", ":")))
