from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers.games import router as games_router
from app.routers.tasks import router as tasks_router
from app.services.task_manager import start_cleanup_task

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
    yield


app = FastAPI(
    title="unciv-game-service",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS,
    license_info={"name": "MIT"},
    lifespan=lifespan,
)

app.include_router(games_router)
app.include_router(tasks_router)


@app.get("/health", tags=["health"], summary="Health check")
async def health():
    return {"status": "ok"}
