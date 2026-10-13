"""Job manager for checker runs. Bounded, defensive."""
from __future__ import annotations
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_LOG_LINES = 5000
MAX_KEPT_JOBS = 30


@dataclass
class Job:
    id: str
    accounts: list[tuple[str, str]]
    devices_file: Optional[str] = None
    telegram_token: Optional[str] = None
    telegram_chat: Optional[str] = None

    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    valid: int = 0
    invalid: int = 0
    errors: int = 0
    total: int = 0
    current_index: int = 0

    results: list[dict[str, Any]] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)

    _subscribers: set[asyncio.Queue] = field(default_factory=set)
    _stop_flag: bool = False
    _task: Optional[asyncio.Task] = None

    def emit(self, kind: str, **payload) -> None:
        entry = {"t": time.time(), "kind": kind, **payload}
        self.log.append(entry)
        if len(self.log) > MAX_LOG_LINES:
            del self.log[:1000]
        dead: list[asyncio.Queue] = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "valid": self.valid,
            "invalid": self.invalid,
            "errors": self.errors,
            "total": self.total,
            "current_index": self.current_index,
            "results": self.results[-500:],
            "log_tail": self.log[-200:],
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, accounts, devices_file=None,
                     telegram_token=None, telegram_chat=None) -> Job:
        async with self._lock:
            jid = uuid.uuid4().hex[:12]
            job = Job(id=jid, accounts=accounts, devices_file=devices_file,
                      telegram_token=telegram_token, telegram_chat=telegram_chat)
            job.total = len(accounts)
            self.jobs[jid] = job
            # prune old finished jobs
            if len(self.jobs) > MAX_KEPT_JOBS:
                finished = [
                    (jid, j) for jid, j in self.jobs.items()
                    if j.status in ("done", "stopped", "error")
                ]
                finished.sort(key=lambda x: x[1].created_at)
                for jid_old, _ in finished[:len(self.jobs) - MAX_KEPT_JOBS]:
                    self.jobs.pop(jid_old, None)
            return job

    def get(self, jid: str) -> Optional[Job]:
        return self.jobs.get(jid)

    def list(self) -> list[dict]:
        return [
            {"id": j.id, "status": j.status, "total": j.total,
             "valid": j.valid, "invalid": j.invalid,
             "created_at": j.created_at}
            for j in sorted(self.jobs.values(), key=lambda x: x.created_at, reverse=True)
        ]


manager = JobManager()