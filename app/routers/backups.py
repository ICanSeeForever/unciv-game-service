"""Backup listing endpoints (для спектатора и восстановления в core).

Каталоги бэкапов лежат в ``{backup_path}``:
- по игре:      ``{backup_path}/{game}``          — бэкапы конкретной игры (/save)
- ротация:      ``{backup_path}/rotate/{game}``   — пер-ходовые бэкапы
- служебные:    ``trash``, ``rotate``             — не игры

Эндпоинты только читают файловую систему, ничего не пишут.
"""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import settings

router = APIRouter(prefix="/backups", tags=["backups"])

_RESERVED = {"trash", "rotate"}


def _sorted_files(directory: Path) -> list[str]:
    """Имена файлов каталога, отсортированные по времени модификации (старые→новые)."""
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    return [p.name for p in files]


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
    # только относительный путь без обхода каталогов
    sub = subdirectory.strip("/")
    if not sub or ".." in sub.split("/"):
        raise HTTPException(status_code=400, detail="bad subdirectory")
    directory = Path(settings.get_backup_path()) / sub
    return {"subdirectory": sub, "files": _sorted_files(directory)}
