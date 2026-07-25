"""Game file access: local MultiplayerFiles + MP server fetch."""
import os
from pathlib import Path

import httpx

from app.config import settings
from app.game.parser import decode_save


def _local_path(game_id: str) -> Path:
    return Path(settings.civ_path) / "MultiplayerFiles" / game_id


def _preview_path(game_id: str) -> Path:
    return Path(settings.civ_path) / "MultiplayerFiles" / f"{game_id}_Preview"


async def _fetch_from_server(game_id: str, mp_server_url: str) -> str:
    url = f"{mp_server_url.rstrip('/')}/files/{game_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


async def get_save_dict(game_id: str, mp_server_url: str | None = None) -> dict:
    """Return parsed save dict from local file, or MP server if absent."""
    path = _local_path(game_id)
    if path.is_file():
        raw = path.read_text(encoding="utf-8")
    elif mp_server_url:
        raw = await _fetch_from_server(game_id, mp_server_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
    else:
        raise FileNotFoundError(f"Game file not found locally and no mp_server_url given: {game_id}")
    return decode_save(raw.strip())


async def get_preview_dict(game_id: str) -> dict:
    path = _preview_path(game_id)
    if not path.is_file():
        raise FileNotFoundError(f"Preview file not found: {game_id}_Preview")
    raw = path.read_text(encoding="utf-8")
    return decode_save(raw.strip())


def write_save(game_id: str, raw: str) -> None:
    """Atomically write a save file to MultiplayerFiles."""
    path = _local_path(game_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.rename(path)


def write_preview(game_id: str, raw: str) -> None:
    path = _preview_path(game_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.rename(path)
