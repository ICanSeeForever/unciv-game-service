from fastapi import APIRouter, HTTPException

from app.services.task_manager import get_task

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/{task_id}", summary="Poll async task status and log")
async def task_status(task_id: str):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "id": task.id,
        "status": task.status,
        "attempt": task.attempt,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "log": task.log,
        "result": task.result,
        "error": task.error,
    }
