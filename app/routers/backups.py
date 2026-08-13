"""Backup listing endpoints (для спектатора и восстановления в core).

Каталоги бэкапов лежат в ``{backup_path}``:
- по игре:      ``{backup_path}/{game}``          — бэкапы конкретной игры (/save)
- ротация:      ``{backup_path}/rotate/{game}``   — пер-ходовые бэкапы
- служебные:    ``trash``, ``rotate``             — не игры

Эндпоинты только читают файловую систему, ничего не пишут.
"""
import asyncio
import os
import tarfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.game.parser import decode_save

router = APIRouter(prefix="/backups", tags=["backups"])

_RESERVED = {"trash", "rotate"}


def _sorted_files(directory: Path) -> list[str]:
    """Имена файлов каталога, отсортированные по времени модификации (старые→новые)."""
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    return [p.name for p in files]


def _parse_tar_save(archive: Path) -> tuple[dict | None, int | None]:
    """Извлечь и декодировать основной сейв из .tar.gz; вернуть (dict, turn_number)."""
    try:
        turn_num = int(archive.name.split("_")[0])
    except (ValueError, IndexError):
        turn_num = None
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                base = os.path.basename(member.name)
                if "-" in base and "Preview" not in base:
                    f = tar.extractfile(member)
                    if f:
                        return decode_save(f.read().decode("utf-8").strip()), turn_num
    except Exception:
        pass
    return None, turn_num


def _count_military_deaths(archives: list[Path], until_turn: int | None) -> dict[str, int]:
    """Пройти по бэкапам и посчитать потери боевых юнитов по владельцу."""
    totals: dict[str, int] = {}
    prev_units: list[dict] = []

    for archive in archives:
        save, current_turn = _parse_tar_save(archive)
        if save is None:
            continue

        tiles = (save.get("tileMap") or {}).get("tileList") or []
        curr_units = []
        curr_ids = []
        for tile in tiles:
            u = tile.get("militaryUnit")
            if u:
                curr_units.append({"id": u["id"], "owner": u["owner"]})
                curr_ids.append(u["id"])

        id_set = set(curr_ids)
        for unit in prev_units:
            if unit["id"] not in id_set:
                owner = unit["owner"]
                totals[owner] = totals.get(owner, 0) + 1

        prev_units = curr_units
        if until_turn is not None and current_turn is not None and current_turn >= until_turn:
            break

    return totals


@router.get("", summary="Список игровых папок с бэкапами")
async def list_backup_folders():
    base = Path(settings.get_backup_path())
    if not base.is_dir():
        return {"folders": []}
    folders = sorted(
        d.name for d in base.iterdir()
        if d.is_dir() and d.name not in _RESERVED
    )
    return {"folders": folders}


@router.get("/files", summary="Список бэкапов в подкаталоге (по времени)")
async def list_backup_files(subdirectory: str = Query(...)):
    sub = subdirectory.strip("/")
    if not sub or ".." in sub.split("/"):
        raise HTTPException(status_code=400, detail="bad subdirectory")
    directory = Path(settings.get_backup_path()) / sub
    return {"subdirectory": sub, "files": _sorted_files(directory)}


@router.get("/{folder}/deaths", summary="Боевые потери по бэкапам игры (сессии)")
async def backup_deaths(
    folder: str,
    turn: int | None = Query(default=None, description="Остановить анализ на этом ходу"),
):
    folder_clean = folder.strip("/")
    if not folder_clean or ".." in folder_clean.split("/") or folder_clean in _RESERVED:
        raise HTTPException(status_code=400, detail="bad folder")

    base = Path(settings.get_backup_path()) / folder_clean
    if not base.is_dir():
        raise HTTPException(status_code=404, detail=f"No backup folder: {folder}")

    archives = sorted(
        [p for p in base.iterdir() if p.is_file() and p.name.endswith(".tar.gz")],
        key=lambda p: p.stat().st_mtime,
    )

    loop = asyncio.get_event_loop()
    deaths = await loop.run_in_executor(None, _count_military_deaths, archives, turn)
    return {"game": folder, "deaths": deaths}
