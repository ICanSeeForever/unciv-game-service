"""Боевые потери для реплея: та же логика, что у /deaths (``routers/backups``) —
боевой юнит, который был в предыдущем снапшоте и пропал в следующем, считается
погибшим у своего владельца. Отличия:

* счёт ведётся до ВЫБРАННОГО снапшота реплея (бэкап / ротация / живой сейв);
* разбор каждого архива кешируется по (путь, mtime) — повторные запросы и
  листание ходов не декодируют сейвы заново;
* рестарт карты (номер хода уменьшился) обнуляет счёт: юниты старой карты
  «пропадают» все разом, это не потери.
"""
from __future__ import annotations

import os
import tarfile
from collections import OrderedDict
from pathlib import Path

from app.game.parser import decode_save

# Поля, которые есть только у города-государства (см. routers/backups.py).
_CITY_STATE_MARKERS = (
    "cityStatePersonality", "cityStateResource", "cityStateType",
    "cityStateFunctions", "cityStateUniqueUnit",
)

# (path, mtime) -> frame; frame = {"turn", "majors", "units": {id: owner}}
_CACHE: "OrderedDict[tuple[str, float], dict | None]" = OrderedDict()
_CACHE_MAX = 3000


def frame_from_save(save: dict, turn: int | None) -> dict:
    majors: set[str] = set()
    for civ in save.get("civilizations") or []:
        if not isinstance(civ, dict):
            continue
        name = civ.get("civName")
        if not name or name == "Barbarians":
            continue
        if "playerType" in civ and not any(k in civ for k in _CITY_STATE_MARKERS):
            majors.add(name)
    units: dict = {}
    for tile in (save.get("tileMap") or {}).get("tileList") or []:
        u = tile.get("militaryUnit")
        if u and "id" in u:
            units[u["id"]] = u.get("owner")
    if turn is None:
        turn = save.get("turns")
    return {"turn": turn, "majors": majors, "units": units}


def _turn_from_name(path: Path) -> int | None:
    head = path.name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


def _decode_archive(path: Path) -> dict | None:
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                base = os.path.basename(member.name)
                if "-" in base and "Preview" not in base:
                    f = tar.extractfile(member)
                    if f:
                        return decode_save(f.read().decode("utf-8").strip())
    except Exception:  # noqa: BLE001 — битый архив просто пропускаем, как /deaths
        return None
    return None


def archive_frame(path: Path) -> dict | None:
    """Кадр потерь архива (с кешем по mtime). None — архив не читается."""
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return None
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    save = _decode_archive(path)
    frame = frame_from_save(save, _turn_from_name(path)) if save is not None else None
    _CACHE[key] = frame
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return frame


def compute(frames: list[dict]) -> dict:
    """Потери по кадрам (по времени). Возвращает итог до последнего кадра и потери
    за ход последнего кадра; все не-мажоры, кроме варваров, свёрнуты в ``cs``."""
    total: dict[str, int] = {}
    per_turn: dict[int | None, dict[str, int]] = {}
    majors: set[str] = set()
    prev: dict | None = None
    for fr in frames:
        if fr is None:
            continue
        majors |= fr["majors"]
        if prev is not None and fr["turn"] is not None and prev["turn"] is not None \
                and fr["turn"] < prev["turn"]:
            # Рестарт карты: начинаем счёт заново.
            total, per_turn, majors = {}, {}, set(fr["majors"])
            prev = fr
            continue
        if prev is not None:
            bucket = per_turn.setdefault(fr["turn"], {})
            for uid, owner in prev["units"].items():
                if uid not in fr["units"] and owner:
                    total[owner] = total.get(owner, 0) + 1
                    bucket[owner] = bucket.get(owner, 0) + 1
        prev = fr

    def fold(d: dict[str, int]) -> dict[str, int]:
        if not majors:
            return dict(d)
        out: dict[str, int] = {}
        for owner, n in d.items():
            k = owner if (owner == "Barbarians" or owner in majors) else "cs"
            out[k] = out.get(k, 0) + n
        return out

    last_turn = prev["turn"] if prev else None
    return {
        "turn": last_turn,
        "total": fold(total),
        "thisTurn": fold(per_turn.get(last_turn, {})),
        "frames": sum(1 for f in frames if f is not None),
    }
