"""Game endpoints: info, map-check, start."""
import asyncio
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.game.fetcher import get_save_dict, get_preview_dict, list_all_games
from app.game.static_data import CITY_STATES
from app.launchers import get_launcher
from app.services.map_checker import check_map
from app.services.task_manager import (
    TaskStatus,
    create_task,
    update_task,
)

router = APIRouter(prefix="/games", tags=["games"], responses={404: {"description": "Game not found"}})

_GAME_ID_RE = re.compile(r"^[0-9a-f-]{32,}$", re.IGNORECASE)


def _validate_game_id(game_id: str) -> None:
    if not _GAME_ID_RE.match(game_id):
        raise HTTPException(status_code=400, detail="Invalid game_id format")


# ---------------------------------------------------------------------------
# GET /games  — list all local games
# ---------------------------------------------------------------------------

_EXCLUDE_FROM_HUMAN_LIST = CITY_STATES | frozenset({"Spectator", "Barbarians"})


@router.get(
    "",
    summary="List all local games",
    description=(
        "Scans the mounted `CIV_PATH/MultiplayerFiles/` directory and returns a summary of every "
        "parseable game: human civilizations (city-states and Spectator excluded), "
        "current player, and turn number."
    ),
)
async def list_games():
    games = await list_all_games(exclude_civs=_EXCLUDE_FROM_HUMAN_LIST)
    return {"count": len(games), "games": games}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/info  — lightweight: currentPlayer + turns
# ---------------------------------------------------------------------------

@router.get("/{game_id}/info", summary="Current player and turn number")
async def game_info(
    game_id: str,
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "game_id": game_id,
        "current_player": save.get("currentPlayer"),
        "turns": save.get("turns"),
    }


# ---------------------------------------------------------------------------
# GET /games/{game_id}/map-check  — full map quality check
# ---------------------------------------------------------------------------

@router.get("/{game_id}/map-check", summary="Check map quality (distances, luxuries, marine civs, land ratio)")
async def map_check(
    game_id: str,
    mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override"),
    min_distance: int | None = Query(default=None, description="Override minimum distance between start positions"),
    max_distance: int | None = Query(default=None, description="Override maximum distance between start positions"),
    min_luxuries: int | None = Query(default=None, description="Override minimum total luxuries in radius"),
    min_unique_luxuries: int | None = Query(default=None, description="Override minimum unique luxury types"),
):
    _validate_game_id(game_id)
    try:
        save = await get_save_dict(game_id, mp_server_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    result = check_map(
        save,
        min_distance=min_distance,
        max_distance=max_distance,
        min_luxuries=min_luxuries,
        min_unique_luxuries=min_unique_luxuries,
    )
    return {"ok": result.ok, "issues": result.issues, "details": result.details}


# ---------------------------------------------------------------------------
# GET /games/{game_id}/save  — full parsed save dict
# ---------------------------------------------------------------------------

@router.get("/{game_id}/save", summary="Full parsed save file (large response)")
async def game_save(game_id: str, mp_server_url: str | None = Query(default=None, description="Unciv MP server URL override")):
    _validate_game_id(game_id)
    try:
        return await get_save_dict(game_id, mp_server_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# GET /games/{game_id}/preview  — parsed preview file
# ---------------------------------------------------------------------------

@router.get("/{game_id}/preview", summary="Parsed preview file (lightweight metadata)")
async def game_preview(game_id: str):
    _validate_game_id(game_id)
    try:
        return await get_preview_dict(game_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------------------------------------------------------------------
# POST /games/start  — async game creation task
# ---------------------------------------------------------------------------

class StartGameRequest(BaseModel):
    config: dict
    mp_server_url: str | None = None
    max_attempts: int | None = None


@router.post(
    "/start",
    summary="Start game creation (async)",
    description=(
        "Launches Unciv.jar `--creategame` with the supplied config. "
        "Returns a `task_id` immediately. The service retries until map check passes "
        "or `max_attempts` is exhausted. Poll `GET /tasks/{task_id}` for progress."
    ),
    status_code=202,
)
async def start_game(body: StartGameRequest):
    task = await create_task()
    asyncio.create_task(_run_start_game(task, body))
    return {
        "task_id": task.id,
        "status_url": f"/tasks/{task.id}",
    }


async def _run_start_game(task, body: StartGameRequest) -> None:
    max_attempts = body.max_attempts or settings.max_start_attempts
    launcher = get_launcher()
    await update_task(task, status=TaskStatus.running)

    for attempt in range(1, max_attempts + 1):
        await update_task(task, attempt=attempt)
        task.add_log(f"Попытка {attempt}/{max_attempts}: запуск игры...")

        try:
            output = await launcher.launch(body.config)
            task.add_log(f"Jar вывод: {output[:500]}")
        except Exception as e:
            task.add_log(f"Ошибка запуска: {e}")
            await update_task(task, status=TaskStatus.failed, error=str(e))
            return

        # Extract game_id from jar output — Unciv prints the UUID to stdout
        game_id = _extract_game_id(output)
        if not game_id:
            task.add_log("Не удалось получить game_id из вывода jar")
            await update_task(task, status=TaskStatus.failed, error="No game_id in output")
            return

        task.add_log(f"Создана игра {game_id}, проверяем карту...")

        try:
            save = await get_save_dict(game_id, body.mp_server_url)
        except Exception as e:
            task.add_log(f"Не удалось прочитать сейв: {e}")
            await update_task(task, status=TaskStatus.failed, error=str(e))
            return

        result = check_map(save)
        if result.ok:
            task.add_log("✅ Карта прошла проверку")
            await update_task(task, status=TaskStatus.done, result={"game_id": game_id})
            return

        task.add_log("❌ Карта не подходит:\n" + "\n".join(result.issues))
        if attempt >= max_attempts:
            break

    await update_task(
        task,
        status=TaskStatus.failed,
        error=f"Карта не прошла проверку за {max_attempts} попыток",
    )


def _extract_game_id(output: str) -> str | None:
    """Extract UUID from Unciv.jar output.

    Unciv prints: 'Game started successfully with game id: <uuid>'
    """
    m = re.search(
        r"Game started successfully with game id:\s*([a-f0-9-]+)",
        output, re.IGNORECASE,
    )
    return m.group(1) if m else None
