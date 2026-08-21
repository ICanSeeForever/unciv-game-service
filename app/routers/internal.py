"""Internal (api_key) endpoints for core-service, not exposed to the browser.

core is the sole web edge and reverse-proxies only ``/spectator`` and ``/games``.
Anything under ``/internal`` is reachable **only** by core over the docker network
(with the shared api_key), never by the public. Used for the homepage active-games
strip: a cheap, engine-free roster/turn summary of games currently in progress.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.game.fetcher import get_save_dict
from app.routers.spectator import (
    _backup_folder, _has_live_save, _player_civs, _resolve_uuid,
    _session_statuses, _ENDED_STATUSES,
)

router = APIRouter(prefix="/internal", tags=["internal"])
log = logging.getLogger(__name__)


def _map_type(save: dict) -> str:
    mp = (save.get("tileMap") or {}).get("mapParameters") or {}
    kind = mp.get("type") or ""
    size = (mp.get("mapSize") or {}).get("name") or ""
    return " · ".join(x for x in (kind, size) if x) or "—"


def _roster(save: dict) -> list[dict]:
    """Real (human) players: nation + playerId + alive. Colors/emblems the
    frontend resolves from its own bundled nation metadata."""
    alive_names = set(_player_civs(save))
    out: list[dict] = []
    for civ in save.get("civilizations") or []:
        if (civ.get("playerType") or "") != "Human":
            continue
        name = civ.get("civName")
        if not name or name in ("Spectator", "Barbarians"):
            continue
        out.append({
            "nation": name,
            "playerId": str(civ.get("playerId") or ""),
            "alive": name in alive_names,
        })
    return out


@router.get("/active-summary", summary="Cheap roster/turn summary of in-progress games")
async def active_summary() -> dict:
    """For every non-ended game with a live save: name, map type, current turn,
    whose turn it is, and the human roster. No native engine (no score/income)."""
    statuses = _session_statuses()
    games: list[dict] = []
    for name, status in statuses.items():
        if status in _ENDED_STATUSES:
            continue
        try:
            folder = _backup_folder(name)
        except Exception:
            continue
        uuid = _resolve_uuid(folder)
        if not uuid or not _has_live_save(uuid):
            continue
        try:
            save = await get_save_dict(uuid)
        except Exception:
            log.warning("active-summary: не смог прочитать live-сейв %s", name, exc_info=True)
            continue
        games.append({
            "name": name,
            "status": status,
            "turn": int(save.get("turns") or 0),
            "currentPlayer": save.get("currentPlayer"),
            "mapType": _map_type(save),
            "players": _roster(save),
        })
    return {"games": games}
