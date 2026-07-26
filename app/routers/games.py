"""Game endpoints: info, map-check, start."""
import asyncio
import re
import tarfile

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.game.fetcher import (
    get_save_dict, get_preview_dict, list_all_games, get_file_created_at,
    delete_game, patch_prophet, load_spectate_backup,
)
from app.game.static_data import CITY_STATES
from app.launchers import get_launcher
from app.services.map_checker import check_map
from app.services.task_manager import (
    TaskStatus,
    create_task,
    update_task,
)

router = APIRouter(prefix="/games", tags=["games"], responses={404: {"description": "Game not found"}})

_GAME_ID_RE = re.compile(r"^[0-9a-f-]{32,}$", re.IGNORECASE)


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
    summary="Game info: current player, turns, and full player list",
    description=(
        "Returns current player, turn number, and the list of all civilizations "
        "with their type (`is_human=true` for Human, `false` for AI/city-states). "
        "Barbarians are excluded."
    ),
)
async def game_info(
    game_id: str,
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    players = [
        {
            "civ_name": c["civName"],
            "is_human": c.get("playerType") == "Human",
        }
        for c in save.get("civilizations", [])
        if c.get("civName") and c.get("civName") != "Barbarians"
    ]

    created_at = get_file_created_at(game_id)

    return {
        "game_id": game_id,
        "current_player": save.get("currentPlayer"),
        "turns": save.get("turns") or 0,
        "created_at": created_at,
        "players": players,
    }


# ---------------------------------------------------------------------------
# GET /games/{game_id}/map-check  — full map quality check
# ---------------------------------------------------------------------------

@router.get("/{game_id}/map-check", summary="Check map quality (distances, luxuries, marine civs, land ratio)")
async def map_check(
    game_id: str,
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
    min_distance: int | None = Query(default=None, description="Override minimum distance between start positions"),
    max_distance: int | None = Query(default=None, description="Override maximum distance between start positions"),
    min_luxuries: int | None = Query(default=None, description="Override minimum total luxuries in radius"),
    min_unique_luxuries: int | None = Query(default=None, description="Override minimum unique luxury types"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    result = check_map(
        save,
        min_distance=min_distance,
        max_distance=max_distance,
        min_luxuries=min_luxuries,
        min_unique_luxuries=min_unique_luxuries,
    )
    return {"ok": result.ok, "issues": result.issues, "details": result.details}


# ---------------------------------------------------------------------------
# DELETE /games/{game_id}  — delete game file and preview
# ---------------------------------------------------------------------------

@router.delete(
    "/{game_id}",
    summary="Delete game files",
    description="Deletes the main save file and preview file for the given game_id.",
)
async def delete_game_files(game_id: str):
    _validate_game_id(game_id)
    result = delete_game(game_id)
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
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
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
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
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
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
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
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    relations = _extract_diplomacy(save)
    return {"game_id": game_id, "count": len(relations), "relations": relations}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/techs  — researched technologies per civ
# ---------------------------------------------------------------------------

def _extract_techs(save: dict) -> dict:
    """Return researched techs per civ (excluding Barbarians)."""
    result = {}
    for civ in save.get("civilizations", []):
        nation = civ.get("civName")
        if not nation or nation == "Barbarians":
            continue
        tech = civ.get("tech") or {}
        researched = tech.get("techsResearched") if isinstance(tech, dict) else []
        policies = civ.get("policies") or {}
        adopted = policies.get("adoptedPolicies") if isinstance(policies, dict) else []
        result[nation] = {
            "techs_researched": list(researched) if researched else [],
            "adopted_policies": list(adopted) if adopted else [],
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
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"game_id": game_id, "civs": _extract_techs(save)}


# ---------------------------------------------------------------------------
# POST /games/{game_id}/prophet  — patch Great Prophet counter for one nation
# ---------------------------------------------------------------------------

class ProphetPatchRequest(BaseModel):
    nation: str
    value: int


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
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, patch_prophet, game_id, body.nation, body.value)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "game_id": game_id, "nation": body.nation, "value": body.value}


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
# GET /games/{game_id}/preview  — parsed preview file
# ---------------------------------------------------------------------------

@router.get("/{game_id}/preview", summary="Parsed preview file (lightweight metadata)")
async def game_preview(game_id: str):
    _validate_game_id(game_id)
    try:
        return await get_preview_dict(game_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# POST /games/start  — async game creation task
# ---------------------------------------------------------------------------

class StartGameRequest(BaseModel):
    config: dict
    mp_server_url: str | None = None
    max_attempts: int | None = None


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
    max_attempts = body.max_attempts or settings.max_start_attempts
    launcher = get_launcher()
    await update_task(task, status=TaskStatus.running)

    for attempt in range(1, max_attempts + 1):
        await update_task(task, attempt=attempt)
        task.add_log(f"Попытка {attempt}/{max_attempts}: запуск игры...")

        try:
            output = await launcher.launch(body.config)
            task.add_log(f"Jar вывод: {output[:500]}")
        except Exception as e:
            task.add_log(f"Ошибка запуска: {e}")
            await update_task(task, status=TaskStatus.failed, error=str(e))
            return

        # Extract game_id from jar output — Unciv prints the UUID to stdout
        game_id = _extract_game_id(output)
        if not game_id:
            task.add_log("Не удалось получить game_id из вывода jar")
            await update_task(task, status=TaskStatus.failed, error="No game_id in output")
            return

        task.add_log(f"Создана игра {game_id}, проверяем карту...")

        try:
            save = await get_save_dict(game_id, body.mp_server_url)
        except Exception as e:
            task.add_log(f"Не удалось прочитать сейв: {e}")
            await update_task(task, status=TaskStatus.failed, error=str(e))
            return

        result = check_map(save)
        if result.ok:
            task.add_log("✅ Карта прошла проверку")
            await update_task(task, status=TaskStatus.done, result={"game_id": game_id})
            return

        task.add_log("❌ Карта не подходит:\n" + "\n".join(result.issues))
        if attempt >= max_attempts:
            break

    await update_task(
        task,
        status=TaskStatus.failed,
        error=f"Карта не прошла проверку за {max_attempts} попыток",
    )


def _extract_game_id(output: str) -> str | None:
    """Extract UUID from Unciv.jar output.

    Unciv prints: 'Game started successfully with game id: <uuid>'
    """
    m = re.search(
        r"Game started successfully with game id:\s*([a-f0-9-]+)",
        output, re.IGNORECASE,
    )
    return m.group(1) if m else None
