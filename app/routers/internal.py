"""Internal (api_key) endpoints for core-service, not exposed to the browser.

core is the sole web edge and reverse-proxies only ``/spectator`` and ``/games``.
Anything under ``/internal`` is reachable **only** by core over the docker network
(with the shared api_key), never by the public. Used for the homepage active-games
strip: a cheap, engine-free roster/turn summary of games currently in progress.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Query

from app.config import settings
from app.game.fetcher import get_save_dict
from app.game.native_stats import compute_income_native
from app.game.parser import encode_save
from app.routers.spectator import (
    _backup_folder, _demographics_by_civ, _has_live_save, _player_civs,
    _politics, _resolve_uuid, _session_statuses, _tile_owner_by_position,
    _ENDED_STATUSES,
)

router = APIRouter(prefix="/internal", tags=["internal"])
log = logging.getLogger(__name__)

_HOST_DESC = "External Unciv host; when set, read the game via its API not local files"

# Дефолты Unciv для флагов отображения: Unciv при сериализации сейва ОПУСКАЕТ
# поля, равные своему дефолту (showCharts/showRankings по умолчанию true), поэтому
# отсутствие ключа ≠ false. Зеркалит core.stats_mode._UNCIV_FLAG_DEFAULTS —
# дублируется тут, т.к. game-service не импортирует core.
_UNCIV_FLAG_DEFAULTS = {
    "showCharts": True, "showRankings": True,
    "showDemographics": False, "showCivilizationStats": False,
}


def _stats_mode(gp: dict) -> str:
    """Режим отображения (full/demographics/none) из gameParameters живого сейва —
    authoritative источник. Зеркалит core.stats_mode.mode_from_flags."""
    gp = gp or {}

    def flag(key: str) -> bool:
        v = gp.get(key)
        return _UNCIV_FLAG_DEFAULTS[key] if v is None else bool(v)

    charts, demo, rankings = flag("showCharts"), flag("showDemographics"), flag("showRankings")
    if rankings and charts:
        return "full"
    if demo:
        return "demographics"
    if not charts and not demo and not rankings:
        return "none"
    return "demographics"  # DEFAULT_STATS_MODE


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


async def _summarize_game(name: str, status: str, host: str | None = None) -> dict | None:
    """Сводка одной игры из живого сейва (roster/turn/map). None если сейва нет.

    ``host`` задан (внешняя игра) → сейв тянем через API этого хоста, а не из
    локальных MultiplayerFiles (их для external у нас нет). UUID берём из локального
    бэкапа (бэкапы всегда храним у себя)."""
    try:
        folder = _backup_folder(name)
    except Exception:
        return None
    uuid = _resolve_uuid(folder)
    if not uuid:
        return None
    # Локальный live-сейв обязателен только для наших игр; у external его нет.
    if not host and not _has_live_save(uuid):
        return None
    try:
        save = await get_save_dict(uuid, host)
    except Exception:
        log.warning("summary: не смог прочитать live-сейв %s", name, exc_info=True)
        return None
    return {
        "name": name,
        "status": status,
        "turn": int(save.get("turns") or 0),
        "currentPlayer": save.get("currentPlayer"),
        "currentTurnStartTime": int(save.get("currentTurnStartTime") or 0),
        "mapType": _map_type(save),
        "players": _roster(save),
    }


@router.get("/active-summary", summary="Cheap roster/turn summary of in-progress games")
async def active_summary() -> dict:
    """For every non-ended game with a live save: name, map type, current turn,
    whose turn it is, and the human roster. No native engine (no score/income)."""
    statuses = _session_statuses()
    games: list[dict] = []
    for name, status in statuses.items():
        if status in _ENDED_STATUSES:
            continue
        g = await _summarize_game(name, status)
        if g is not None:
            games.append(g)
    return {"games": games}


@router.get("/game-summary/{name}", summary="Сводка одной игры (в т.ч. завершённой)")
async def game_summary(
    name: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
) -> dict:
    """Как active-summary, но для одной игры и без фильтра ended (для страницы
    завершённой игры). ``host`` задан → сейв читаем с внешнего хоста (external-игра).
    Если живого сейва нет — минимальный объект из индекса."""
    status = _session_statuses().get(name, "ended")
    g = await _summarize_game(name, status, host)
    if g is None:
        g = {"name": name, "status": status, "turn": 0, "currentPlayer": None,
             "currentTurnStartTime": 0, "mapType": "—", "players": []}
    return g


@router.post("/purge-game", summary="Удалить файлы игры: сейв, preview, бэкапы, ротация")
async def purge_game(body: dict = Body(default={})) -> dict:
    """Стирает сейв/preview (по game_id) и папки бэкапов/ротации (по имени).
    Гейт на «game*»-имена — на стороне core; здесь только чистая зачистка файлов."""
    import os
    import shutil

    name = str(body.get("name") or "").strip().strip("/")
    game_id = str(body.get("game_id") or "").strip().strip("/")
    if not name or "/" in name or ".." in name:
        raise HTTPException(status_code=400, detail="bad name")

    civ = settings.civ_path.rstrip("/")
    bpath = settings.get_backup_path().rstrip("/")
    targets: list[tuple[str, str]] = []
    if game_id and "/" not in game_id and ".." not in game_id:
        # Сейвы лежат в civ/MultiplayerFiles/<id> (+ _Preview); плюс файловые
        # ротации в MultiplayerFiles_early_turns / _zero_turns.
        for sub in ("MultiplayerFiles", "MultiplayerFiles_early_turns",
                    "MultiplayerFiles_zero_turns"):
            targets.append(("file", os.path.join(civ, sub, game_id)))
            targets.append(("file", os.path.join(civ, sub, f"{game_id}_Preview")))
    targets.append(("dir", os.path.join(bpath, name)))
    targets.append(("dir", os.path.join(bpath, "rotate", name)))

    removed: list[str] = []
    for kind, path in targets:
        try:
            if kind == "file" and os.path.isfile(path):
                os.remove(path)
                removed.append(path)
            elif kind == "dir" and os.path.isdir(path):
                shutil.rmtree(path)
                removed.append(path)
        except OSError:
            log.exception("purge-game: не удалось удалить %s", path)
    log.info("purge-game[%s]: удалено %d — %s", name, len(removed), removed)
    return {"name": name, "removed": removed}


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
        # libGDX опускает дефолтные значения: если shape нет — это Hexagonal
        # (дефолт Unciv MapShape).
        "shape": mp.get("shape") or "Hexagonal",
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
async def score(
    name: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
) -> dict:
    """Ленивый расчёт счёта (getStatForRanking → Score) движком для live-сейва.
    Тяжёлый путь — зовётся только по кнопке на карточке главной. ``host`` задан
    (external) → сейв тянем с внешнего хоста (локального файла у нас нет)."""
    try:
        folder = _backup_folder(name)
    except Exception:
        raise HTTPException(status_code=404, detail="game not found")
    uuid = _resolve_uuid(folder)
    if not uuid or (not host and not _has_live_save(uuid)):
        raise HTTPException(status_code=404, detail="no live save")
    save = await get_save_dict(uuid, host)
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


@router.get("/stats/{name}", summary="Полная статистика + демография активной игры (движок)")
async def stats(
    name: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
) -> dict:
    """Пер-игрок ranking (окно «Статистика») + demographics (окно «Демография»)
    + данные окна «Политика» (эра, принятые институты, здания городов для
    классификации чудес на фронте, природные чудеса, дип-отношения) для
    live-сейва — те же значения, что игрок видит в игре. Не палит карту/юниты,
    только агрегаты. Отдаётся ТОЛЬКО для 1v1 (см. гейт ниже). Используется
    плитками на странице дуэли. ``host`` (external) → сейв с внешнего хоста."""
    try:
        folder = _backup_folder(name)
    except Exception:
        raise HTTPException(status_code=404, detail="game not found")
    uuid = _resolve_uuid(folder)
    if not uuid or (not host and not _has_live_save(uuid)):
        raise HTTPException(status_code=404, detail="no live save")
    save = await get_save_dict(uuid, host)
    human = {p["nation"] for p in _roster(save)}
    # Head-to-head отдаём ТОЛЬКО для 1v1 (ровно два человека по сетапу игры).
    # Это authoritative-гейт по реальной форме партии: отсекает FFA-турниры —
    # их тоже метят TOURNAMENT, но людей там больше двух, и тянуть чужую
    # статистику нельзя. _roster считает всех людей (живых и выбывших), так что
    # FFA, ужатый выбыванием до двух, всё равно остаётся FFA и закрыт.
    if len(human) != 2:
        return {"name": name, "players": []}
    income = compute_income_native(encode_save(save)) or {}
    demo_by_civ = _demographics_by_civ(save)

    # ── Данные окна «Политика» (сырьё; классификация чудес/ветвей/лидера — на
    # фронте через ruleset-метаданные вьюера) ────────────────────────────────
    relations = _politics(save)                 # чистый парс сейва
    owners = _tile_owner_by_position(save)
    buildings_by_civ: dict[str, list[str]] = {}
    policies_by_civ: dict[str, list[str]] = {}
    for civ in save.get("civilizations") or []:
        cn = civ.get("civName")
        if not cn:
            continue
        blds: list[str] = []
        for city in civ.get("cities") or []:
            blds.extend((city.get("cityConstructions") or {}).get("builtBuildings") or [])
        if blds:
            buildings_by_civ[cn] = blds
        policies_by_civ[cn] = list(((civ.get("policies") or {}).get("adoptedPolicies")) or [])
    # Природные чудеса в политике показываем ТОЛЬКО если ОБА игрока их обнаружили
    # (исследовали хотя бы один тайл чуда — Unciv Tile.exploredBy). Иначе скрываем
    # совсем: не выдаём ни владельца, ни сам факт чуда — иначе это слило бы
    # оппоненту, что у соперника есть чудо (и какое). Фильтр на бэке → прямой
    # запрос API тоже ничего лишнего не вернёт. Владелец чуда = владелец тайла.
    nw_explorers: dict[str, set[str]] = {}
    nw_owner: dict[str, str] = {}
    for tile in (save.get("tileMap") or {}).get("tileList") or []:
        if not isinstance(tile, dict):
            continue
        nw = tile.get("naturalWonder")
        if not nw:
            continue
        nw_explorers.setdefault(nw, set()).update(tile.get("exploredBy") or [])
        pos = tile.get("position") or {}
        owner = owners.get((pos.get("x", 0), pos.get("y", 0)))
        if owner and nw not in nw_owner:
            nw_owner[nw] = owner
    natural_by_civ: dict[str, list[str]] = {}
    for nw, owner in nw_owner.items():
        if human <= nw_explorers.get(nw, set()):  # оба игрока обнаружили чудо
            natural_by_civ.setdefault(owner, []).append(nw)

    players = []
    for civ_name, inc in income.items():
        if civ_name not in human:
            continue
        ranking = {k: int(v) for k, v in ((inc or {}).get("ranking") or {}).items()}
        demo = {k: int(v) for k, v in (demo_by_civ.get(civ_name) or {}).items()}
        players.append({
            "nation": civ_name,
            "ranking": ranking,
            "demographics": demo,
            "major": bool((inc or {}).get("major", False)),
            "alive": bool((inc or {}).get("alive", True)),
            # Политика:
            "era": str((inc or {}).get("era") or ""),
            "policies": policies_by_civ.get(civ_name, []),
            "buildings": buildings_by_civ.get(civ_name, []),
            "naturalWonders": natural_by_civ.get(civ_name, []),
            "relations": relations.get(civ_name) or {
                "wars": [], "friends": [], "denounced": [], "pacts": [], "allies": [],
            },
        })
    game_params = save.get("gameParameters") or {}
    # Defense-in-depth: отдаём ТОЛЬКО то, что разрешает режим отображения игры,
    # чтобы прямой запрос к API нельзя было использовать в обход плитки (напр.
    # вытащить полный ranking в игре с режимом «только демография»). Режим —
    # authoritative, из флагов живого сейва. Поля «Политики» (era/policies/
    # relations/…) от режима НЕ зависят — это отдельная плитка, их не трогаем.
    mode = _stats_mode(game_params)
    if mode != "full":
        for p in players:
            p["ranking"] = {}
    if mode == "none":
        for p in players:
            p["demographics"] = {}
    return {
        "name": name,
        "players": players,
        "baseRuleset": game_params.get("baseRuleset"),
        "mods": [m for m in (game_params.get("mods") or []) if m],
    }
