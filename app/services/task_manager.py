"""In-memory async task store with TTL cleanup."""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.config import settings


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class Task:
    id: str
    status: TaskStatus = TaskStatus.pending
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempt: int = 0
    log: list[str] = field(default_factory=list)
    result: Any = None
    error: str | None = None

    def add_log(self, msg: str) -> None:
        self.log.append(msg)
        self.updated_at = time.time()


_tasks: dict[str, Task] = {}
_lock = asyncio.Lock()
_start_lock = asyncio.Lock()


def get_start_lock() -> asyncio.Lock:
    """Mutex: only one game-creation task runs at a time; others stay pending."""
    return _start_lock


async def create_task() -> Task:
    task = Task(id=str(uuid.uuid4()))
    async with _lock:
        _tasks[task.id] = task
    return task


async def get_task(task_id: str) -> Task | None:
    return _tasks.get(task_id)


async def list_tasks() -> list[Task]:
    return sorted(_tasks.values(), key=lambda t: t.created_at)


async def update_task(task: Task, **kwargs) -> None:
    for k, v in kwargs.items():
        setattr(task, k, v)
    task.updated_at = time.time()


async def cleanup_expired() -> int:
    """Remove tasks older than task_ttl. Returns count removed."""
    cutoff = time.time() - settings.task_ttl
    to_remove = [
        tid for tid, t in _tasks.items()
        if t.status in (TaskStatus.done, TaskStatus.failed, TaskStatus.cancelled)
        and t.updated_at < cutoff
    ]
    async with _lock:
        for tid in to_remove:
            _tasks.pop(tid, None)
    return len(to_remove)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(300)
        await cleanup_expired()


def start_cleanup_task() -> asyncio.Task:
    return asyncio.create_task(_cleanup_loop())
