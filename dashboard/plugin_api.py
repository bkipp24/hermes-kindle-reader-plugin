"""Profile-scoped queue API for the Kindle Reader desktop half."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from hermes_constants import get_hermes_home

router = APIRouter()


def _root() -> Path:
    return get_hermes_home() / "plugin-data" / "kindle-reader"


def _manifest() -> dict:
    return json.loads((_root() / "playlist.json").read_text(encoding="utf-8"))


def _find(item_id: str) -> tuple[dict, Path]:
    raw = next((item for item in _manifest().get("items", []) if str(item.get("id")) == item_id), None)
    if raw is None:
        raise HTTPException(status_code=404, detail="Queue item not found")
    path = _root() / Path(str(raw["src"])).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return raw, path


@router.get("/queue")
async def queue():
    manifest = _manifest()
    items = []
    for raw in manifest.get("items", []):
        path = _root() / Path(str(raw["src"])).name
        if path.is_file():
            items.append({
                "id": str(raw["id"]),
                "label": str(raw.get("label") or raw["id"]),
                "duration": float(raw.get("duration") or 0),
                "bytes": path.stat().st_size,
            })
    return {"ok": True, "complete": bool(manifest.get("complete")), "items": items}


@router.get("/audio/{item_id}")
async def audio(item_id: str):
    _raw, path = _find(item_id)
    return {
        "ok": True,
        "id": item_id,
        "audio": base64.b64encode(path.read_bytes()).decode("ascii"),
    }
