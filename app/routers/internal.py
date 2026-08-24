"""Internal (api_key) endpoints for core-service, not exposed to the browser.

core is the sole web edge and reverse-proxies only ``/spectator`` and ``/games``.
Anything under ``/internal`` is reachable **only** by core over the docker network
(with the shared api_key), never by the public. Used for the homepage active-games
strip: a cheap, engine-free roster/turn summary of games currently in progress.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.game.fetcher import get_save_dict
from app.game.native_stats import compute_income_native
from app.game.parser import encode_save
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
            "currentTurnStartTime": int(save.get("currentTurnStartTime") or 0),
            "mapType": _map_type(save),
            "players": _roster(save),
        })
    return {"games": games}


def _version_label(save: dict) -> str:
    """Версия Unciv из ``version.createdWith`` в формате как в Games.json."""
    created = (save.get("version") or {}).get("createdWith") or {}
    text = str(created.get("text") or "").strip()
    build = str(created.get("number") or "").strip()
    if text and build:
        return f"{text} (Build {build})"
    return text or build


@router.get("/game-meta/{name}", summary="Мета активной игры из живого сейва")
async def game_meta(name: str) -> dict:
    """Версия Unciv, форма/тип/размер карты, world-wrap, рулсет/моды, скорость,
    сложность, типы победы — из живого сейва. Движок не зовём (дёшево)."""
    try:
        folder = _backup_folder(name)
    except Exception:
        raise HTTPException(status_code=404, detail="game not found")
    uuid = _resolve_uuid(folder)
    if not uuid or not _has_live_save(uuid):
        raise HTTPException(status_code=404, detail="no live save")
    save = await get_save_dict(uuid)
    mp = (save.get("tileMap") or {}).get("mapParameters") or {}
    size = mp.get("mapSize") or {}
    gp = save.get("gameParameters") or {}
    return {
        "name": name,
        "version": _version_label(save),
        "shape": mp.get("shape") or "",       # Hexagonal / Rectangular
        "genType": mp.get("type") or "",       # Perlin / Pangaea / …
        "sizeName": size.get("name") or "",    # Tiny…Huge / Custom
        "radius": size.get("radius") or 0,     # для Hexagonal
        "width": size.get("width") or 0,       # для Rectangular
        "height": size.get("height") or 0,
        "worldWrap": bool(mp.get("worldWrap", False)),
        "baseRuleset": gp.get("baseRuleset") or "",
        "mods": [m for m in (gp.get("mods") or []) if m],
        "gameSpeed": gp.get("gameSpeed") or gp.get("speed") or "",
        "difficulty": gp.get("difficulty") or "",
        "victoryTypes": [v for v in (gp.get("victoryTypes") or []) if v],
    }


@router.get("/score/{name}", summary="Актуальный счёт активной игры (движок, по клику)")
async def score(name: str) -> dict:
    """Ленивый расчёт счёта (getStatForRanking → Score) движком для live-сейва.
    Тяжёлый путь — зовётся только по кнопке на карточке главной."""
    try:
        folder = _backup_folder(name)
    except Exception:
        raise HTTPException(status_code=404, detail="game not found")
    uuid = _resolve_uuid(folder)
    if not uuid or not _has_live_save(uuid):
        raise HTTPException(status_code=404, detail="no live save")
    save = await get_save_dict(uuid)
    # только нации-игроки (люди), как в списке на карточке — без городов-государств
    human = {p["nation"] for p in _roster(save)}
    income = compute_income_native(encode_save(save)) or {}
    scores = []
    for civ_name, inc in income.items():
        if civ_name not in human:
            continue
        ranking = (inc or {}).get("ranking") or {}
        if "Score" in ranking:
            scores.append({"nation": civ_name, "score": int(ranking["Score"])})
    return {"name": name, "scores": scores}
