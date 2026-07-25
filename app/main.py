from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers.games import router as games_router
from app.routers.tasks import router as tasks_router
from app.services.task_manager import start_cleanup_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_cleanup_task()
    yield


app = FastAPI(
    title="unciv-game-service",
    description="Unciv multiplayer game management microservice",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(games_router)
app.include_router(tasks_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
