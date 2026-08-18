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


def _extract_units(save: dict) -> list[dict]:
    """Units on the map, from each tile's civilian/military unit slots."""
    units: list[dict] = []
    for tile in (save.get("tileMap") or {}).get("tileList") or []:
        if not isinstance(tile, dict):
            continue
        pos = tile.get("position") or {}
        x, y = pos.get("x", 0), pos.get("y", 0)
        for key in ("civilianUnit", "militaryUnit"):
            unit = tile.get(key)
            if not isinstance(unit, dict):
                continue
            health = unit.get("health")
            units.append({
                "x": x,
                "y": y,
                "name": unit.get("name") or "",
                "owner": unit.get("owner") or unit.get("originalOwner"),
                "military": key == "militaryUnit",
                "health": int(health) if health is not None else 100,
            })
    return units


def _extract_cities(save: dict) -> list[dict]:
    """Cities with current owner and capital flag (Palace = current capital)."""
    cities: list[dict] = []
    for civ in save.get("civilizations") or []:
        owner = civ.get("civName")
        for city in civ.get("cities") or []:
            loc = city.get("location") or {}
            constructions = city.get("cityConstructions") or {}
            built = constructions.get("builtBuildings") or []
            is_capital = "Palace" in built or bool(city.get("isOriginalCapital"))
            cities.append({
                "x": loc.get("x", 0),
                "y": loc.get("y", 0),
                "name": city.get("name") or "",
                "owner": owner,
                "isCapital": is_capital,
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
            "improvement": tile.get("improvement"),
            "roadStatus": tile.get("roadStatus"),
            "owningCiv": owners.get((x, y)),
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
    }
