"""Exact per-civ income / happiness via the native Unciv engine.

Instead of reimplementing Unciv's stats math (a moving target), we run the real
game code headless: a tiny wrapper (native_engine/StatDumper.java) is compiled
against the same Unciv.jar the bot games run on, loads a save with the native
engine, and prints each civ's `statsForNextTurn` + `getHappiness()` — the exact
figures the world-screen top bar shows.

Engine prep (once per process): extract the builtin rulesets (`jsons/`) from the
mounted Unciv.jar, drop in the bundled base-ruleset mod (`mods/`), and compile
StatDumper against the jar. Per call: run `java … StatDumper <save>` and parse the
`STATS_JSON=` line. Results are cached by save hash (turn states are immutable).
Any failure returns None so the caller can fall back to the pure-Python engine.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_SRC_DIR = Path(__file__).parent / "native_engine"
_ENGINE_DIR = Path("/tmp/unciv_stat_engine")
_JAR = settings.unciv_jar_path

_lock = threading.Lock()
_ready: bool | None = None  # None = not yet attempted
_cache: dict[str, dict] = {}
_CACHE_MAX = 512


def _prepare() -> bool:
    """Idempotently build the engine working dir (jsons + mods + compiled wrapper)."""
    global _ready
    if _ready is not None:
        return _ready
    with _lock:
        if _ready is not None:
            return _ready
        try:
            jar = Path(_JAR)
            if not jar.exists():
                logger.warning("native stats: Unciv.jar not found at %s", _JAR)
                _ready = False
                return _ready
            _ENGINE_DIR.mkdir(parents=True, exist_ok=True)
            # Builtin rulesets (Civ V Vanilla / GnK) live inside the jar.
            jsons_dir = _ENGINE_DIR / "jsons"
            if not jsons_dir.exists():
                with zipfile.ZipFile(jar) as z:
                    for n in z.namelist():
                        if n.startswith("jsons/") and not n.endswith("/"):
                            z.extract(n, _ENGINE_DIR)
            # Base-ruleset mod ("RekMOD iron"), bundled with the service.
            mods_src = _SRC_DIR / "mods"
            if mods_src.exists():
                shutil.copytree(mods_src, _ENGINE_DIR / "mods", dirs_exist_ok=True)
            # Compile the wrapper against the exact runtime jar (needs a JDK).
            proc = subprocess.run(
                ["javac", "-cp", str(jar), "-d", str(_ENGINE_DIR),
                 str(_SRC_DIR / "StatDumper.java")],
                capture_output=True, text=True, timeout=180,
            )
            if proc.returncode != 0:
                logger.warning("native stats: javac failed: %s", proc.stderr[-800:])
                _ready = False
                return _ready
            logger.info("native stats: engine ready at %s", _ENGINE_DIR)
            _ready = True
            return _ready
        except Exception:
            logger.exception("native stats: engine prep failed")
            _ready = False
            return _ready


def compute_income_native(save_string: str) -> dict | None:
    """Return {civName: {gold, science, culture, faith, happiness}} via the native
    engine, or None if unavailable/failed. `save_string` is the raw Unciv save
    (base64+gzip). Cached by save hash."""
    if not save_string or not _prepare():
        return None
    key = hashlib.sha1(save_string.encode("utf-8")).hexdigest()
    cached = _cache.get(key)
    if cached is not None:
        return cached
    save_file = _ENGINE_DIR / f"_save_{key}.txt"
    try:
        save_file.write_text(save_string, encoding="utf-8")
        proc = subprocess.run(
            ["java", "-Djava.awt.headless=true", "-cp", f"{_JAR}:{_ENGINE_DIR}",
             "StatDumper", str(save_file)],
            cwd=str(_ENGINE_DIR), capture_output=True, text=True, timeout=90,
        )
        result: dict | None = None
        for line in proc.stdout.splitlines():
            if line.startswith("STATS_JSON="):
                result = json.loads(line[len("STATS_JSON="):])
                break
        if result is None:
            logger.warning("native stats: no STATS_JSON in output; stderr=%s",
                           proc.stderr[-400:])
            return None
        if len(_cache) > _CACHE_MAX:
            _cache.clear()
        _cache[key] = result
        return result
    except Exception:
        logger.exception("native stats: run failed")
        return None
    finally:
        save_file.unlink(missing_ok=True)
