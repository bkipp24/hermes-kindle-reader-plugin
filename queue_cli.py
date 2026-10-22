from __future__ import annotations
import argparse, base64, json
from pathlib import Path
from hermes_constants import get_hermes_home

def setup_cli(parser) -> None:
    parser.add_argument("action", nargs="?", choices=["queue"], default="queue")
    parser.add_argument("--json", action="store_true")

def _queue_dir() -> Path:
    return get_hermes_home() / "plugin-data" / "kindle-reader"

def handle_cli(args) -> None:
    root = _queue_dir()
    try:
        manifest = json.loads((root / "playlist.json").read_text(encoding="utf-8"))
        items = []
        for raw in manifest.get("items", []):
            path = root / Path(str(raw["src"])).name
            if not path.is_file():
                continue
            items.append({
                "id": str(raw["id"]),
                "label": str(raw.get("label") or raw["id"]),
                "duration": float(raw.get("duration") or 0),
                "audio": base64.b64encode(path.read_bytes()).decode("ascii"),
            })
        payload = {"ok": True, "complete": bool(manifest.get("complete")), "items": items}
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "items": []}
    print(json.dumps(payload, separators=(",", ":")))
