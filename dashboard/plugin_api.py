from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes_constants import get_hermes_home

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_STORE_PATH = PLUGIN_ROOT / "store.py"
_STORE_MODULE_NAME = "_hermes_plugin_kindle_reader_{}_store".format(
    hashlib.sha256(str(PLUGIN_ROOT).encode("utf-8")).hexdigest()[:16]
)


def _load_store_module():
    existing = sys.modules.get(_STORE_MODULE_NAME)
    if existing is not None:
        if Path(existing.__file__).resolve() != _STORE_PATH:
            raise ImportError(f"module identity collision for {_STORE_PATH}")
        return existing
    spec = importlib.util.spec_from_file_location(_STORE_MODULE_NAME, _STORE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin store from {_STORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_STORE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_STORE_MODULE_NAME, None)
        raise
    return module


_store_module = _load_store_module()
ConflictError = _store_module.ConflictError
KindleStore = _store_module.KindleStore
NotFoundError = _store_module.NotFoundError

router = APIRouter()


def _root() -> Path:
    return get_hermes_home() / "plugin-data" / "kindle-reader"


def _store() -> KindleStore:
    return KindleStore(_root())


def _raise(error: Exception):
    if isinstance(error, NotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, ConflictError):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=422, detail=str(error)) from error
    raise error


class RequestCreate(BaseModel):
    action: Literal["detect", "fill", "recover", "stop"]
    book_id: str | None = None
    controller_session_id: str | None = None
    controller_stored_session_id: str | None = None
    expected_cursor_hash: str | None = None
    target_screens: int = Field(default=5, ge=1, le=20)
    control_generation: int | None = Field(default=None, ge=0)


class ResumeRequest(BaseModel):
    control_generation: int | None = Field(default=None, ge=0)


class PlaybackUpdate(BaseModel):
    item_id: str | None = None
    offset: float = Field(default=0, ge=0)
    status: Literal["playing", "paused", "stopped"]


class SwitchConfirm(BaseModel):
    target_book_id: str
    restart: bool = False


@router.get("/state")
async def state():
    try:
        return _store().state()
    except Exception as exc:
        return _raise(exc)


@router.post("/requests")
async def create_request(body: RequestCreate):
    try:
        return _store().create_request(**body.model_dump())
    except Exception as exc:
        return _raise(exc)


@router.get("/requests/{request_id}")
async def request_status(request_id: str):
    try:
        return _store().get_request(request_id)
    except Exception as exc:
        return _raise(exc)


@router.post("/requests/{request_id}/cancel")
async def cancel_request(request_id: str):
    try:
        return _store().cancel_request(request_id)
    except Exception as exc:
        return _raise(exc)


@router.post("/books/{book_id}/resume")
async def resume(book_id: str, body: ResumeRequest | None = None):
    try:
        return _store().resume(
            book_id,
            expected_control_generation=body.control_generation if body else None,
        )
    except Exception as exc:
        return _raise(exc)


@router.get("/books/{book_id}/queue")
async def book_queue(book_id: str):
    try:
        return _store().queue(book_id)
    except Exception as exc:
        return _raise(exc)


@router.get("/books/{book_id}/audio/{item_id}")
async def book_audio(book_id: str, item_id: str):
    try:
        path = _store().audio_path(book_id, item_id)
        if not path.is_file():
            raise NotFoundError("audio not found")
        return {"ok": True, "id": item_id, "mime": "audio/mpeg", "audio": base64.b64encode(path.read_bytes()).decode("ascii")}
    except Exception as exc:
        return _raise(exc)


@router.patch("/books/{book_id}/playback")
async def update_playback(book_id: str, body: PlaybackUpdate):
    try:
        return _store().checkpoint(book_id, body.item_id, body.offset, body.status)
    except Exception as exc:
        return _raise(exc)


@router.post("/books/{book_id}/completed/{item_id}")
async def complete(book_id: str, item_id: str):
    try:
        return _store().complete_item(book_id, item_id)
    except Exception as exc:
        return _raise(exc)


@router.post("/switch/confirm")
async def confirm_switch(body: SwitchConfirm):
    try:
        return _store().confirm_switch(body.target_book_id, restart=body.restart)
    except Exception as exc:
        return _raise(exc)


@router.post("/switch/cancel")
async def cancel_switch():
    try:
        _store().cancel_switch()
        return {"ok": True}
    except Exception as exc:
        return _raise(exc)


# v1 compatibility: keep active playback alive during migration.
@router.get("/queue")
async def queue():
    store = _store()
    current = store.state()
    if current.get("active_book"):
        return {"ok": True, **store.queue(current["active_book"]["id"])}
    path = _root() / "playlist.json"
    if not path.is_file():
        return {"ok": True, "items": [], "complete": False}
    return {**store._read_json(path), "ok": True}


@router.get("/audio/{item_id}")
async def audio(item_id: str):
    if Path(item_id).name != item_id:
        raise HTTPException(status_code=400, detail="Invalid item id")
    store = _store()
    current = store.state()
    path = store.audio_path(current["active_book"]["id"], item_id) if current.get("active_book") else _root() / f"{item_id}.mp3"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio not found")
    return {"ok": True, "id": item_id, "mime": "audio/mpeg", "audio": base64.b64encode(path.read_bytes()).decode("ascii")}
