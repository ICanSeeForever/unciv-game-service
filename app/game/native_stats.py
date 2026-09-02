"""Exact per-civ income / happiness via the native Unciv engine.

Instead of reimplementing Unciv's stats math (a moving target), we run the real
game code headless: tiny wrappers compiled against the same Unciv.jar the bot games
run on load a save with the native engine and print each civ's `statsForNextTurn` +
`getHappiness()` — the exact figures the world-screen top bar shows.

Two run modes, tried in order:

  1. **Warm daemon** (`StatDaemon`): one long-lived JVM that loads the ruleset once
     and stays resident, so JIT warms up — a cold call is ~5s, every later one ~1s.
     game-service feeds it save paths over stdin and reads one `STATS_JSON=` line back.
  2. **One-shot** (`StatDumper`): a fresh JVM per call (~6s) used only if the daemon
     can't start or has died mid-flight.

Both cost ~60s if the JVM is allowed to exit on its own (Unciv keeps non-daemon
background threads alive), so both `System.exit(0)` the moment the answer is printed.

Engine prep (once per process): extract the builtin rulesets (`jsons/`) from the
mounted Unciv.jar, drop in the bundled base-ruleset mod (`mods/`), and compile the
wrappers against the jar. Results are cached by save hash (turn states are immutable).
Any failure returns None so the caller can fall back to the pure-Python engine.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
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
_CP = f"{_JAR}:{_ENGINE_DIR}"

_prep_lock = threading.Lock()
_ready: bool | None = None  # None = not yet attempted
_jar_sig: tuple | None = None  # (mtime, size) of the jar the engine was built against

_cache: dict[str, dict] = {}
_CACHE_MAX = 512

# Sentinel: the engine responded but can't compute this save (bad/incompatible). The
# caller should NOT fall back to the one-shot (it would fail the same way, slowly).
_ENGINE_ERROR = object()

# Warm-daemon state, guarded by _daemon_lock (also serializes stdin/stdout I/O).
# A dedicated reader thread drains the daemon's stdout into _daemon_q: mixing
# select() with a buffered pipe is unreliable (readline() pulls a line into Python's
# buffer, then select() reports the fd "empty" and we'd miss it), so we never select
# on the pipe — a blocking thread reads lines and the request side gets them with a
# queue timeout.
_daemon_lock = threading.Lock()
_daemon: subprocess.Popen | None = None
_daemon_q: "queue.Queue[str | None]" | None = None
_DAEMON_READY_TIMEOUT = 60.0   # ruleset load + first JIT
_DAEMON_CALL_TIMEOUT = 45.0    # per-save compute (cold first call is the slow one)


def _current_jar_sig() -> tuple | None:
    """(mtime, size) of the Unciv.jar, or None if it's missing."""
    try:
        st = Path(_JAR).stat()
        return (int(st.st_mtime), st.st_size)
    except OSError:
        return None


def _invalidate_if_jar_changed() -> None:
    """Rebuild the engine when the Unciv.jar is swapped under us (a redeploy replaces
    the bind-mounted jar in place, without recreating the container).

    The resident warm daemon is a long-lived JVM: once the jar changes, classes it
    loads afterwards mismatch the ones already resident and fail with
    "Could not initialize class …", so every stats call silently returns nothing.
    On a detected change we reset prep state, wipe the compiled wrappers + extracted
    rulesets, kill the stale daemon, and clear the cache — the next call rebuilds and
    reboots cleanly against the new jar. Cost on the hot path is one ``stat()``.
    """
    global _ready, _jar_sig
    sig = _current_jar_sig()
    if sig is None or sig == _jar_sig:
        return
    with _prep_lock:
        if sig == _jar_sig:  # another thread already handled it
            return
        if _jar_sig is not None:  # not the first run → the jar actually changed
            logger.warning("native stats: Unciv.jar changed %s -> %s; rebuilding engine",
                            _jar_sig, sig)
            with _daemon_lock:
                _kill_daemon()
            _cache.clear()
            try:
                shutil.rmtree(_ENGINE_DIR)
            except OSError:
                pass
            _ready = None
        _jar_sig = sig


def _prepare() -> bool:
    """Idempotently build the engine working dir (jsons + mods + compiled wrappers)."""
    global _ready
    if _ready is not None:
        return _ready
    with _prep_lock:
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
            # Compile both wrappers against the exact runtime jar (needs a JDK).
            proc = subprocess.run(
                ["javac", "-cp", str(jar), "-d", str(_ENGINE_DIR),
                 str(_SRC_DIR / "StatDumper.java"), str(_SRC_DIR / "StatDaemon.java")],
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


def _q_get(timeout: float) -> str | None:
    """Next daemon stdout line, or None on EOF/timeout. Caller holds _daemon_lock."""
    if _daemon_q is None:
        return None
    try:
        return _daemon_q.get(timeout=timeout)  # None = EOF sentinel from the reader
    except queue.Empty:
        return None


def _daemon_alive() -> bool:
    return _daemon is not None and _daemon.poll() is None


def _start_daemon() -> bool:
    """Spawn the warm daemon and wait for DAEMON_READY. Caller holds _daemon_lock."""
    global _daemon, _daemon_q
    try:
        proc = subprocess.Popen(
            ["java", "-Djava.awt.headless=true", "-cp", _CP, "StatDaemon"],
            cwd=str(_ENGINE_DIR),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
    except Exception:
        logger.exception("native stats: failed to spawn daemon")
        return False
    q: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:  # blocking; safe on its own thread
                q.put(line.rstrip("\n"))
        except Exception:
            pass
        finally:
            q.put(None)  # EOF sentinel

    threading.Thread(target=_reader, name="native-stats-reader", daemon=True).start()
    _daemon, _daemon_q = proc, q
    # Wait for readiness (skip Unciv's own log lines until DAEMON_READY).
    while True:
        line = _q_get(_DAEMON_READY_TIMEOUT)
        if line is None:
            logger.warning("native stats: daemon did not become ready; killing")
            _kill_daemon()
            return False
        if line.strip() == "DAEMON_READY":
            logger.info("native stats: warm daemon ready (pid=%s)", proc.pid)
            return True


def _kill_daemon() -> None:
    global _daemon, _daemon_q
    if _daemon is not None:
        try:
            _daemon.kill()
        except Exception:
            pass
    _daemon = None
    _daemon_q = None


def _compute_via_daemon(save_file: Path) -> dict | None:
    """Send a save path to the warm daemon and parse its STATS_JSON reply.
    Restarts the daemon once on failure. Caller holds _daemon_lock."""
    for attempt in (1, 2):
        if not _daemon_alive():
            if not _start_daemon():
                return None
        try:
            _daemon.stdin.write(f"{save_file}\n")
            _daemon.stdin.flush()
        except Exception:
            _kill_daemon()
            continue
        # Read until we get a result line (skip stray log lines the JVM may emit).
        while True:
            line = _q_get(_DAEMON_CALL_TIMEOUT)
            if line is None:
                logger.warning("native stats: daemon timed out/died (attempt %s)", attempt)
                _kill_daemon()
                break  # retry with a fresh daemon
            if line.startswith("STATS_JSON="):
                try:
                    return json.loads(line[len("STATS_JSON="):])
                except Exception:
                    logger.warning("native stats: bad STATS_JSON from daemon")
                    return _ENGINE_ERROR
            if line.startswith("STATS_ERROR="):
                # The engine ran but can't compute this save (e.g. an old, version-
                # incompatible save). The one-shot would hit the same error slowly, so
                # signal "engine responded, don't retry" instead of returning None.
                logger.warning("native stats: daemon reported %s", line)
                return _ENGINE_ERROR
            # else: unrelated log line — keep reading
    return None


def _compute_via_oneshot(save_file: Path) -> dict | None:
    """Fallback: a fresh StatDumper JVM per call (used only if the daemon is down)."""
    try:
        proc = subprocess.run(
            ["java", "-Djava.awt.headless=true", "-cp", _CP, "StatDumper", str(save_file)],
            cwd=str(_ENGINE_DIR), capture_output=True, text=True, timeout=90,
        )
    except Exception:
        logger.exception("native stats: one-shot run failed")
        return None
    for line in proc.stdout.splitlines():
        if line.startswith("STATS_JSON="):
            try:
                return json.loads(line[len("STATS_JSON="):])
            except Exception:
                return None
    logger.warning("native stats: no STATS_JSON from one-shot; stderr=%s", proc.stderr[-400:])
    return None


def prewarm() -> None:
    """Compile the wrappers and boot the warm daemon in the background at startup, so
    the first spectator request doesn't pay the ~7s cold-boot cost. Non-blocking: the
    app starts immediately; if prep fails, compute_income_native falls back as usual."""
    def _run() -> None:
        try:
            if not _prepare():
                return
            with _daemon_lock:
                if not _daemon_alive():
                    _start_daemon()
        except Exception:
            logger.exception("native stats: prewarm failed")
    threading.Thread(target=_run, name="native-stats-prewarm", daemon=True).start()


def compute_income_native(save_string: str) -> dict | None:
    """Return {civName: {gold, science, culture, faith, happiness}} via the native
    engine, or None if unavailable/failed. `save_string` is the raw Unciv save
    (base64+gzip). Cached by save hash; warm daemon first, one-shot as fallback."""
    _invalidate_if_jar_changed()
    if not save_string or not _prepare():
        return None
    key = hashlib.sha1(save_string.encode("utf-8")).hexdigest()
    cached = _cache.get(key)
    if cached is not None:
        return cached
    save_file = _ENGINE_DIR / f"_save_{key}.txt"
    try:
        save_file.write_text(save_string, encoding="utf-8")
        with _daemon_lock:
            result = _compute_via_daemon(save_file)
        if result is _ENGINE_ERROR:
            return None  # engine responded but can't compute — don't retry via one-shot
        if result is None:
            result = _compute_via_oneshot(save_file)
        if result is None:
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
