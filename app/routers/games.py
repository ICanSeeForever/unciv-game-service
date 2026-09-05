"""Game endpoints: info, map-check, start."""
import asyncio
import json
import logging
import os
import re
import tarfile

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import settings
from app.game.fetcher import (
    get_save_dict, get_preview_dict, list_all_games, get_file_created_at,
    delete_game, patch_prophet, load_spectate_backup, write_save, write_preview,
    create_backup, restore_backup, make_single_player,
    store_save, store_preview, get_save_raw, get_preview_raw,
    extract_backup_contents,
)
from app.game.parser import align_preview_to_save, decode_save, encode_save
from app.game import remote
from app.game.static_data import CITY_STATES
from app.launchers import get_launcher
from app.services.map_checker import check_map
from app.services.task_manager import TaskStatus, create_task, get_start_lock, update_task

router = APIRouter(prefix="/games", tags=["games"], responses={404: {"description": "Game not found"}})

_GAME_ID_RE = re.compile(r"^[0-9a-f-]{32,}$", re.IGNORECASE)

# When set, the game lives on an external Unciv server: read/write go through its
# REST API (GET/PUT <host>/files/<id>) instead of the local MultiplayerFiles mount.
_HOST_DESC = "External Unciv host; when set, use its API instead of local files"


def _validate_game_id(game_id: str) -> None:
    if not _GAME_ID_RE.match(game_id):
        raise HTTPException(status_code=400, detail="Invalid game_id format")


# ---------------------------------------------------------------------------
# GET /games  — list all local games
# ---------------------------------------------------------------------------

_EXCLUDE_FROM_HUMAN_LIST = CITY_STATES | frozenset({"Spectator", "Barbarians"})


@router.get(
    "",
    summary="List all local games",
    description=(
        "Scans the mounted `CIV_PATH/MultiplayerFiles/` directory and returns a summary of every "
        "parseable game: human civilizations (city-states and Spectator excluded), "
        "current player, and turn number."
    ),
)
async def list_games():
    games = await list_all_games(exclude_civs=_EXCLUDE_FROM_HUMAN_LIST)
    return {"count": len(games), "games": games}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/info  — lightweight: currentPlayer + turns
# ---------------------------------------------------------------------------

@router.get(
    "/{game_id}/info",
    summary="Game info: current player, turns, speed, version, victory data, and full player list",
    description=(
        "Returns current player, turn number, game speed, version, victory data, "
        "and the list of all civilizations "
        "with their type (`is_human=true` for Human, `false` for AI/city-states). "
        "Barbarians are excluded."
    ),
)
async def game_info(
    game_id: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    players = [
        {
            "civ_name": c["civName"],
            "is_human": c.get("playerType") == "Human",
            "player_id": c.get("playerId"),
        }
        for c in save.get("civilizations", [])
        if c.get("civName") and c.get("civName") != "Barbarians"
    ]

    created_at = get_file_created_at(game_id)

    game_params = save.get("gameParameters") or {}
    version_raw = save.get("version") or {}
    created_with = version_raw.get("createdWith") or {}
    victory_data = save.get("victoryData") or {}

    return {
        "game_id": game_id,
        "current_player": save.get("currentPlayer"),
        "turns": save.get("turns") or 0,
        "created_at": created_at,
        "players": players,
        "speed": game_params.get("speed"),
        "version": {
            "text": created_with.get("text"),
            "number": created_with.get("number"),
        },
        "victory_data": {
            "winning_civ": victory_data.get("winningCiv"),
            "victory_type": victory_data.get("victoryType"),
        } if victory_data else None,
    }


# ---------------------------------------------------------------------------
# GET /games/{game_id}/map-check  — full map quality check
# ---------------------------------------------------------------------------

@router.get("/{game_id}/map-check", summary="Check map quality (distances, luxuries, marine civs, land ratio)")
async def map_check(
    game_id: str,
    min_distance: int | None = Query(default=None, description="Override minimum distance between start positions"),
    max_distance: int | None = Query(default=None, description="Override maximum distance between start positions"),
    min_luxuries: int | None = Query(default=None, description="Override minimum total luxuries in radius"),
    min_unique_luxuries: int | None = Query(default=None, description="Override minimum unique luxury types"),
    min_land_per_player: int | None = Query(default=None, description="Override minimum land per player"),
    max_land_per_player: int | None = Query(default=None, description="Override maximum land per player"),
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    result = check_map(
        save,
        min_distance=min_distance,
        max_distance=max_distance,
        min_luxuries=min_luxuries,
        min_unique_luxuries=min_unique_luxuries,
        min_land_per_player=min_land_per_player,
        max_land_per_player=max_land_per_player,
    )
    return {"ok": result.ok, "issues": result.issues, "details": result.details}


# ---------------------------------------------------------------------------
# Синхронизация игры между двумя хостами (host-sync)
# ---------------------------------------------------------------------------

@router.get("/{game_id}/progress",
            summary="Дешёвый прогресс хода (turns, currentPlayer) из превью")
async def game_progress(
    game_id: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    """Из превью (`<id>_Preview`, ~1 КБ) — номер хода и текущий игрок. Если превью
    нет — из основного сейва. Используется шедулером синхронизации для дешёвого
    детекта, не таща весь сейв."""
    _validate_game_id(game_id)
    raw = await get_preview_raw(game_id, host)
    source = "preview"
    if not raw:
        try:
            raw = await get_save_raw(game_id, host)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        source = "save"
    try:
        d = decode_save(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"decode failed: {e}")
    return {"turns": int(d.get("turns") or 0),
            "currentPlayer": d.get("currentPlayer"), "source": source}


class MirrorRequest(BaseModel):
    from_host: str | None = None   # None → локальный хост (наши MultiplayerFiles)
    to_host: str | None = None     # None → локальный хост


@router.post("/{game_id}/mirror",
             summary="Зеркалировать id+id_Preview с одного хоста на другой")
async def mirror_game(game_id: str, body: MirrorRequest):
    """Читает сырьё `id`+`id_Preview` с ``from_host`` и пишет как есть на ``to_host``
    (локально или PUT на external). Перед записью проверяет, что основной `id`
    согласован с превью (тот же ход/игрок) — иначе не зеркалит (`mirrored:false`),
    чтобы не тащить полу-обновлённое состояние."""
    _validate_game_id(game_id)
    from_host = body.from_host or None
    to_host = body.to_host or None
    try:
        save_raw = await get_save_raw(game_id, from_host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=f"source save missing: {e}")
    try:
        sd = decode_save(save_raw)
    except Exception as e:  # noqa: BLE001
        return {"mirrored": False, "reason": f"source save decode failed: {e}"}
    s_turns = int(sd.get("turns") or 0)
    s_cur = sd.get("currentPlayer")

    preview_raw = await get_preview_raw(game_id, from_host)
    if preview_raw:
        try:
            pd = decode_save(preview_raw)
        except Exception:  # noqa: BLE001
            return {"mirrored": False, "reason": "source preview decode failed"}
        if (int(pd.get("turns") or 0), pd.get("currentPlayer")) != (s_turns, s_cur):
            return {"mirrored": False, "reason": "save/preview inconsistent",
                    "save": {"turns": s_turns, "currentPlayer": s_cur},
                    "preview": {"turns": int(pd.get("turns") or 0),
                                "currentPlayer": pd.get("currentPlayer")}}

    await store_save(game_id, save_raw, host=to_host)
    if preview_raw:
        await store_preview(game_id, preview_raw, host=to_host)
    return {"mirrored": True, "turns": s_turns, "currentPlayer": s_cur,
            "hadPreview": bool(preview_raw)}


# ---------------------------------------------------------------------------
# DELETE /games/{game_id}  — delete game file and preview
# ---------------------------------------------------------------------------

@router.delete(
    "/{game_id}",
    summary="Delete game files",
    description="Deletes the main save file and preview file for the given game_id.",
)
async def delete_game_files(game_id: str,
                            host: str | None = Query(default=None, description=_HOST_DESC)):
    _validate_game_id(game_id)
    result = delete_game(game_id, host)
    if host:
        return result  # external: deletion is a no-op (skipped_external), never 404
    if not result["deleted_main"] and not result["deleted_preview"]:
        raise HTTPException(status_code=404, detail=f"No files found for game_id: {game_id}")
    return result


# ---------------------------------------------------------------------------
# GET /games/{game_id}/capitals  — original capitals for all civs
# ---------------------------------------------------------------------------

def _extract_capitals(save: dict) -> dict:
    """Return original capital info for all non-Barbarian civs."""
    civ_cities: dict[str, list] = {
        c["civName"]: c.get("cities") or []
        for c in save.get("civilizations", [])
        if c.get("civName") and c.get("civName") != "Barbarians"
    }
    all_cities: list[tuple[str, dict]] = [
        (owner, city)
        for owner, cities in civ_cities.items()
        for city in cities
    ]

    result = {}
    for nation in civ_cities:
        capital = None
        for _, city in all_cities:
            if city.get("foundingCiv") == nation and city.get("isOriginalCapital"):
                loc = city.get("location") or {}
                capital = {
                    "name": city.get("name"),
                    "x": loc.get("x"),
                    "y": loc.get("y"),
                    "current_owner": None,
                }
                break
        if capital is None:
            continue
        # find current owner
        for owner, city in all_cities:
            if city.get("name") == capital["name"]:
                loc = city.get("location") or {}
                if loc.get("x") == capital["x"] and loc.get("y") == capital["y"]:
                    capital["current_owner"] = owner
                    break
        result[nation] = capital

    return result


@router.get(
    "/{game_id}/capitals",
    summary="Original capitals for all civilizations",
    description=(
        "Returns each civilization's original (native) capital: name, tile coordinates, "
        "and current owner. `current_owner` differs from the nation key when the capital "
        "has been captured. Barbarians excluded."
    ),
)
async def game_capitals(
    game_id: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"game_id": game_id, "capitals": _extract_capitals(save)}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/units  — all units on the map (human civs only opt-in)
# ---------------------------------------------------------------------------

def _extract_units(save: dict) -> list:
    """Return all units from tileList with owner, name, location, health, xp, promos."""
    tiles = (save.get("tileMap") or {}).get("tileList") or []
    units = []
    for tile in tiles:
        pos = tile.get("position") or {}
        x, y = pos.get("x"), pos.get("y")
        for key in ("civilianUnit", "militaryUnit"):
            unit = tile.get(key)
            if not isinstance(unit, dict):
                continue
            promotions = unit.get("promotions") or {}
            if not isinstance(promotions, dict):
                promotions = {}
            xp = promotions.get("XP")
            promo_names = promotions.get("promotions") or []
            promo_count = promotions.get("numberOfPromotions")
            health = unit.get("health")
            units.append({
                "id": unit.get("id"),
                "name": unit.get("name"),
                "owner": unit.get("owner") or unit.get("originalOwner"),
                "x": x,
                "y": y,
                "health": int(health) if health is not None else 100,
                "xp": int(xp) if xp is not None else 0,
                "promotions": promo_names,
                "promotion_count": int(promo_count) if promo_count is not None else 0,
            })
    return units


@router.get(
    "/{game_id}/units",
    summary="All units on the map",
    description=(
        "Returns every unit on the tile map: owner, name, tile coordinates, health, "
        "XP, and promotions. Use `owner` filter to get units of a specific civ."
    ),
)
async def game_units(
    game_id: str,
    owner: str | None = Query(default=None, description="Filter by civ name"),
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    units = _extract_units(save)
    if owner:
        units = [u for u in units if u["owner"] == owner]
    return {"game_id": game_id, "count": len(units), "units": units}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/cities  — cities for all or one civ
# ---------------------------------------------------------------------------

def _extract_cities(save: dict, nation: str | None = None) -> list:
    """Return cities with owner, founding civ, location, and buildings count."""
    result = []
    for civ in save.get("civilizations", []):
        owner = civ.get("civName")
        if not owner or owner == "Barbarians":
            continue
        if nation and owner != nation:
            continue
        for city in civ.get("cities") or []:
            loc = city.get("location") or {}
            constructions = city.get("cityConstructions") or {}
            built = constructions.get("builtBuildings") if isinstance(constructions, dict) else []
            result.append({
                "id": city.get("id"),
                "name": city.get("name"),
                "owner": owner,
                "founding_civ": city.get("foundingCiv"),
                "is_original_capital": bool(city.get("isOriginalCapital")),
                "x": loc.get("x"),
                "y": loc.get("y"),
                "population": (city.get("population") or {}).get("population") if isinstance(city.get("population"), dict) else city.get("population"),
                "built_buildings": built or [],
            })
    return result


@router.get(
    "/{game_id}/cities",
    summary="All cities (optionally filtered by civ)",
    description=(
        "Returns all cities across all civs, or just one civ's cities via `?nation=`. "
        "Includes founding civ, original-capital flag, population, and built buildings. "
        "Used by irrelevant_guard to check if a civ lost half its founded cities."
    ),
)
async def game_cities(
    game_id: str,
    nation: str | None = Query(default=None, description="Filter by civ name"),
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    cities = _extract_cities(save, nation)
    return {"game_id": game_id, "count": len(cities), "cities": cities}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/diplomacy  — diplomatic relations between all civs
# ---------------------------------------------------------------------------

def _extract_diplomacy(save: dict) -> list:
    """Return all war/peace relations between civs."""
    result = []
    seen = set()
    for civ in save.get("civilizations", []):
        nation = civ.get("civName")
        if not nation or nation == "Barbarians":
            continue
        diplomacy = civ.get("diplomacy")
        if not isinstance(diplomacy, dict):
            continue
        for key, row in diplomacy.items():
            if not isinstance(row, dict):
                continue
            other = row.get("otherCivName") or key
            if not other or other == "Barbarians":
                continue
            pair = tuple(sorted([nation, other]))
            if pair in seen:
                continue
            seen.add(pair)
            status = row.get("diplomaticStatus")
            flags = row.get("flagsCountdown") or {}
            at_war = status == "War" or (
                isinstance(flags, dict) and "DeclaredWar" in flags
            )
            result.append({
                "civ_a": nation,
                "civ_b": other,
                "status": status,
                "at_war": at_war,
            })
    return result


@router.get(
    "/{game_id}/diplomacy",
    summary="Diplomatic relations between all civilizations",
    description=(
        "Returns all civ-pair diplomatic relations. `at_war=true` when "
        "`diplomaticStatus=War` or `DeclaredWar` flag is active. "
        "Used by diplomacy_war_guard to enforce war-declaration rules."
    ),
)
async def game_diplomacy(
    game_id: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    relations = _extract_diplomacy(save)
    return {"game_id": game_id, "count": len(relations), "relations": relations}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/techs  — researched technologies per civ
# ---------------------------------------------------------------------------

def _extract_techs(save: dict) -> dict:
    """Return researched techs and policy state per civ (excluding Barbarians)."""
    result = {}
    for civ in save.get("civilizations", []):
        nation = civ.get("civName")
        if not nation or nation == "Barbarians":
            continue
        tech = civ.get("tech") or {}
        researched = tech.get("techsResearched") if isinstance(tech, dict) else []
        policies = civ.get("policies") or {}
        adopted = policies.get("adoptedPolicies") if isinstance(policies, dict) else []
        stored_culture = policies.get("storedCulture") if isinstance(policies, dict) else None
        should_open = policies.get("shouldOpenPolicyPicker") if isinstance(policies, dict) else None
        result[nation] = {
            "techs_researched": list(researched) if researched else [],
            "adopted_policies": list(adopted) if adopted else [],
            "stored_culture": int(stored_culture) if stored_culture is not None else 0,
            "should_open_policy_picker": bool(should_open) if should_open is not None else False,
        }
    return result


@router.get(
    "/{game_id}/techs",
    summary="Researched technologies and adopted policies per civilization",
    description=(
        "Returns `techs_researched` and `adopted_policies` for every non-Barbarian civ. "
        "Used by tech checks (institutes, scientists) and voting eligibility logic."
    ),
)
async def game_techs(
    game_id: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"game_id": game_id, "civs": _extract_techs(save)}


# ---------------------------------------------------------------------------
# POST /games/{game_id}/prophet  — patch Great Prophet counter for one nation
# ---------------------------------------------------------------------------

class ProphetPatchRequest(BaseModel):
    nation: str
    value: int
    host: str | None = None


@router.post(
    "/{game_id}/prophet",
    summary="Patch Great Prophet counter for a nation",
    description=(
        "Sets `boughtItemsWithIncreasingPrice['Great Prophet']` in the save file "
        "for the specified nation. Use `value=99` to stop spawning, "
        "or the original value to restore. "
        "Reads, patches, and atomically writes the save — no full save upload needed."
    ),
)
async def patch_game_prophet(game_id: str, body: ProphetPatchRequest):
    _validate_game_id(game_id)
    if not body.host:
        # Local fast path: read-patch-write on disk in one executor call.
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, patch_prophet, game_id, body.nation, body.value)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "game_id": game_id, "nation": body.nation, "value": body.value}
    # External: fetch, patch in memory, PUT back.
    try:
        save = await get_save_dict(game_id, body.host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    patched = False
    for civ in save.get("civilizations", []):
        if civ.get("civName") == body.nation:
            constructions = civ.setdefault("civConstructions", {})
            bought = constructions.setdefault("boughtItemsWithIncreasingPrice", {})
            bought["Great Prophet"] = body.value
            patched = True
            break
    if not patched:
        raise HTTPException(status_code=422, detail=f"Nation not found in save: {body.nation}")
    await store_save(game_id, encode_save(save), host=body.host)
    return {"ok": True, "game_id": game_id, "nation": body.nation, "value": body.value}


# ---------------------------------------------------------------------------
# POST /games/{game_id}/backup  — create a .tar.gz backup of the save
# ---------------------------------------------------------------------------

class BackupRequest(BaseModel):
    subdirectory: str | None = None
    turn: int | None = None
    nation: str | None = None
    max_keep: int = 30
    offsite: bool = False  # дополнительно отправить архив на второй сервер (neth)
    host: str | None = None  # внешний хост: содержимое тянем через API, архив — локально


async def _send_offsite(archive_path: str, game: str | None = None) -> bool:
    """Best-effort POST архива на приёмник (BACKUP_IP): multipart file + form
    password (+ game — приёмник кладёт в backups/<game>/). Ошибка не роняет
    бэкап (второй сервер может быть недоступен)."""
    if not settings.backup_ip:
        return False
    try:
        import httpx
        data = {"password": settings.backup_password}
        if game:
            data["game"] = game
        with open(archive_path, "rb") as f:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(
                    settings.backup_ip,
                    files={"file": (os.path.basename(archive_path), f, "application/gzip")},
                    data=data)
        if resp.status_code != 200:
            logger.warning("offsite backup: приёмник вернул %s", resp.status_code)
            return False
        return True
    except Exception:
        logger.warning("offsite backup: отправка не удалась", exc_info=True)
        return False


@router.post(
    "/{game_id}/backup",
    summary="Create a .tar.gz backup of the game save",
    description=(
        "Tars `MultiplayerFiles/{game_id}` (+ preview) into the backup directory "
        "(optionally a `subdirectory`) as `{turn}_{nation}_{ts}.tar.gz`. "
        "Spectate-compatible: loadable back via `POST /games/{id}/spectate`. "
        "Keeps at most `max_keep` newest archives in the directory."
    ),
)
async def create_game_backup(game_id: str, body: BackupRequest):
    _validate_game_id(game_id)
    backup_dir = settings.get_backup_path()
    if body.subdirectory:
        backup_dir = f"{backup_dir}/{body.subdirectory}"
    loop = asyncio.get_event_loop()
    # Внешний хост: файлы игры не лежат локально — тянем их через API и архивируем
    # уже переданное содержимое (бэкапы всегда храним у себя).
    save_text = preview_text = None
    if body.host:
        try:
            save_text = await get_save_raw(game_id, body.host)
            preview_text = await get_preview_raw(game_id, body.host)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
    try:
        name = await loop.run_in_executor(None, lambda: create_backup(
            game_id, backup_dir=backup_dir, turn=body.turn,
            nation=body.nation, max_keep=body.max_keep,
            save_text=save_text, preview_text=preview_text))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    offsite_ok = False
    if body.offsite:
        # game = имя игры (для permanent subdirectory == session name)
        offsite_ok = await _send_offsite(f"{backup_dir}/{name}", game=body.subdirectory)
    return {"ok": True, "backup_name": name, "subdirectory": body.subdirectory,
            "offsite": offsite_ok}


# ---------------------------------------------------------------------------
# POST /games/{game_id}/save и /preview — залить готовый файл игры/превью
# ---------------------------------------------------------------------------

def _normalize_to_encoded(raw: bytes) -> str:
    """Привести загруженное тело к on-disk формату (gzip+base64 текст).

    Принимаем оба варианта, как исторически слал бот:
    - декодированный JSON сейва → кодируем `encode_save`;
    - уже закодированный сейв (base64+gzip) → пишем как есть, но валидируем через
      `decode_save` (мусор отбиваем 400).
    """
    text = raw.decode("utf-8", errors="strict").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if parsed is not None:
        return encode_save(parsed)
    try:
        decode_save(text)  # валидируем уже-закодированный сейв
    except Exception:
        raise HTTPException(status_code=400,
                            detail="body is neither JSON save nor encoded save")
    return text


async def _safety_backup(game_id: str, *, host: str | None = None) -> None:
    """Best-effort бэкап перед перезаписью (как backup=True в монолите).

    Внешний хост → содержимое тянем через API и архивируем локально (бэкапы всегда
    храним у себя). Локально файла может ещё не быть (первая заливка) — тогда no-op.
    """
    loop = asyncio.get_event_loop()
    try:
        if host:
            save_text = await get_save_raw(game_id, host)
            preview_text = await get_preview_raw(game_id, host)
            await loop.run_in_executor(None, lambda: create_backup(
                game_id, backup_dir=settings.get_backup_path(), max_keep=30,
                save_text=save_text, preview_text=preview_text))
        else:
            await loop.run_in_executor(None, lambda: create_backup(
                game_id, backup_dir=settings.get_backup_path(), max_keep=30))
    except FileNotFoundError:
        pass  # файла ещё нет (первая заливка) — бэкапить нечего
    except Exception:
        logger.exception("safety backup перед заливкой не удался: %s", game_id)


@router.post(
    "/{game_id}/save",
    summary="Upload/replace the full game save file",
    description=(
        "Body — сам сейв: декодированный JSON (сервис закодирует) либо уже "
        "закодированный (base64+gzip) текст. Атомарная запись в "
        "MultiplayerFiles/{game_id}. По умолчанию перед перезаписью снимается "
        "safety-бэкап (?backup=false — отключить)."
    ),
)
async def upload_game_save(game_id: str, request: Request,
                           backup: bool = Query(True),
                           host: str | None = Query(default=None, description=_HOST_DESC),
                           uid: str | None = Query(default=None,
                               description="Basic-auth userId for external PUT (e.g. Spectator id)"),
                           password: str | None = Query(default=None,
                               description="Basic-auth password for external PUT")):
    _validate_game_id(game_id)
    raw = await request.body()
    encoded = _normalize_to_encoded(raw)
    if backup:
        await _safety_backup(game_id, host=host)
    await store_save(game_id, encoded, host=host, uid=uid, password=password)
    return {"ok": True, "game_id": game_id, "bytes": len(encoded)}


@router.post(
    "/{game_id}/preview",
    summary="Upload/replace the game preview file",
    description=(
        "Body — превью-файл: декодированный JSON (сервис закодирует) либо уже "
        "закодированный текст. Атомарная запись в MultiplayerFiles/{game_id}_Preview."
    ),
)
async def upload_game_preview(game_id: str, request: Request,
                              host: str | None = Query(default=None, description=_HOST_DESC),
                              uid: str | None = Query(default=None,
                                  description="Basic-auth userId for external PUT (e.g. Spectator id)"),
                              password: str | None = Query(default=None,
                                  description="Basic-auth password for external PUT")):
    _validate_game_id(game_id)
    raw = await request.body()
    encoded = _normalize_to_encoded(raw)
    await store_preview(game_id, encoded, host=host, uid=uid, password=password)
    return {"ok": True, "game_id": game_id, "bytes": len(encoded)}


# ---------------------------------------------------------------------------
# POST /games/{game_id}/spectate  — load backup as spectate game
# ---------------------------------------------------------------------------

class SpectateRequest(BaseModel):
    backup_name: str
    subdirectory: str | None = None


@router.post(
    "/{game_id}/spectate",
    summary="Load backup as a spectate game",
    description=(
        "Extracts a `.tar.gz` backup from the server's backup directory, "
        "patches it for spectating (`anyoneCanSpectate=True`, `speed=Solo-iron`, "
        "`baseRuleset=RekMOD iron`), zeroes all non-Spectator player IDs, "
        "and writes the result to `MultiplayerFiles/{game_id}`. "
        "`backup_name` is the filename within the backup directory. "
        "Optional `subdirectory` appends a subfolder (e.g. measurement name). "
        "Returns the Spectator player's `spec_id`."
    ),
)
async def load_spectate_game(game_id: str, body: SpectateRequest):
    _validate_game_id(game_id)
    from pathlib import Path as _Path

    backup_dir = settings.get_backup_path()
    if body.subdirectory:
        backup_dir = f"{backup_dir}/{body.subdirectory}"
    backup_file = _Path(backup_dir) / body.backup_name

    if not backup_file.is_file():
        raise HTTPException(status_code=404, detail=f"Backup not found: {backup_file}")

    loop = asyncio.get_event_loop()
    try:
        spec_id = await loop.run_in_executor(None, load_spectate_backup, backup_file, game_id)
    except (ValueError, tarfile.TarError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "game_id": game_id, "spec_id": spec_id}


# ---------------------------------------------------------------------------
# POST /games/{game_id}/restore  — restore save+preview from a backup archive
# ---------------------------------------------------------------------------

class RestoreRequest(BaseModel):
    backup_name: str
    subdirectory: str | None = None
    safety_backup: bool = True
    host: str | None = None       # внешний хост: заливаем PUT'ом, не пишем локально
    uid: str | None = None        # логин для PUT на внешний хост (id спектатора)
    password: str | None = None


@router.post(
    "/{game_id}/restore",
    summary="Restore the live save from a backup archive (destructive)",
    description=(
        "Extracts the save (+preview) from a `.tar.gz` backup and overwrites "
        "`MultiplayerFiles/{game_id}`. Used by core `/rollback` `/reload` "
        "`/loadgame` `/loadrotate`. `backup_name` is the filename inside the "
        "backup dir; optional `subdirectory` (e.g. `rotate/game27` or `game27`). "
        "By default a safety backup of the current live file is taken first."
    ),
)
async def restore_game(game_id: str, body: RestoreRequest):
    _validate_game_id(game_id)
    from pathlib import Path as _Path

    sub = (body.subdirectory or "").strip("/")
    if sub and ".." in sub.split("/"):
        raise HTTPException(status_code=400, detail="bad subdirectory")
    backup_dir = settings.get_backup_path()
    if sub:
        backup_dir = f"{backup_dir}/{sub}"
    backup_file = _Path(backup_dir) / body.backup_name
    if not backup_file.is_file():
        raise HTTPException(status_code=404, detail=f"Backup not found: {backup_file}")

    loop = asyncio.get_event_loop()
    if not body.host:
        try:
            result = await loop.run_in_executor(
                None, lambda: restore_backup(
                    backup_file, game_id, safety_backup=body.safety_backup))
        except (ValueError, tarfile.TarError) as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "game_id": game_id, **result}

    # Внешний хост: содержимое архива заливаем PUT'ом на сервер, локально не пишем
    # (удалять/писать в чужой MultiplayerFiles нельзя). Safety-бэкап текущего
    # состояния снимаем как обычно (тянется через API).
    try:
        contents = await loop.run_in_executor(
            None, lambda: extract_backup_contents(backup_file))
    except (ValueError, tarfile.TarError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    if body.safety_backup:
        await _safety_backup(game_id, host=body.host)
    result = {"save": False, "preview": False}
    await store_save(game_id, contents["save_text"], host=body.host,
                     uid=body.uid, password=body.password)
    result["save"] = True
    # Превью в архиве может отставать от сейва на ход — выравниваем по сейву.
    preview_text = align_preview_to_save(contents["save_text"],
                                         contents.get("preview_text"))
    if preview_text:
        await store_preview(game_id, preview_text, host=body.host,
                            uid=body.uid, password=body.password)
        result["preview"] = True
    return {"ok": True, "game_id": game_id, **result}


# ---------------------------------------------------------------------------
# POST /games/{game_id}/single  — convert save to single-player with manual civs
# ---------------------------------------------------------------------------

class SingleRequest(BaseModel):
    nations: list[str] = []
    host: str | None = None


@router.post(
    "/{game_id}/single",
    summary="Convert a save to single-player with manual nations",
    description=(
        "Returns the decoded save patched for single-player: "
        "`isOnlineMultiplayer=false`, difficulty `King`, the listed nations set "
        "to `playerType=Human` and `playerType` removed from the rest. "
        "Unknown nations (not present in the save) → 422 with the missing list. "
        "Used by core `/getsingle`; the caller ships the JSON back to the admin."
    ),
)
async def game_single(game_id: str, body: SingleRequest):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, body.host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return make_single_player(save, body.nations)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail={"missing_nations": str(e).split(",") if str(e) else []})


# ---------------------------------------------------------------------------
# GET /games/{game_id}/preview  — parsed preview file
# ---------------------------------------------------------------------------

@router.get("/{game_id}/preview", summary="Parsed preview file (lightweight metadata)")
async def game_preview(game_id: str,
                       host: str | None = Query(default=None, description=_HOST_DESC)):
    _validate_game_id(game_id)
    try:
        return await get_preview_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# POST /games/start  — async game creation task
# ---------------------------------------------------------------------------

class StartGameRequest(BaseModel):
    config: dict
    max_attempts: int | None = None
    nochecks: bool = False
    noupdate: bool = False
    jar_url: str | None = None
    mod_git_url: str | None = None
    # Встроенный базовый ruleset (напр. «Civ V - Gods & Kings»), который надо
    # выложить из jar как мод (иначе форк грузит его без speeds → краш).
    builtin_ruleset: str | None = None
    # Пороги проверки карты (None → дефолты game-service settings.expect_*).
    min_distance: int | None = None
    max_distance: int | None = None
    min_luxuries: int | None = None
    min_unique_luxuries: int | None = None
    min_land_per_player: int | None = None
    max_land_per_player: int | None = None


@router.post(
    "/start",
    summary="Start game creation (async)",
    description=(
        "Launches Unciv.jar `--creategame` with the supplied config. "
        "Returns a `task_id` immediately. The service retries until map check passes "
        "or `max_attempts` is exhausted. Poll `GET /tasks/{task_id}` for progress."
    ),
    status_code=202,
)
async def start_game(body: StartGameRequest):
    task = await create_task()
    asyncio.create_task(_run_start_game(task, body))
    return {
        "task_id": task.id,
        "status_url": f"/tasks/{task.id}",
    }


async def _run_start_game(task, body: StartGameRequest) -> None:
    import time as _time
    max_attempts = body.max_attempts or settings.max_start_attempts
    launcher = get_launcher()
    config = dict(body.config)

    # Wait for exclusive slot — task stays pending until no other game is starting
    async with get_start_lock():
        if task.status == TaskStatus.cancelled:
            return

        await update_task(task, status=TaskStatus.running)

        if body.mod_git_url:
            task.add_log("⬇️ Обновление мода...")
            try:
                await launcher.clone_mod(body.mod_git_url)
                task.add_log("✅ Мод обновлён.")
            except Exception as e:
                await update_task(task, status=TaskStatus.failed, error=f"Ошибка обновления мода: {e}")
                return
        elif body.builtin_ruleset:
            task.add_log(f"⬇️ Подготовка рулсета «{body.builtin_ruleset}»...")
            try:
                # Выкладываем jsons/ из jar на диск — console-режим читает базовые
                # рулсеты оттуда. Мод-папка НЕ нужна, config.mods не трогаем.
                await launcher.prepare_builtin_ruleset()
                task.add_log("✅ Рулсет подготовлен.")
            except Exception as e:
                await update_task(task, status=TaskStatus.failed, error=f"Ошибка подготовки рулсета: {e}")
                return

        if not body.noupdate and body.jar_url:
            task.add_log("⬇️ Обновление Unciv.jar...")
            try:
                await launcher.update(body.jar_url)
                task.add_log("✅ Unciv.jar обновлён.")
            except Exception as e:
                await update_task(task, status=TaskStatus.failed, error=f"Ошибка обновления jar: {e}")
                return

        last_issues: list[str] = []

        for attempt in range(1, max_attempts + 1):
            if task.status == TaskStatus.cancelled:
                return
            await update_task(task, attempt=attempt)
            # Fresh seed on every attempt so the map is different each time
            config.setdefault("mapParameters", {})["seed"] = int(_time.time() * 1000)

            try:
                output = await launcher.launch(config)
            except Exception as e:
                is_last = attempt >= max_attempts
                logger.error("Attempt %d: Unciv jar crashed: %s", attempt, e)
                task.add_log(
                    f"⚠️ Попытка #{attempt}: Unciv упал при генерации карты."
                    + ("" if is_last else "\n🔄 Рестарт...")
                )
                if is_last:
                    await update_task(task, status=TaskStatus.failed, error=str(e))
                    return
                continue

            game_id = _extract_game_id(output)
            if not game_id:
                await update_task(task, status=TaskStatus.failed, error="No game_id in jar output")
                return

            try:
                save = await get_save_dict(game_id)
            except Exception as e:
                await update_task(task, status=TaskStatus.failed, error=f"Cannot read save: {e}")
                return

            if body.nochecks:
                await update_task(task, status=TaskStatus.done, result={"game_id": game_id})
                return

            result = check_map(
                save,
                min_distance=body.min_distance,
                max_distance=body.max_distance,
                min_luxuries=body.min_luxuries,
                min_unique_luxuries=body.min_unique_luxuries,
                min_land_per_player=body.min_land_per_player,
                max_land_per_player=body.max_land_per_player,
            )
            if result.ok:
                await update_task(task, status=TaskStatus.done, result={"game_id": game_id})
                return

            last_issues = result.issues
            is_last = attempt >= max_attempts
            task.add_log(
                f"⚠️ Попытка #{attempt} не прошла проверку критериев\n\n"
                f"Несоответствия:\n" + "\n".join(last_issues)
                + ("" if is_last else "\n🔄 Рестарт...")
            )
            if is_last:
                break

        await update_task(
            task,
            status=TaskStatus.failed,
            error=f"❌ Не удалось создать игру за {max_attempts} попыток\n\n"
                  f"Последние несоответствия:\n" + "\n".join(last_issues),
        )


# ---------------------------------------------------------------------------
# GET /games/{game_id}/snapshot  — all tracker data in one save read
# ---------------------------------------------------------------------------

_SKIP_SNAPSHOT_CIVS = frozenset({"Barbarians", "Spectator", "Spectators"})


_STATS_LETTER_RE = re.compile(r"([A-Z])(-?\d+)")


def _parse_stats(stats) -> dict:
    """Parse Unciv statsHistory entry (dict or compact string like 'S123W45A9E918').

    Каждый стат — буква-id RankingType Unciv + целое (может быть отрицательным).
    Буквы: S score, N population, C growth, P production, G gold, T territory,
    F force, H happiness, W technologies, A culture, E tilesExplored. Разбор
    регуляркой, чтобы не терять новые/старые буквы (раньше набор без ``E`` склеивал
    ``A9E918`` в ``A`` → культура всегда 0)."""
    if isinstance(stats, dict):
        return stats
    result: dict[str, int] = {}
    for letter, raw in _STATS_LETTER_RE.findall(str(stats)):
        try:
            result[letter] = int(raw)
        except ValueError:
            continue
    return result


def _extract_snapshot(save: dict) -> dict:
    """Extract all tracker-relevant data in a single save scan."""
    human_nations: list[str] = []
    tech_by_nation: dict[str, list[str]] = {}
    policies_by_nation: dict[str, list[str]] = {}
    nations_status: list[dict] = []
    city_buildings: list[dict] = []
    capitals = _extract_capitals(save)

    for civ in save.get("civilizations", []):
        name = civ.get("civName")
        if not name or name in _SKIP_SNAPSHOT_CIVS:
            continue
        player_type = civ.get("playerType", "AI")
        is_human = player_type == "Human"
        if is_human and name not in CITY_STATES:
            human_nations.append(name)

        cities = civ.get("cities") or []
        city_count = len(cities)
        founded_count = sum(1 for c in cities if c.get("foundingCiv") == name)

        nations_status.append({
            "name": name,
            "player_type": player_type,
            "city_count": city_count,
            "founded_cities": founded_count,
        })

        tech = civ.get("tech") or {}
        researched = tech.get("techsResearched") if isinstance(tech, dict) else []
        tech_by_nation[name] = list(researched) if researched else []

        policies = civ.get("policies") or {}
        adopted = policies.get("adoptedPolicies") if isinstance(policies, dict) else []
        policies_by_nation[name] = list(adopted) if adopted else []

        if is_human:
            for city in cities:
                city_id = city.get("id")
                if city_id is None:
                    continue
                constructions = city.get("cityConstructions") or {}
                built = constructions.get("builtBuildings") if isinstance(constructions, dict) else []
                city_buildings.append({
                    "city_id": str(city_id),
                    "owner": name,
                    "city_name": str(city.get("name") or ""),
                    "buildings": list(built) if built else [],
                    # Поля для трекера запрета сжигания городов (city_burn).
                    # Отдаём прямо здесь, чтобы core не докачивал полный сейв.
                    "founding_civ": str(city.get("foundingCiv") or ""),
                    "is_being_razed": bool(city.get("isBeingRazed")),
                    "is_original_capital": bool(city.get("isOriginalCapital")),
                })

    units = _extract_units(save)
    human_set = set(human_nations)
    human_units = [u for u in units if u.get("owner") in human_set]

    return {
        "turns": int(save.get("turns") or 0),
        "human_nations": human_nations,
        "tech_by_nation": tech_by_nation,
        "policies_by_nation": policies_by_nation,
        "nations_status": nations_status,
        "human_units": human_units,
        "city_buildings": city_buildings,
        "capitals": capitals,
    }


@router.get(
    "/{game_id}/snapshot",
    summary="Full tracker snapshot (one save read)",
    description=(
        "Returns all data needed by bot trackers in a single save parse: "
        "human nations, techs, units, sea trade routes, city buildings, "
        "capitals, and nation status (city count, founding info). "
        "Replaces direct save reads in save_snapshot + capital_tracker."
    ),
)
async def game_snapshot(
    game_id: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    data = _extract_snapshot(save)
    return {"game_id": game_id, **data}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/veto  — veto rights computation
# ---------------------------------------------------------------------------

def _get_veto_from_param(stats_by_nation: dict, param: str, threshold: int) -> list[str]:
    if not stats_by_nation:
        return []
    max_val = max(d.get(param, 0) for d in stats_by_nation.values())
    return [n for n, d in stats_by_nation.items() if max_val - d.get(param, 0) <= threshold]


def _compute_veto(save: dict) -> dict:
    """Compute veto rights identical to bot's get_priority_minus."""
    all_stats: dict[str, dict] = {}

    for civ in save.get("civilizations", []):
        name = civ.get("civName")
        if not name or name in _SKIP_SNAPSHOT_CIVS or name in CITY_STATES:
            continue
        if civ.get("playerType") != "Human":
            continue
        if not civ.get("cities"):
            continue
        history = civ.get("statsHistory")
        if not isinstance(history, dict) or not history:
            continue
        last_entry = history[next(reversed(history))]
        stats = _parse_stats(last_entry)
        all_stats[name] = {
            "score": stats.get("S", 0),
            "tech": stats.get("W", 0),
            "cult": stats.get("A", 0),
            "force": stats.get("F", 0),
        }

    veto_by_type: dict[str, list[str]] = {
        "tech": _get_veto_from_param(all_stats, "tech", 3),
        "cult": _get_veto_from_param(all_stats, "cult", 0),
        "force": _get_veto_from_param(all_stats, "force", 0),
        "score": _get_veto_from_param(all_stats, "score", 0),
        "utopia": [],
        "apollon": [],
        "capitals": [],
    }

    for civ in save.get("civilizations", []):
        name = civ.get("civName")
        if not name or civ.get("playerType") != "Human" or not civ.get("cities"):
            continue
        for city in civ.get("cities") or []:
            constructions = city.get("cityConstructions") or {}
            if isinstance(constructions, dict):
                if "Utopia Project" in (constructions.get("inProgressConstructions") or []):
                    if name not in veto_by_type["utopia"]:
                        veto_by_type["utopia"].append(name)
                if "Apollo Program" in (constructions.get("builtBuildings") or []):
                    if name not in veto_by_type["apollon"]:
                        veto_by_type["apollon"].append(name)

    for civ in save.get("civilizations", []):
        name = civ.get("civName")
        if not name or civ.get("playerType") != "Human" or not civ.get("cities"):
            continue
        cap_count = sum(
            1 for city in civ.get("cities") or []
            if city.get("isOriginalCapital")
            and city.get("foundingCiv") not in (CITY_STATES | {None, ""})
        )
        if cap_count >= 3:
            veto_by_type["capitals"].append(name)

    veto_right_dict: dict[str, list[str]] = {}
    for veto_type, nations in veto_by_type.items():
        for nation in nations:
            veto_right_dict.setdefault(nation, []).append(veto_type)

    return {"veto": veto_right_dict, "stats": all_stats}


@router.get(
    "/{game_id}/veto",
    summary="Compute veto rights for all human civs",
    description=(
        "Replicates the bot's get_priority_minus logic server-side. "
        "Returns `veto` (nation → list of veto types) and `stats` "
        "(score/tech/cult/force per nation from statsHistory). "
        "Used by voting.py and capital_tracker."
    ),
)
async def game_veto(
    game_id: str,
    host: str | None = Query(default=None, description=_HOST_DESC),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    result = _compute_veto(save)
    return {"game_id": game_id, **result}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/prophet  — prophet purchase counts per nation
# ---------------------------------------------------------------------------

@router.get("/{game_id}/save-raw", summary="Decoded main save file (for admin download)")
async def game_save_raw(game_id: str,
                        host: str | None = Query(default=None, description=_HOST_DESC)):
    _validate_game_id(game_id)
    try:
        return await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


class PatchSpeedRequest(BaseModel):
    speed: str
    host: str | None = None


@router.post("/{game_id}/patch-speed", summary="Patch gameParameters.speed in save and preview")
async def patch_speed(game_id: str, body: PatchSpeedRequest):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, body.host)
        save.setdefault("gameParameters", {})["speed"] = body.speed
        await store_save(game_id, encode_save(save), host=body.host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        preview = await get_preview_dict(game_id, body.host)
        preview.setdefault("gameParameters", {})["speed"] = body.speed
        await store_preview(game_id, encode_save(preview), host=body.host)
    except Exception:
        pass
    return {"ok": True, "speed": body.speed}


@router.get("/{game_id}/prophet", summary="Great Prophet purchase counts per nation")
async def get_prophet(game_id: str,
                      host: str | None = Query(default=None, description=_HOST_DESC)):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, host)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    counts = {}
    for civ in save.get("civilizations", []):
        civ_name = civ.get("civName")
        if not civ_name:
            continue
        constructions = civ.get("civConstructions") or {}
        bought = constructions.get("boughtItemsWithIncreasingPrice") or {}
        if "Great Prophet" in bought:
            counts[civ_name] = bought["Great Prophet"]
    return {"prophet_counts": counts}


def _extract_game_id(output: str) -> str | None:
    """Extract UUID from Unciv.jar output.

    Unciv prints: 'Game started successfully with game id: <uuid>'
    """
    m = re.search(
        r"Game started successfully with game id:\s*([a-f0-9-]+)",
        output, re.IGNORECASE,
    )
    return m.group(1) if m else None
