"""Read-only scan of per-game backup archives: Great People factory record.

Сканирование .tar.gz бэкапов игры для подсчёта великих людей (ВЛ).

Раскладка бэкапов (см. routers/backups.py):
- ``{backup_path}/{game}/*.tar.gz``       — пер-ходовые бэкапы игры
- ``{backup_path}/{game}start/*.tar.gz``  — стартовые бэкапы (если есть)

Имена папок регистрозависимы (``IronLeague-30``), поэтому регистр НЕ понижаем
при построении путей — только при сравнении MEASUREMENT-фильтра.
"""
from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path

from app.game.parser import decode_save

_SKIP_CIV = frozenset({"Barbarians", "Spectator", "Spectators"})

# Historical backups keep the OLD MEASUREMENT (``game29``) inside their
# ``game_params.json`` even after the folder was renamed to ``IronLeague-29``.
# Canonicalize both league prefixes so the filter still matches.
_CANON_TEAM_RE = re.compile(r"^(?:ironleague-|game)team(\d+)$")
_CANON_GAME_RE = re.compile(r"^(?:ironleague-|game)(\d+)$")


def _canon_game_key(name: str) -> str:
    """Normalize a game/measurement name across the ``game*`` → ``IronLeague-*`` rename."""
    key = (name or "").strip().lower()
    team = _CANON_TEAM_RE.match(key)
    if team:
        return f"team{team.group(1)}"
    plain = _CANON_GAME_RE.match(key)
    if plain:
        return plain.group(1)
    return key

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
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return turn, mtime


def _archive_paths(base: Path, folder: str) -> list[Path]:
    """Collect backup archives for a game folder (and optional ``{folder}start``).

    ``base`` — корень бэкапов, ``folder`` — имя игры (регистр сохраняется).
    """
    name = folder.strip()
    dirs: list[Path] = []
    for cand in (name, name + "start"):
        candidate = base / cand
        if candidate.is_dir():
            dirs.append(candidate)
    paths: list[Path] = []
    for directory in dirs:
        paths.extend(
            p for p in directory.iterdir()
            if p.is_file() and p.name.endswith(".tar.gz")
        )
    if not paths:
        paths.extend(base.glob(f"{name}_*.tar.gz"))
    return sorted(paths, key=_turn_sort_key)


def _decode_save_bytes(data: bytes) -> dict | None:
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        obj = json.loads(text) if text.startswith("{") else decode_save(text)
        return obj if isinstance(obj, dict) and "civilizations" in obj else None
    except Exception:
        return None


def _extract_game_params(archive: Path) -> dict:
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
    params: dict,
    *,
    game_id: str = "",
    measurement: str = "",
) -> bool:
    gid = (game_id or "").strip()
    meas = _canon_game_key(measurement)
    if gid:
        param_gid = str(params.get("GAME_ID") or "").strip()
        if param_gid and param_gid != gid:
            param_meas = _canon_game_key(str(params.get("MEASUREMENT") or ""))
            if meas and param_meas != meas:
                return False
    if meas:
        param_meas = _canon_game_key(str(params.get("MEASUREMENT") or ""))
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
        if params and not _matches_filter(
            params, game_id=game_id, measurement=measurement,
        ):
            return None
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                if name in ("game_params.json", "game_state.dat", "pacts.db"):
                    continue
                if name.endswith("_Preview") or name.endswith(".db") or name.endswith(".dat"):
                    continue
                if name.endswith(".json"):
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


def classify_great_person(unit_name: str) -> str | None:
    """Map a unit name to a Games.json GP type, or ``None``.

    Пророк исключён: бесплатные/верой пророки завышали бы запись «завода» ВЛ.
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

    Считает первые появления ВЛ по нациям (без Пророка).
    ``base`` — корень бэкапов, ``folder`` — имя игры.
    """
    measurement = folder.strip()
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
