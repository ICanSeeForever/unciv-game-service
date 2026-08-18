"""Public (unauthenticated) read-only state for the unciv-web spectator viewer.

This router is intentionally mounted WITHOUT the API-key dependency: the web
frontend is public and renders exactly this data, so we expose a denormalized,
read-only projection of the save instead of shipping the game-service API key
to the web tier. Games are addressed by unguessable UUIDs.
"""
import re

from fastapi import APIRouter, HTTPException

from app.game.fetcher import get_save_dict

router = APIRouter(prefix="/games", tags=["spectator"])

_GAME_ID_RE = re.compile(r"^[0-9a-f-]{32,}$", re.IGNORECASE)


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
            })
    return cities


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
            "hasBottomRiver": bool(tile.get("hasBottomRiver")),
            "hasBottomLeftRiver": bool(tile.get("hasBottomLeftRiver")),
            "hasBottomRightRiver": bool(tile.get("hasBottomRightRiver")),
        })

    game_params = save.get("gameParameters") or {}
    mods = [m for m in (game_params.get("mods") or []) if m]

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
        "cities": _extract_cities(save),
        "attacks": _civ_attacks(save),
    }
