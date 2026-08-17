"""Read-only scans of per-game backup archives (Iron League / bot sync).

Сканирование .tar.gz бэкапов игры: TURNS, порядок веток институтов.
"""
from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path
from typing import Any

from app.game.parser import decode_save

_SKIP_CIV = frozenset({"Barbarians", "Spectator", "Spectators"})
_POLICY_BRANCHES = frozenset({
    "Tradition", "Liberty", "Honor", "Piety", "Patronage",
    "Aesthetics", "Commerce", "Exploration", "Rationalism",
})
# First-seen GP units on the map (exclude Prophet). UU names like
# "Merchant of Venice" still match the keyword.
_GP_KEYWORDS = (
    ("Scientist", "Scientist"),
    ("Engineer", "Engineer"),
    ("Merchant", "Merchant"),
    ("Musician", "Musician"),
    ("Artist", "Artist"),
    ("Writer", "Writer"),
    ("Admiral", "Admiral"),
    ("General", "General"),
)


def _turn_sort_key(path: Path) -> tuple[int, float]:
    match = re.match(r"^(\d+)_", path.name)
    turn = int(match.group(1)) if match else 10**9
    return turn, path.stat().st_mtime


def _archive_paths(base: Path, folder: str) -> list[Path]:
    """Collect backup archives for a game folder (and optional ``{folder}start``)."""
    meas = folder.strip().lower()
    dirs: list[Path] = []
    for name in (meas, meas + "start"):
        candidate = base / name
        if candidate.is_dir():
            dirs.append(candidate)
    paths: list[Path] = []
    for directory in dirs:
        paths.extend(
            p for p in directory.iterdir()
            if p.is_file() and p.name.endswith(".tar.gz")
        )
    if not paths:
        globbed = list(base.glob(f"{meas}_*.tar.gz"))
        paths.extend(globbed)
    return sorted(paths, key=_turn_sort_key)


def _decode_save_bytes(data: bytes) -> dict | None:
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        if text.startswith("{"):
            obj = json.loads(text)
        else:
            obj = decode_save(text)
        return obj if isinstance(obj, dict) and "civilizations" in obj else None
    except Exception:
        return None


def _read_tar_members(archive: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(archive, "r:gz") as tar:
        return {
            Path(member.name).name: member
            for member in tar.getmembers()
            if member.isfile()
        }


def _extract_game_params(archive: Path) -> dict[str, Any]:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if Path(member.name).name != "game_params.json":
                    continue
                raw = tar.extractfile(member)
                if not raw:
                    return {}
                data = json.loads(raw.read().decode("utf-8", errors="replace"))
                return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _matches_filter(
    params: dict[str, Any],
    *,
    game_id: str = "",
    measurement: str = "",
) -> bool:
    gid = (game_id or "").strip()
    meas = (measurement or "").strip().lower()
    if gid:
        param_gid = str(params.get("GAME_ID") or "").strip()
        if param_gid and param_gid != gid:
            param_meas = str(params.get("MEASUREMENT") or "").strip().lower()
            if meas and param_meas != meas:
                return False
    if meas:
        param_meas = str(params.get("MEASUREMENT") or "").strip().lower()
        if param_meas and param_meas not in ("", meas):
            return False
    return True


def _load_save_from_tar(
    archive: Path,
    *,
    game_id: str = "",
    measurement: str = "",
) -> dict | None:
    try:
        params = _extract_game_params(archive)
        if params and not _matches_filter(params, game_id=game_id, measurement=measurement):
            return None
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                if name in ("game_params.json", "game_state.dat", "pacts.db"):
                    continue
                if name.endswith("_Preview") or name.endswith(".db") or name.endswith(".dat"):
                    continue
                if name.endswith(".json") and name != "game_params.json":
                    continue
                if "-" not in name:
                    continue
                raw = tar.extractfile(member)
                if not raw:
                    continue
                data = raw.read()
                if len(data) < 200:
                    continue
                game = _decode_save_bytes(data)
                if game:
                    return game
    except Exception:
        return None
    return None


def best_turns_from_backups(
    base: Path,
    folder: str,
    *,
    game_id: str = "",
) -> dict[str, str]:
    """Return fullest ``{nation: username}`` from backup ``game_params.json`` / TURNS."""
    measurement = folder.strip().lower()
    paths = _archive_paths(base, folder)
    best: dict[str, str] = {}
    best_count = -1
    for archive in paths:
        params = _extract_game_params(archive)
        if params and not _matches_filter(
            params, game_id=game_id, measurement=measurement,
        ):
            continue
        turns = params.get("ORIGINAL_TURNS") or params.get("TURNS") or {}
        if not isinstance(turns, dict):
            continue
        cleaned: dict[str, str] = {}
        for nation, username in turns.items():
            nation_s = str(nation).strip()
            user_s = str(username).strip().lstrip("@")
            if nation_s and user_s:
                cleaned[nation_s] = user_s
        if len(cleaned) > best_count:
            best_count = len(cleaned)
            best = cleaned
    return best


def policy_open_order_from_backups(
    base: Path,
    folder: str,
    *,
    game_id: str = "",
    max_archives: int = 120,
) -> dict[str, list[str]]:
    """First-seen social-policy branch order per nation across turn backups."""
    measurement = folder.strip().lower()
    paths = _archive_paths(base, folder)
    if len(paths) > max_archives:
        step = max(1, len(paths) // max_archives)
        paths = paths[::step]

    order: dict[str, list[str]] = {}
    for archive in paths:
        game = _load_save_from_tar(
            archive, game_id=game_id, measurement=measurement,
        )
        if not game:
            continue
        for civ in game.get("civilizations") or []:
            if not isinstance(civ, dict):
                continue
            nation = str(civ.get("civName") or "").strip()
            if not nation or nation in _SKIP_CIV:
                continue
            adopted = ((civ.get("policies") or {}).get("adoptedPolicies")) or []
            if not isinstance(adopted, list):
                continue
            bucket = order.setdefault(nation, [])
            for item in adopted:
                name = str(item).strip()
                if name in _POLICY_BRANCHES and name not in bucket:
                    bucket.append(name)
    return order


def latest_save_from_backups(
    base: Path,
    folder: str,
    *,
    game_id: str = "",
    max_archives: int = 40,
) -> dict | None:
    """Return decoded save from the newest matching backup archive."""
    measurement = folder.strip().lower()
    paths = sorted(_archive_paths(base, folder), key=_turn_sort_key, reverse=True)
    for archive in paths[:max_archives]:
        game = _load_save_from_tar(
            archive, game_id=game_id, measurement=measurement,
        )
        if game:
            return game
    return None


def classify_great_person(unit_name: str) -> str | None:
    """Map a unit name to a Games.json GP type, or ``None``.

    Prophet is excluded: free/faith prophets would inflate the factory record.
    """
    name = (unit_name or "").strip()
    if not name or "Prophet" in name:
        return None
    for needle, short in _GP_KEYWORDS:
        if needle in name:
            return short
    return None


def great_people_from_backups(
    base: Path,
    folder: str,
    *,
    game_id: str = "",
) -> dict[str, dict[str, int]]:
    """Count first-seen Great Person units per nation (exclude Prophet).

    Считает первых появлений ВЛ по нациям (без Пророка).
    """
    measurement = folder.strip().lower()
    paths = _archive_paths(base, folder)
    seen_ids: set[str] = set()
    counts: dict[str, dict[str, int]] = {}
    for archive in paths:
        game = _load_save_from_tar(
            archive, game_id=game_id, measurement=measurement,
        )
        if not game:
            continue
        tiles = (game.get("tileMap") or {}).get("tileList") or []
        for tile in tiles:
            if not isinstance(tile, dict):
                continue
            for key in ("civilianUnit", "militaryUnit"):
                unit = tile.get(key)
                if not isinstance(unit, dict):
                    continue
                uid = str(unit.get("id") or "").strip()
                if not uid or uid in seen_ids:
                    continue
                gp_type = classify_great_person(str(unit.get("name") or ""))
                if not gp_type:
                    continue
                seen_ids.add(uid)
                owner = str(unit.get("owner") or "").strip()
                if not owner or owner in _SKIP_CIV:
                    continue
                bucket = counts.setdefault(owner, {})
                bucket[gp_type] = bucket.get(gp_type, 0) + 1
    return counts
