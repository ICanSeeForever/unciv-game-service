from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.auth import verify_api_key
from app.routers.backups import router as backups_router
from app.routers.games import router as games_router
from app.routers.spectator import router as spectator_router
from app.routers.spectator import browser as spectator_browser_router
from app.routers.tasks import router as tasks_router
from app.services.task_manager import start_cleanup_task
from app.game.native_stats import prewarm as prewarm_native_stats

DESCRIPTION = """
**unciv-game-service** — REST API for managing Unciv multiplayer games.

## Features

- **Game data** — read parsed save files: current player, units, capitals, diplomacy, cities, techs
- **Map check** — validate start positions: distances, luxuries, marine civs, land ratio
- **Game creation** — launch `Unciv.jar --creategame` via local subprocess or SSH, with automatic map-check retry loop
- **Async tasks** — long-running jobs (game start) return a `task_id` immediately; poll `GET /tasks/{id}` for progress

## Save file format

Unciv saves are `base64(gzip(JSON))`. This service reads them directly from the
mounted `CIV_PATH/MultiplayerFiles/` directory, or fetches them from the Unciv
multiplayer server as a fallback.
"""

TAGS = [
    {
        "name": "games",
        "description": "Game data endpoints — read and analyse save files.",
    },
    {
        "name": "tasks",
        "description": "Async task polling — check status of long-running operations.",
    },
    {
        "name": "health",
        "description": "Service health check.",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_cleanup_task()
    prewarm_native_stats()  # boot the warm Unciv stat daemon in the background
    yield


app = FastAPI(
    title="unciv-game-service",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS,
    license_info={"name": "MIT"},
    lifespan=lifespan,
)

_auth = [Depends(verify_api_key)]

app.include_router(games_router, dependencies=_auth)
app.include_router(tasks_router, dependencies=_auth)
app.include_router(backups_router, dependencies=_auth)
# Spectator projection for the web viewer. After the "flip" the sole web edge is
# core-service (nginx /api/ → core), which reverse-proxies these paths here with
# the internal API key. game-service is no longer internet-facing, so these are
# now behind api_key like the rest.
app.include_router(spectator_router, dependencies=_auth)
app.include_router(spectator_browser_router, dependencies=_auth)


@app.get("/health", tags=["health"], summary="Health check")
async def health():
    return {"status": "ok"}
