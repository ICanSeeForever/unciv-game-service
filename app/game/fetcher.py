"""Game file access: local MultiplayerFiles."""
import asyncio
import ctypes
import ctypes.util
import json
import os
import struct
import tarfile
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

_TZ_MOSCOW = timezone(timedelta(hours=3))


def _fmt_ts(ts: float) -> str:
    """Format Unix timestamp as 'YYYY-MM-DD HH:MM:SS' in UTC+3."""
    return datetime.fromtimestamp(ts, tz=_TZ_MOSCOW).strftime("%Y-%m-%d %H:%M:%S")

from app.config import settings
from app.game.parser import decode_save

# ---------------------------------------------------------------------------
# File birth time via statx (Linux kernel ≥4.11, Python has no built-in)
# ---------------------------------------------------------------------------

_STATX_BTIME = 0x800
_AT_FDCWD = -100
_SYS_STATX = 332  # x86_64

def _file_birth_time(path: Path) -> float | None:
    """Return file creation (birth) time as Unix timestamp, or None on failure."""
    try:
        # statx struct: only the fields we need up to stx_btime
        # offset of stx_btime is 88 bytes from the start
        buf = (ctypes.c_uint8 * 256)()
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        ret = libc.statx(
            _AT_FDCWD,
            str(path).encode(),
            ctypes.c_int(0),
            ctypes.c_uint(_STATX_BTIME),
            buf,
        )
        if ret != 0:
            return None
        # stx_mask is at offset 0 (uint32)
        mask = struct.unpack_from("<I", buf, 0)[0]
        if not (mask & _STATX_BTIME):
            return None
        # stx_btime is at offset 80: tv_sec (int64), tv_nsec at 88 (uint32)
        tv_sec = struct.unpack_from("<q", buf, 80)[0]
        return float(tv_sec)
    except Exception:
        return None


def _file_created_at(path: Path) -> float:
    """Birth time if available, otherwise mtime."""
    t = _file_birth_time(path)
    return t if t is not None else path.stat().st_mtime


def _local_path(game_id: str) -> Path:
    return Path(settings.civ_path) / "MultiplayerFiles" / game_id


def _preview_path(game_id: str) -> Path:
    return Path(settings.civ_path) / "MultiplayerFiles" / f"{game_id}_Preview"


async def get_save_dict(game_id: str) -> dict:
    path = _local_path(game_id)
    if not path.is_file():
        raise FileNotFoundError(f"Game file not found: {game_id}")
    return decode_save(path.read_text(encoding="utf-8").strip())


async def get_preview_dict(game_id: str) -> dict:
    path = _preview_path(game_id)
    if not path.is_file():
        raise FileNotFoundError(f"Preview file not found: {game_id}_Preview")
    raw = path.read_text(encoding="utf-8")
    return decode_save(raw.strip())


def create_backup(game_id: str, *, backup_dir: str, turn=None,
                  nation: str | None = None, max_keep: int = 30) -> str:
    """Tar the save (+ preview) into ``backup_dir`` as ``{turn}_{nation}_{ts}.tar.gz``.

    Spectate-совместимый архив: сам сейв под arcname = game_id (в имени есть дефис),
    preview под ``{game_id}_Preview`` — ровно то, что ищет ``load_spectate_backup``.
    Ротация: в каталоге остаётся не более ``max_keep`` свежих архивов. Возвращает
    имя созданного файла.
    """
    save = _local_path(game_id)
    if not save.is_file():
        raise FileNotFoundError(f"Game file not found: {game_id}")
    preview = _preview_path(game_id)
    bdir = Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    prefix = f"{turn}_{nation}_" if (turn is not None and nation) else ""
    name = f"{prefix}{ts}.tar.gz"
    archive = bdir / name
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(save, arcname=game_id)
        if preview.is_file():
            tar.add(preview, arcname=f"{game_id}_Preview")
    if max_keep and max_keep > 0:
        files = sorted(
            [p for p in bdir.iterdir() if p.is_file() and p.name.endswith(".tar.gz")],
            key=lambda p: p.stat().st_mtime,
        )
        for old in files[:-max_keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return name


def write_save(game_id: str, raw: str) -> None:
    """Atomically write a save file to MultiplayerFiles."""
    path = _local_path(game_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.rename(path)


def write_preview(game_id: str, raw: str) -> None:
    path = _preview_path(game_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(raw, encoding="utf-8")
    tmp.rename(path)


def restore_backup(backup_file: Path, target_game_id: str, *,
                   safety_backup: bool = True) -> dict:
    """Восстановить сейв (+preview) из бэкап-архива в живую игру target_game_id.

    Деструктивно: перезаписывает ``MultiplayerFiles/{target_game_id}`` (и preview).
    Перед перезаписью (по умолчанию) снимается safety-бэкап текущего файла — как
    ``backup=True`` в монолите. Архив — спектат-совместимый ``.tar.gz`` из
    ``create_backup`` (сейв под arcname = game_id, preview под ``{id}_Preview``).
    Возвращает ``{"save": bool, "preview": bool}``.
    """
    if not backup_file.is_file():
        raise FileNotFoundError(f"Backup not found: {backup_file}")
    if safety_backup:
        try:
            create_backup(target_game_id, backup_dir=settings.get_backup_path(),
                          max_keep=30)
        except FileNotFoundError:
            pass  # живого файла ещё нет (первая заливка) — бэкапить нечего
    restored = {"save": False, "preview": False}
    with tarfile.open(backup_file, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            content = extracted.read().decode("utf-8")
            base = os.path.basename(member.name)
            if base.endswith("_Preview"):
                write_preview(target_game_id, content)
                restored["preview"] = True
            else:
                write_save(target_game_id, content)
                restored["save"] = True
    if not restored["save"]:
        raise ValueError("no save file in backup archive")
    return restored


def make_single_player(save: dict, nations: list[str]) -> dict:
    """Перевести сейв в синглплеер с ручными нациями (порт /getsingle-трансформа).

    - ``isOnlineMultiplayer=False``, сложность ``King``;
    - указанные нации → ``playerType=Human``, у остальных ``playerType`` снимается.
    Валидация: нации должны присутствовать в сейве, иначе ``ValueError`` со списком.
    """
    civ_names = {c.get("civName") for c in save.get("civilizations", [])}
    missing = [n for n in nations if n not in civ_names]
    if missing:
        raise ValueError(",".join(missing))
    params = save.setdefault("gameParameters", {})
    params["isOnlineMultiplayer"] = False
    params["difficulty"] = "King"
    save["difficulty"] = "King"
    for civ in save.get("civilizations", []):
        if civ.get("civName") in nations:
            civ["playerType"] = "Human"
        elif "playerType" in civ:
            del civ["playerType"]
    return save


def _parse_game_file(path: Path) -> tuple[dict, float] | None:
    """Parse a single save file; return (game_dict, created_at) or None on error."""
    try:
        game = decode_save(path.read_text(encoding="utf-8").strip())
        return game, _file_created_at(path)
    except Exception:
        return None


async def list_all_games(exclude_civs: frozenset[str]) -> list[dict]:
    """Scan MultiplayerFiles and return summary of every parseable game."""
    base = Path(settings.civ_path) / "MultiplayerFiles"
    if not base.is_dir():
        return []

    paths = [
        p for p in base.iterdir()
        if p.is_file() and not p.name.endswith("_Preview")
    ]

    loop = asyncio.get_event_loop()

    async def _load(path: Path) -> dict | None:
        result = await loop.run_in_executor(None, _parse_game_file, path)
        if result is None:
            return None
        game, created_at = result
        civs = [
            c["civName"]
            for c in game.get("civilizations", [])
            if c.get("playerType") == "Human" and c.get("civName") not in exclude_civs
        ]
        return {
            "game_id": path.name,
            "current_player": game.get("currentPlayer"),
            "turns": game.get("turns") or 0,
            "human_civs": civs,
            "created_at": _fmt_ts(created_at),
        }

    results = await asyncio.gather(*[_load(p) for p in paths])
    return [r for r in results if r is not None]


def get_file_created_at(game_id: str) -> str:
    """Return filesystem creation time for a game file as UTC+3 string."""
    return _fmt_ts(_file_created_at(_local_path(game_id)))


def patch_prophet(game_id: str, nation: str, value: int) -> None:
    """Set Great Prophet counter for nation. value=99 stops spawning, original restores."""
    path = _local_path(game_id)
    if not path.is_file():
        raise FileNotFoundError(f"Game file not found: {game_id}")
    game = decode_save(path.read_text(encoding="utf-8").strip())
    patched = False
    for civ in game.get("civilizations", []):
        if civ.get("civName") == nation:
            constructions = civ.setdefault("civConstructions", {})
            bought = constructions.setdefault("boughtItemsWithIncreasingPrice", {})
            bought["Great Prophet"] = value
            patched = True
            break
    if not patched:
        raise ValueError(f"Nation not found in save: {nation}")
    from app.game.parser import encode_save
    write_save(game_id, encode_save(game))


def load_spectate_backup(backup_file: Path, target_game_id: str) -> str:
    """Extract backup .tar.gz, patch for spectating, write to MultiplayerFiles.

    Returns the Spectator player's playerId (spec_id).
    """
    import base64
    import gzip as _gzip
    from app.game.parser import encode_save

    (Path(settings.civ_path) / "MultiplayerFiles").mkdir(parents=True, exist_ok=True)

    def _decode(raw: str, old_id: str) -> dict:
        decompressed = _gzip.decompress(base64.b64decode(raw)).decode('utf-8')
        return json.loads(decompressed.replace(old_id, target_game_id))

    def _patch_params(d: dict) -> dict:
        params = d.setdefault("gameParameters", {})
        params["speed"] = "Solo-iron"
        params["anyoneCanSpectate"] = True
        params["baseRuleset"] = "RekMOD iron"
        return d

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        original_game_id = None

        with tarfile.open(backup_file, "r:gz") as tar:
            for member in tar.getmembers():
                file_name = os.path.basename(member.name)
                if "-" not in file_name:
                    continue
                is_preview = "Preview" in file_name
                if not is_preview and original_game_id is None:
                    original_game_id = file_name
                member.name = "tmp_Preview" if is_preview else "tmp_main"
                tar.extract(member, path=tmp_dir)

        if not original_game_id:
            raise ValueError("No game file found inside backup archive")

        main_dict = _decode((tmp_path / "tmp_main").read_text(encoding="utf-8").strip(), original_game_id)
        _patch_params(main_dict)
        for player in main_dict.get("civilizations", []):
            if player.get("civName") != "Spectator" and "playerId" in player:
                player["playerId"] = "00000000-0000-0000-0000-000000000000"
        write_save(target_game_id, encode_save(main_dict))

        spec_id = ""
        preview_tmp = tmp_path / "tmp_Preview"
        if preview_tmp.is_file():
            prev_dict = _decode(preview_tmp.read_text(encoding="utf-8").strip(), original_game_id)
            _patch_params(prev_dict)
            for player in prev_dict.get("civilizations", []):
                if player.get("civName") == "Spectator":
                    spec_id = player.get("playerId", "")
                    break
            write_preview(target_game_id, encode_save(prev_dict))

    return spec_id


def delete_game(game_id: str) -> dict[str, bool]:
    """Delete main save file and preview file. Returns which files were deleted."""
    main = _local_path(game_id)
    preview = _preview_path(game_id)
    deleted_main = main.is_file()
    deleted_preview = preview.is_file()
    if deleted_main:
        main.unlink()
    if deleted_preview:
        preview.unlink()
    return {"deleted_main": deleted_main, "deleted_preview": deleted_preview}
