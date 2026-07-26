"""Game file access: local MultiplayerFiles + MP server fetch."""
import asyncio
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


def _parse_game_file(path: Path) -> dict | None:
    """Parse a single save file; return None on any error."""
    try:
        return decode_save(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


async def list_all_games(exclude_civs: frozenset[str]) -> list[dict]:
    """Scan MultiplayerFiles and return summary of every parseable game."""
    base = Path(settings.civ_path) / "MultiplayerFiles"
    if not base.is_dir():
        return []

    paths = [
        p for p in base.iterdir()
        if p.is_file() and not p.name.endswith("_Preview")
    ]

    loop = asyncio.get_event_loop()

    async def _load(path: Path) -> dict | None:
        game = await loop.run_in_executor(None, _parse_game_file, path)
        if game is None:
            return None
        civs = [
            c["civName"]
            for c in game.get("civilizations", [])
            if c.get("playerType") == "Human" and c.get("civName") not in exclude_civs
        ]
        return {
            "game_id": path.name,
            "current_player": game.get("currentPlayer"),
            "turns": game.get("turns") or 0,
            "human_civs": civs,
        }

    results = await asyncio.gather(*[_load(p) for p in paths])
    return [r for r in results if r is not None]
