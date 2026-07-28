"""Map quality check — ported from civ_bot /tiles command."""
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from app.config import settings
from app.game.static_data import get_city_states, get_luxuries, get_marine_civs


@dataclass
class MapCheckResult:
    ok: bool
    issues: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def _hex_distance(pos1: dict, pos2: dict, map_width: int = 0) -> int:
    dx = pos2["x"] - pos1["x"]
    dy = pos2["y"] - pos1["y"]
    d = max(abs(dx), abs(dy), abs(dx - dy))
    if map_width > 0:
        for wdx, wdy in [(dx + map_width, dy - map_width), (dx - map_width, dy + map_width)]:
            d = min(d, max(abs(wdx), abs(wdy), abs(wdx - wdy)))
    return d


def _neighbors(x: int, y: int) -> list[tuple[int, int]]:
    return [
        (x + 1, y), (x - 1, y),
        (x, y + 1), (x, y - 1),
        (x + 1, y + 1), (x - 1, y - 1),
    ]


def _tile_at(nx: int, ny: int, tiles_dict: dict, map_width: int) -> dict | None:
    """Look up tile at (nx, ny), trying wrap-adjusted coordinates if needed."""
    tile = tiles_dict.get((nx, ny))
    if tile is None and map_width > 0:
        tile = tiles_dict.get((nx + map_width, ny - map_width))
        if tile is None:
            tile = tiles_dict.get((nx - map_width, ny + map_width))
    return tile


def _neighbor_exists(nx: int, ny: int, tiles_dict: dict, map_width: int) -> bool:
    if (nx, ny) in tiles_dict:
        return True
    if map_width > 0:
        if (nx + map_width, ny - map_width) in tiles_dict:
            return True
        if (nx - map_width, ny + map_width) in tiles_dict:
            return True
    return False


def _find_boundary_tiles(tiles_dict: dict, map_width: int) -> set[tuple[int, int]]:
    """Return tiles that border a real (non-wrapping) map edge."""
    boundary: set[tuple[int, int]] = set()
    for (x, y) in tiles_dict:
        for nx, ny in _neighbors(x, y):
            if not _neighbor_exists(nx, ny, tiles_dict, map_width):
                boundary.add((x, y))
                break
    return boundary


def check_map(
    file_dict: dict,
    *,
    min_distance: int | None = None,
    max_distance: int | None = None,
    min_luxuries: int | None = None,
    min_unique_luxuries: int | None = None,
    min_land_per_player: int | None = None,
    max_land_per_player: int | None = None,
) -> MapCheckResult:
    """Check map quality; returns MapCheckResult with issues list."""
    min_distance = min_distance if min_distance is not None else settings.expect_min_distance
    max_distance = max_distance if max_distance is not None else settings.expect_max_distance
    min_lux = min_luxuries if min_luxuries is not None else settings.expect_min_luxuries
    min_unique_lux = min_unique_luxuries if min_unique_luxuries is not None else settings.expect_min_unique_luxuries
    min_land_pp = min_land_per_player if min_land_per_player is not None else settings.expect_min_land_per_player
    max_land_pp = max_land_per_player if max_land_per_player is not None else settings.expect_max_land_per_player

    luxury_radius = min_distance // 2
    city_states = set(get_city_states())
    marine_civs = set(get_marine_civs())
    luxuries = get_luxuries()

    tile_map_params = file_dict.get("tileMap", {}).get("mapParameters", {})
    world_wrap = tile_map_params.get("worldWrap", False)
    map_width = tile_map_params.get("mapSize", {}).get("width", 0) if world_wrap else 0

    game_nations = [
        civ["civName"] for civ in file_dict.get("civilizations", [])
        if civ["civName"] not in city_states
    ]

    # Find start positions: palace/special building → settler/pioneer unit
    nations: dict[str, dict] = {}
    for civ in file_dict.get("civilizations", []):
        name = civ["civName"]
        if name not in game_nations:
            continue
        for city in civ.get("cities", []):
            buildings = city.get("cityConstructions", {}).get("builtBuildings", [])
            if "Palace" in buildings or "Orszaggyules" in buildings:
                loc = city.get("location", {})
                nations[name] = {"x": loc.get("x", 0), "y": loc.get("y", 0)}
                break

    count_ocean = 0
    count_coast = 0
    count_tiles = 0
    tiles_dict: dict[tuple, dict] = {}

    for tile in file_dict.get("tileMap", {}).get("tileList", []):
        base = tile.get("baseTerrain", "")
        if base == "Ocean":
            count_ocean += 1
        elif base == "Coast":
            count_coast += 1
        count_tiles += 1

        pos = tile.get("position", {})
        x, y = pos.get("x", 0), pos.get("y", 0)
        tiles_dict[(x, y)] = tile

        # Settler / Ranchero in civilian slot
        if "civilianUnit" in tile and tile["civilianUnit"]["name"] in ("Settler", "Ranchero"):
            owner = tile["civilianUnit"]["owner"]
            if owner in game_nations and owner not in nations:
                nations[owner] = {"x": x, "y": y}
        # Pioneer in military slot
        elif "militaryUnit" in tile and tile["militaryUnit"]["name"] == "Pioneer":
            owner = tile["militaryUnit"]["owner"]
            if owner in game_nations and owner not in nations:
                nations[owner] = {"x": x, "y": y}

    if not nations:
        return MapCheckResult(ok=False, issues=["Не найдены стартовые позиции наций"])

    boundary_tiles = _find_boundary_tiles(tiles_dict, map_width)

    min_distances: list[int] = []
    marine_no_coast: list[str] = []
    bad_lux: list[str] = []
    near_edge: list[str] = []

    for name, pos in nations.items():
        # Nearest neighbour distance (with wrap)
        nearest = min(
            _hex_distance(pos, other_pos, map_width)
            for other_name, other_pos in nations.items()
            if other_name != name
        )
        min_distances.append(nearest)

        # Marine civ coast check (with wrap)
        if name in marine_civs:
            has_coast = any(
                (_tile_at(nx, ny, tiles_dict, map_width) or {}).get("baseTerrain") == "Coast"
                for nx, ny in _neighbors(pos["x"], pos["y"])
            )
            if not has_coast:
                marine_no_coast.append(name)

        # Luxury radius check (with wrap)
        lux_counts: dict[str, int] = defaultdict(int)
        for (tx, ty), tile in tiles_dict.items():
            resource = tile.get("resource")
            if resource and resource in luxuries:
                if _hex_distance(pos, {"x": tx, "y": ty}, map_width) <= luxury_radius:
                    lux_counts[resource] += 1

        total_lux = sum(lux_counts.values())
        unique_lux = len(lux_counts)
        if total_lux < min_lux or unique_lux < min_unique_lux:
            bad_lux.append(f"{name} (всего={total_lux}, уник={unique_lux})")

        # Edge proximity check: must be >1 tile from real (non-wrapping) boundary
        for (bx, by) in boundary_tiles:
            if _hex_distance(pos, {"x": bx, "y": by}, map_width) <= 1:
                near_edge.append(name)
                break

    water = count_ocean + count_coast
    land = count_tiles - water
    player_count = len(nations)
    expect_min_land = min_land_pp * player_count
    expect_max_land = max_land_pp * player_count
    min_dist = min(min_distances)
    max_dist = max(min_distances)

    issues: list[str] = []
    if land < expect_min_land:
        issues.append(f"Суша {land} < {expect_min_land} (мин {min_land_pp}/игрок)")
    if land > expect_max_land:
        issues.append(f"Суша {land} > {expect_max_land} (макс {max_land_pp}/игрок)")
    if min_dist < min_distance:
        issues.append(f"Мин. дистанция {min_dist} < {min_distance}")
    if max_dist > max_distance:
        issues.append(f"Макс. дистанция {max_dist} > {max_distance}")
    if marine_no_coast:
        issues.append(f"Морские нации без побережья: {', '.join(marine_no_coast)}")
    if bad_lux:
        issues.append(
            f"Люксы в радиусе {luxury_radius} (нужно ≥{min_lux} всего и ≥{min_unique_lux} уник):\n  "
            + "\n  ".join(bad_lux)
        )
    if near_edge:
        issues.append(f"Нации у края карты (≤1 тайла от границы): {', '.join(near_edge)}")

    avg_dist = sum(min_distances) / len(min_distances)
    median_dist = statistics.median(min_distances)

    details = {
        "thresholds": {
            "land_range": [expect_min_land, expect_max_land],
            "dist_range": [min_distance, max_distance],
            "lux_min": min_lux,
            "lux_unique_min": min_unique_lux,
            "lux_radius": luxury_radius,
        },
        "land": land,
        "water": water,
        "tiles": count_tiles,
        "land_pct": round((land / count_tiles * 100) if count_tiles else 0, 2),
        "players": player_count,
        "world_wrap": world_wrap,
        "map_width": map_width,
        "distances": {
            "min": min_dist,
            "max": max_dist,
            "median": float(median_dist),
            "avg": round(avg_dist, 2),
        },
    }

    return MapCheckResult(ok=not issues, issues=issues, details=details)
