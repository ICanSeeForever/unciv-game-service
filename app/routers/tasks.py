from fastapi import APIRouter, HTTPException

from app.services.task_manager import TaskStatus, get_task, update_task

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


@router.post("/{task_id}/cancel", summary="Cancel a running task")
async def cancel_task(task_id: str):
    task = await get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in (TaskStatus.pending, TaskStatus.running):
        raise HTTPException(status_code=409, detail=f"Task already in terminal state: {task.status}")
    await update_task(task, status=TaskStatus.cancelled)
    return {"ok": True, "task_id": task_id}
