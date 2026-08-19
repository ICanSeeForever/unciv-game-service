"""Public (unauthenticated) read-only state for the unciv-web spectator viewer.

This router is intentionally mounted WITHOUT the API-key dependency: the web
frontend is public and renders exactly this data, so we expose a denormalized,
read-only projection of the save instead of shipping the game-service API key
to the web tier. Games are addressed by unguessable UUIDs.
"""
import os
import re
import tarfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.game.fetcher import get_save_dict
from app.game.parser import decode_save
from app.game.stats import compute_income

router = APIRouter(prefix="/games", tags=["spectator"])

# Second router (different prefix) for the game/turn browser the web viewer uses.
browser = APIRouter(prefix="/spectator", tags=["spectator"])

_GAME_ID_RE = re.compile(r"^[0-9a-f-]{32,}$", re.IGNORECASE)
_RESERVED_FOLDERS = {"trash", "rotate"}


def _backup_folder(name: str) -> Path:
    """Validated path to a per-game backup folder (guards against traversal)."""
    clean = name.strip("/")
    if not clean or ".." in clean.split("/") or clean in _RESERVED_FOLDERS:
        raise HTTPException(status_code=400, detail="bad game name")
    path = Path(settings.get_backup_path()) / clean
    if not path.is_dir():
        raise HTTPException(status_code=404, detail=f"No backup folder: {name}")
    return path


def _archives(folder: Path) -> list[Path]:
    """The folder's .tar.gz backups, oldest → newest (matches /spectate ordering)."""
    files = [p for p in folder.iterdir() if p.is_file() and p.name.endswith(".tar.gz")]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files


def _turns_of(folder: Path) -> list[int]:
    """Sorted unique turn numbers, parsed from the ``{turn}_...`` filename prefix."""
    turns: set[int] = set()
    for p in _archives(folder):
        head = p.name.split("_", 1)[0]
        if head.isdigit():
            turns.add(int(head))
    return sorted(turns)


def _save_member(tar: tarfile.TarFile):
    """The main save entry inside a backup archive (UUID-named, not the preview)."""
    for member in tar.getmembers():
        base = os.path.basename(member.name)
        if "-" in base and "Preview" not in base:
            return member
    return None


def _resolve_uuid(folder: Path) -> str | None:
    """The game's live UUID, read from the save entry name inside its first backup."""
    for archive in _archives(folder):
        try:
            with tarfile.open(archive, "r:gz") as tar:
                member = _save_member(tar)
                if member:
                    return os.path.basename(member.name)
        except (tarfile.TarError, OSError):
            continue
    return None


def _extract_backup_save(folder: Path, turn: int) -> dict:
    """Decode the save for a given turn straight from its backup archive (no disk write)."""
    archive = next((p for p in _archives(folder) if p.name.split("_", 1)[0] == str(turn)), None)
    if archive is None:
        raise HTTPException(status_code=404, detail=f"No backup for turn {turn}")
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = _save_member(tar)
            f = tar.extractfile(member) if member else None
            if f is None:
                raise HTTPException(status_code=500, detail="No save inside backup archive")
            return decode_save(f.read().decode("utf-8").strip())
    except (tarfile.TarError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"Bad backup archive: {e}")


def _has_live_save(uuid: str | None) -> bool:
    """Whether a live (in-data) save exists in MultiplayerFiles for this UUID."""
    if not uuid:
        return False
    return (Path(settings.civ_path) / "MultiplayerFiles" / uuid).is_file()


def _tile_owner_by_position(save: dict) -> dict[tuple, str]:
    """Map (x, y) -> owning civ name, derived from each city's owned tiles."""
    owners: dict[tuple, str] = {}
    for civ in save.get("civilizations") or []:
        civ_name = civ.get("civName")
        for city in civ.get("cities") or []:
            for pos in city.get("tiles") or []:
                if isinstance(pos, dict):
                    # libGDX Json omits a Vector2 component when it's 0, so a
                    # missing x/y means 0 — must match the tileList default below,
                    # otherwise every owned tile on the x=0 / y=0 axes is lost.
                    owners[(pos.get("x", 0), pos.get("y", 0))] = civ_name
    return owners


def _worked_positions(save: dict) -> set[tuple]:
    """Set of (x, y) tiles currently worked by a citizen of some city."""
    worked: set[tuple] = set()
    for civ in save.get("civilizations") or []:
        for city in civ.get("cities") or []:
            for pos in city.get("workedTiles") or []:
                if isinstance(pos, dict):
                    worked.add((pos.get("x", 0), pos.get("y", 0)))
    return worked


def _unit_trail(unit: dict, cur_x: int, cur_y: int) -> list[dict]:
    """Unit's recorded movement (Unciv movementMemories) as {x, y, type} points.

    Mirrors Unciv updateMovementOverlay: an arrow is drawn to each point using
    THAT point's move type (libGDX omits the default type "UnitMoved"), between
    consecutive memories and finally from the last memory to the unit's current
    tile using mostRecentMoveType. The arrow art encodes the type/colour
    (UnitMoved=blue, UnitAttacked=red, UnitTeleported/UnitWithdrew=white).
    Consecutive duplicate positions collapse so idle units draw nothing.
    """
    points: list[dict] = []

    def push(x, y, mtype):
        pt = {"x": x, "y": y, "type": mtype or "UnitMoved"}
        if not points or (points[-1]["x"], points[-1]["y"]) != (x, y):
            points.append(pt)
        else:
            points[-1]["type"] = pt["type"]  # keep the later type on a repeat

    for mem in unit.get("movementMemories") or []:
        pos = (mem or {}).get("position") or {}
        push(pos.get("x", 0), pos.get("y", 0), (mem or {}).get("type"))
    push(cur_x, cur_y, unit.get("mostRecentMoveType"))
    return points


def _unit_record(unit: dict, x: int, y: int, *, military: bool, air: bool) -> dict:
    """Normalize one unit for the viewer (position, move arrow, attack targets)."""
    health = unit.get("health")
    # A pending multi-turn move is stored as action "moveTo <x>,<y>" — Unciv draws
    # a white "UnitMoving" arrow to that destination (isMoving()).
    move_to = None
    action = str(unit.get("action") or "")
    if action.startswith("moveTo "):
        try:
            mx, my = action.split(" ", 1)[1].split(",")
            move_to = {"x": int(mx), "y": int(my)}
        except (ValueError, IndexError):
            move_to = None
    # Tiles this unit attacked this turn (attacksSinceTurnStart) → red attack arrows.
    attacks = [
        {"x": a.get("x", 0), "y": a.get("y", 0)}
        for a in (unit.get("attacksSinceTurnStart") or [])
        if isinstance(a, dict)
    ]
    # Promotions drive the unit's exact sight range in vision mode (Scouting,
    # Sentry, Extra Sight, …) on top of the base unit's own Sight uniques.
    promotions = list(((unit.get("promotions") or {}).get("promotions")) or [])
    return {
        "x": x,
        "y": y,
        "name": unit.get("name") or "",
        "owner": unit.get("owner") or unit.get("originalOwner"),
        "military": military,
        "air": air,
        "health": int(health) if health is not None else 100,
        "trail": _unit_trail(unit, x, y),
        "moveTo": move_to,
        "attacks": attacks,
        "promotions": promotions,
    }


def _extract_units(save: dict) -> list[dict]:
    """Units on the map, from each tile's civilian/military/air unit slots."""
    units: list[dict] = []
    for tile in (save.get("tileMap") or {}).get("tileList") or []:
        if not isinstance(tile, dict):
            continue
        pos = tile.get("position") or {}
        x, y = pos.get("x", 0), pos.get("y", 0)
        for key in ("civilianUnit", "militaryUnit"):
            unit = tile.get(key)
            if isinstance(unit, dict):
                units.append(_unit_record(unit, x, y, military=key == "militaryUnit", air=False))
        # Air units are a LIST on the tile (a city can hold several).
        for unit in tile.get("airUnits") or []:
            if isinstance(unit, dict):
                units.append(_unit_record(unit, x, y, military=True, air=True))
    return units


def _civ_attacks(save: dict) -> list[dict]:
    """Civ-level attack memories (source -> target) for red attack arrows."""
    out: list[dict] = []
    for civ in save.get("civilizations") or []:
        for a in civ.get("attacksSinceTurnStart") or []:
            src = (a or {}).get("source") or {}
            tgt = (a or {}).get("target") or {}
            out.append({
                "fromX": src.get("x", 0), "fromY": src.get("y", 0),
                "toX": tgt.get("x", 0), "toY": tgt.get("y", 0),
            })
    return out


def _player_civs(save: dict) -> list[str]:
    """Civs present on the map (own a city or a unit), for the vision-mode picker.

    Excludes the Spectator pseudo-civ and Barbarians. City-states are kept here
    (the frontend filters them out via its bundled nation metadata), so this stays
    ruleset-agnostic on the backend.
    """
    unit_owners: set[str] = set()
    for tile in (save.get("tileMap") or {}).get("tileList") or []:
        if not isinstance(tile, dict):
            continue
        for key in ("civilianUnit", "militaryUnit"):
            u = tile.get(key)
            if isinstance(u, dict) and u.get("owner"):
                unit_owners.add(u["owner"])
        for u in tile.get("airUnits") or []:
            if isinstance(u, dict) and u.get("owner"):
                unit_owners.add(u["owner"])
    out: list[str] = []
    for civ in save.get("civilizations") or []:
        name = civ.get("civName")
        if not name or name in ("Spectator", "Barbarians"):
            continue
        if (civ.get("cities") or []) or name in unit_owners:
            out.append(name)
    return out


def _majority_religion(city: dict) -> str | None:
    """City's majority religion = highest religious pressure (Unciv getMajorityReligion)."""
    pressures = ((city.get("religion") or {}).get("pressures")) or {}
    if not pressures:
        return None
    name, value = max(pressures.items(), key=lambda kv: kv[1])
    return name if value > 0 else None


def _extract_cities(save: dict) -> list[dict]:
    """Cities with the data the web viewer's city button plate needs (Unciv CityButton)."""
    cities: list[dict] = []
    for civ in save.get("civilizations") or []:
        owner = civ.get("civName")
        owner_techs = len(((civ.get("tech") or {}).get("techsResearched")) or [])
        for city in civ.get("cities") or []:
            loc = city.get("location") or {}
            cc = city.get("cityConstructions") or {}
            built = cc.get("builtBuildings") or []
            queue = cc.get("constructionQueue") or []
            current = cc.get("currentConstructionFromQueue") or (queue[0] if queue else None)
            in_progress = cc.get("inProgressConstructions") or {}
            pop = city.get("population") or {}
            # Capital = holds a building that "Indicates the capital city"
            # (Palace, or Hungary's Orszaggyules) — NOT isOriginalCapital, which
            # stays true for former capitals that were moved/conquered.
            is_capital = "Palace" in built or "Orszaggyules" in built
            cities.append({
                "x": loc.get("x", 0),
                "y": loc.get("y", 0),
                "name": city.get("name") or "",
                "owner": owner,
                "isCapital": is_capital,
                # libGDX omits population when it's the class default (1), so a
                # missing value means a size-1 city, not size 0.
                "population": pop.get("population", 1),
                "foodStored": pop.get("foodStored", 0),
                "construction": ({"name": current, "workDone": in_progress.get(current, 0)}
                                 if current else None),
                "religion": _majority_religion(city),
                "health": city.get("health", 0),
                "buildings": list(built),
                "ownerTechs": owner_techs,
                # For the per-turn city stats (production/food -> construction &
                # growth turns, starvation): tiles this city works + its specialists.
                "workedTiles": [
                    {"x": p.get("x", 0), "y": p.get("y", 0)}
                    for p in (city.get("workedTiles") or [])
                    if isinstance(p, dict)
                ],
                "specialists": {k: int(v) for k, v in (pop.get("specialistAllocations") or {}).items()},
                # Puppeted cities don't benefit from social policies (stats engine).
                "isPuppet": bool(city.get("isPuppet")),
            })
    return cities


def _religions(save: dict) -> list[dict]:
    """Founded religions + pantheons for the viewer's religion overview (Unciv
    ReligionOverviewTab). The frontend filters founded ones and computes follower
    counts from cities; belief effects come from the mod's Beliefs.json."""
    holy: dict[str, str] = {}
    for civ in save.get("civilizations") or []:
        for city in civ.get("cities") or []:
            rc = city.get("religion") or {}
            name = rc.get("religionThisIsTheHolyCityOf")
            if name:
                holy[name] = city.get("name")
    out: list[dict] = []
    for key, r in (save.get("religions") or {}).items():
        name = r.get("name") or key
        out.append({
            "name": name,
            "displayName": r.get("displayName") or name,
            "founder": r.get("foundingCivName"),
            "holyCity": holy.get(name),
            "founderBeliefs": list(r.get("founderBeliefs") or []),
            "followerBeliefs": list(r.get("followerBeliefs") or []),
        })
    return out


def _civ_economy(save: dict) -> dict:
    """Per-civ economy shown in the viewer's top resource bar (Unciv EmpireOverview
    / the world-screen top stats). Only the save-backed scalars are extracted here;
    per-turn income (gold/science/culture/faith, net happiness) is computed on the
    frontend stats engine, and the tech/policy/religion lists drive the click-through
    windows. Keyed by civName; the frontend shows the entry for the selected nation
    (and all-zero for the spectator, i.e. no nation selected)."""
    out: dict[str, dict] = {}
    for civ in save.get("civilizations") or []:
        name = civ.get("civName")
        if not name or name in ("Spectator", "Barbarians"):
            continue
        tech = civ.get("tech") or {}
        policies = civ.get("policies") or {}
        religion = civ.get("religionManager") or {}
        golden = civ.get("goldenAges") or {}
        out[name] = {
            # Exact, straight from the save.
            "gold": int(civ.get("gold") or 0),
            "storedCulture": int(policies.get("storedCulture") or 0),
            "storedFaith": int(religion.get("storedFaith") or 0),
            "storedHappiness": int(golden.get("storedHappiness") or 0),
            "numberOfGoldenAges": int(golden.get("numberOfGoldenAges") or 0),
            "techsResearched": list(tech.get("techsResearched") or []),
            # {techName: accumulatedScience} for the tech currently being researched.
            "techsInProgress": dict(tech.get("techsInProgress") or {}),
            "currentTech": (tech.get("techsToResearch") or [None])[0],
            # Recent science per turn (Unciv keeps the last 8) — a good proxy for
            # the "+science" figure until the full stats engine lands.
            "scienceOfLast8Turns": list(tech.get("scienceOfLast8Turns") or []),
            "adoptedPolicies": list(policies.get("adoptedPolicies") or []),
            "numberOfAdoptedPolicies": int(policies.get("numberOfAdoptedPolicies") or 0),
            "religionState": religion.get("religionState"),
        }
    return out


@router.get(
    "/{game_id}/spectator-state",
    summary="Normalized spectator state for the web viewer (tiles + hex coords + mods)",
    description=(
        "Read-only, denormalized view of the save tailored for unciv-web rendering: "
        "every tile with its Unciv hex coordinates and render-relevant fields, plus "
        "the base ruleset and mod list so the frontend can load the matching assets."
    ),
)
async def spectator_state(game_id: str):
    if not _GAME_ID_RE.match(game_id):
        raise HTTPException(status_code=400, detail="Invalid game_id format")
    try:
        save = await get_save_dict(game_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _build_state(save, game_id)


def _build_state(save: dict, game_id: str) -> dict:
    """Denormalize a decoded save into the viewer's spectator-state shape."""
    tile_map = save.get("tileMap") or {}
    map_params = tile_map.get("mapParameters") or {}
    map_size = map_params.get("mapSize") or {}
    owners = _tile_owner_by_position(save)
    worked = _worked_positions(save)

    tiles = []
    for tile in tile_map.get("tileList") or []:
        if not isinstance(tile, dict):
            continue
        # libGDX Json omits `position` when it equals the default Vector2(0,0),
        # so a missing position means the origin tile (0, 0).
        pos = tile.get("position") or {}
        x, y = pos.get("x", 0), pos.get("y", 0)
        tiles.append({
            "x": x,
            "y": y,
            "baseTerrain": tile.get("baseTerrain") or "",
            "terrainFeatures": list(tile.get("terrainFeatures") or []),
            "resource": tile.get("resource"),
            "resourceAmount": tile.get("resourceAmount") or 0,
            "improvement": tile.get("improvement"),
            "roadStatus": tile.get("roadStatus"),
            "owningCiv": owners.get((x, y)),
            "worked": (x, y) in worked,
            # Civs that have ever revealed this tile (Unciv Tile.exploredBy) — drives
            # per-nation fog of war in the viewer's vision mode.
            "exploredBy": list(tile.get("exploredBy") or []),
            "hasBottomRiver": bool(tile.get("hasBottomRiver")),
            "hasBottomLeftRiver": bool(tile.get("hasBottomLeftRiver")),
            "hasBottomRightRiver": bool(tile.get("hasBottomRightRiver")),
        })

    game_params = save.get("gameParameters") or {}
    mods = [m for m in (game_params.get("mods") or []) if m]

    cities = _extract_cities(save)
    civ_stats = _civ_economy(save)
    religions = _religions(save)
    # Per-turn income / net happiness (Unciv top-bar figures) computed on the
    # backend from the full save; merged into each civStats entry as `income`.
    try:
        income = compute_income(cities, tiles, civ_stats, religions)
        for name, inc in income.items():
            if name in civ_stats:
                civ_stats[name]["income"] = inc
    except Exception:  # never let the stats engine break the state response
        pass

    return {
        "gameId": game_id,
        "turn": int(save.get("turns") or 0),
        "mapWidth": map_size.get("width") or 0,
        "mapHeight": map_size.get("height") or 0,
        "worldWrap": bool(map_params.get("worldWrap", False)),
        "baseRuleset": game_params.get("baseRuleset"),
        "mods": mods,
        "tiles": tiles,
        "units": _extract_units(save),
        "cities": cities,
        "attacks": _civ_attacks(save),
        "civs": _player_civs(save),
        "civStats": civ_stats,
        "religions": religions,
        "currentPlayer": save.get("currentPlayer"),
    }


# ---------------------------------------------------------------------------
# Game/turn browser for the web viewer's dropdown + turn switcher.
#
# A "game" here is a per-game backup folder (the same set /spectate offers). Each
# archive is named ``{turn}_{nation}_{ts}.tar.gz`` and carries the game's save
# (UUID-named) inside. If that UUID still has a live save in MultiplayerFiles the
# game is "current" (in data) and the viewer can load the live state directly;
# otherwise only past turns from backups are available.
# ---------------------------------------------------------------------------


@browser.get("/games", summary="Games available to spectate (backup folders)")
async def list_games():
    base = Path(settings.get_backup_path())
    if not base.is_dir():
        return {"games": []}
    names = sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and d.name not in _RESERVED_FOLDERS
    )
    return {"games": names}


@browser.get("/games/{name}", summary="Turn list + live-save availability for a game")
async def game_detail(name: str):
    folder = _backup_folder(name)
    uuid = _resolve_uuid(folder)
    return {
        "name": name,
        "turns": _turns_of(folder),
        "currentGameId": uuid,
        "hasCurrent": _has_live_save(uuid),
    }


@browser.get("/games/{name}/state", summary="Spectator state for a game at a turn (or live)")
async def game_state(
    name: str,
    turn: str = Query(..., description='Turn number, or "current" for the live save'),
):
    folder = _backup_folder(name)
    if turn == "current":
        uuid = _resolve_uuid(folder)
        if not _has_live_save(uuid):
            raise HTTPException(status_code=404, detail="No live save for this game")
        try:
            save = await get_save_dict(uuid)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return _build_state(save, uuid)
    if not turn.isdigit():
        raise HTTPException(status_code=400, detail="turn must be a number or 'current'")
    save = _extract_backup_save(folder, int(turn))
    return _build_state(save, f"{name}@{turn}")
