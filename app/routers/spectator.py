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
                    owners[(pos.get("x"), pos.get("y"))] = civ_name
    return owners


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
    }
