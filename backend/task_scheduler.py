from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _from_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _safe_timezone(name: Optional[str], fallback: str = "Asia/Shanghai") -> ZoneInfo:
    candidate = (name or "").strip() or fallback
    try:
        return ZoneInfo(candidate)
    except Exception:
        return ZoneInfo(fallback)


@dataclass
class ScheduleJob:
    id: str
    task: str
    schedule_type: str = "daily"  # daily | interval
    time_of_day: str = "08:00"
    interval_minutes: int = 0
    timezone: str = "Asia/Shanghai"
    run_day: str = "yesterday"  # yesterday | today
    enabled: bool = True
    target_url: Optional[str] = None
    auth_data_file: Optional[str] = None
    max_items: int = 50
    max_pages: int = 5
    list_only: bool = False
    created_at: str = field(default_factory=lambda: _now_utc().isoformat())
    updated_at: str = field(default_factory=lambda: _now_utc().isoformat())
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_message: Optional[str] = None
    last_output_dir: Optional[str] = None
    running: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task,
            "schedule_type": self.schedule_type,
            "time_of_day": self.time_of_day,
            "interval_minutes": self.interval_minutes,
            "timezone": self.timezone,
            "run_day": self.run_day,
            "enabled": self.enabled,
            "target_url": self.target_url,
            "auth_data_file": self.auth_data_file,
            "max_items": self.max_items,
            "max_pages": self.max_pages,
            "list_only": self.list_only,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "last_output_dir": self.last_output_dir,
            "running": self.running,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ScheduleJob":
        return cls(
            id=str(payload.get("id") or f"job_{uuid.uuid4().hex[:8]}"),
            task=str(payload.get("task") or ""),
            schedule_type=str(payload.get("schedule_type") or "daily"),
            time_of_day=str(payload.get("time_of_day") or "08:00"),
            interval_minutes=int(payload.get("interval_minutes") or 0),
            timezone=str(payload.get("timezone") or "Asia/Shanghai"),
            run_day=str(payload.get("run_day") or "yesterday"),
            enabled=bool(payload.get("enabled", True)),
            target_url=payload.get("target_url"),
            auth_data_file=payload.get("auth_data_file"),
            max_items=int(payload.get("max_items") or 50),
            max_pages=int(payload.get("max_pages") or 5),
            list_only=bool(payload.get("list_only", False)),
            created_at=str(payload.get("created_at") or _now_utc().isoformat()),
            updated_at=str(payload.get("updated_at") or _now_utc().isoformat()),
            last_run_at=payload.get("last_run_at"),
            next_run_at=payload.get("next_run_at"),
            last_status=payload.get("last_status"),
            last_message=payload.get("last_message"),
            last_output_dir=payload.get("last_output_dir"),
            running=bool(payload.get("running", False)),
        )


def parse_schedule_hint_from_task(task: str) -> Optional[dict[str, Any]]:
    task_text = (task or "").strip()
    task_lower = task_text.lower()
    if not task_text:
        return None

    schedule_keywords = ["定时", "每天", "每日", "每晚", "每早", "every day", "everyday", "cron"]
    interval_keywords = ["每隔", "interval", "every", "分钟", "minute"]
    has_schedule_hint = any(keyword in task_lower for keyword in schedule_keywords) or any(
        keyword in task_lower for keyword in interval_keywords
    )
    if not has_schedule_hint:
        return None

    run_day = "yesterday" if any(k in task_lower for k in ["昨天", "yesterday"]) else "today"

    match_interval = re.search(r"每隔\s*(\d{1,4})\s*分钟", task_text)
    if not match_interval:
        match_interval = re.search(r"every\s*(\d{1,4})\s*minutes?", task_lower)
    if match_interval:
        minutes = max(1, int(match_interval.group(1)))
        return {
            "schedule_type": "interval",
            "interval_minutes": minutes,
            "run_day": run_day,
        }

    hour = 8
    minute = 0
    match_time = re.search(r"(?:每天|每日|每晚|每早|每日上午|每天下午)?\s*(\d{1,2})\s*(?:[:：点时]\s*(\d{1,2}))?\s*(?:分)?", task_text)
    if match_time:
        parsed_hour = int(match_time.group(1))
        parsed_minute = int(match_time.group(2) or 0)
        if 0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59:
            hour, minute = parsed_hour, parsed_minute
    else:
        match_en_time = re.search(r"every\s*day\s*(?:at)?\s*(\d{1,2})(?::(\d{1,2}))?", task_lower)
        if match_en_time:
            parsed_hour = int(match_en_time.group(1))
            parsed_minute = int(match_en_time.group(2) or 0)
            if 0 <= parsed_hour <= 23 and 0 <= parsed_minute <= 59:
                hour, minute = parsed_hour, parsed_minute

    return {
        "schedule_type": "daily",
        "time_of_day": f"{hour:02d}:{minute:02d}",
        "run_day": run_day,
    }


class ScheduleManager:
    def __init__(
        self,
        *,
        storage_path: str,
        runner: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        default_timezone: str = "Asia/Shanghai",
        poll_interval_seconds: int = 20,
    ) -> None:
        self.storage_path = storage_path
        self.runner = runner
        self.default_timezone = default_timezone
        self.poll_interval_seconds = max(5, int(poll_interval_seconds))
        self._jobs: dict[str, ScheduleJob] = {}
        self._lock = asyncio.Lock()
        self._loop_task: Optional[asyncio.Task] = None
        self._run_tasks: set[asyncio.Task] = set()
        self._stopped = False

    async def load(self) -> None:
        async with self._lock:
            self._jobs = {}
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r", encoding="utf-8") as file:
                    payload = json.load(file)
                for raw in payload.get("jobs", []):
                    job = ScheduleJob.from_dict(raw)
                    job.running = False
                    self._ensure_next_run(job)
                    self._jobs[job.id] = job
            await self._save_locked()

    async def start(self) -> None:
        await self.load()
        self._stopped = False
        if self._loop_task and not self._loop_task.done():
            return
        self._loop_task = asyncio.create_task(self._loop(), name="guiagent-scheduler-loop")

    async def stop(self) -> None:
        self._stopped = True
        if self._loop_task:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for task in list(self._run_tasks):
            task.cancel()
        for task in list(self._run_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._run_tasks.clear()

    async def list_jobs(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [job.to_dict() for job in self._sorted_jobs_locked()]

    async def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    async def add_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            job_id = str(payload.get("id") or f"job_{uuid.uuid4().hex[:10]}")
            if job_id in self._jobs:
                raise ValueError(f"Schedule already exists: {job_id}")

            task = str(payload.get("task") or "").strip()
            if not task:
                raise ValueError("task is required")

            hint = parse_schedule_hint_from_task(task) or {}
            schedule_type = str(payload.get("schedule_type") or hint.get("schedule_type") or "daily").strip().lower()
            time_of_day = str(payload.get("time_of_day") or hint.get("time_of_day") or "08:00")
            interval_minutes = int(payload.get("interval_minutes") or hint.get("interval_minutes") or 0)
            run_day = str(payload.get("run_day") or hint.get("run_day") or "yesterday")

            job = ScheduleJob(
                id=job_id,
                task=task,
                schedule_type="interval" if schedule_type == "interval" else "daily",
                time_of_day=time_of_day,
                interval_minutes=interval_minutes,
                timezone=str(payload.get("timezone") or self.default_timezone),
                run_day=run_day if run_day in {"today", "yesterday"} else "yesterday",
                enabled=bool(payload.get("enabled", True)),
                target_url=payload.get("target_url"),
                auth_data_file=payload.get("auth_data_file"),
                max_items=int(payload.get("max_items") or 50),
                max_pages=int(payload.get("max_pages") or 5),
                list_only=bool(payload.get("list_only", False)),
            )
            self._ensure_next_run(job)
            self._jobs[job.id] = job
            await self._save_locked()
            return job.to_dict()

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)

            for key in [
                "task",
                "schedule_type",
                "time_of_day",
                "interval_minutes",
                "timezone",
                "run_day",
                "enabled",
                "target_url",
                "auth_data_file",
                "max_items",
                "max_pages",
                "list_only",
            ]:
                if key in patch:
                    setattr(job, key, patch[key])

            job.schedule_type = "interval" if str(job.schedule_type).lower() == "interval" else "daily"
            job.time_of_day = str(job.time_of_day or "08:00")
            job.interval_minutes = int(job.interval_minutes or 0)
            job.run_day = str(job.run_day or "yesterday")
            if job.run_day not in {"today", "yesterday"}:
                job.run_day = "yesterday"
            job.updated_at = _now_utc().isoformat()

            if "enabled" in patch or "schedule_type" in patch or "time_of_day" in patch or "interval_minutes" in patch:
                job.running = False
                self._ensure_next_run(job)

            await self._save_locked()
            return job.to_dict()

    async def remove_job(self, job_id: str) -> bool:
        async with self._lock:
            existed = self._jobs.pop(job_id, None) is not None
            if existed:
                await self._save_locked()
            return existed

    async def trigger_job(self, job_id: str) -> dict[str, Any]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise KeyError(job_id)
            if job.running:
                return job.to_dict()
            job.running = True
            job.updated_at = _now_utc().isoformat()
            await self._save_locked()

        task = asyncio.create_task(self._run_job(job_id), name=f"guiagent-scheduler-run-{job_id}")
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)
        return await self.get_job(job_id) or {}

    async def _loop(self) -> None:
        while not self._stopped:
            due_job_ids: list[str] = []
            async with self._lock:
                now = _now_utc()
                for job in self._jobs.values():
                    if not job.enabled or job.running:
                        continue
                    next_run_at = _from_iso(job.next_run_at)
                    if next_run_at and next_run_at <= now:
                        job.running = True
                        job.updated_at = now.isoformat()
                        self._ensure_next_run(job, now=now)
                        due_job_ids.append(job.id)
                if due_job_ids:
                    await self._save_locked()

            for job_id in due_job_ids:
                task = asyncio.create_task(self._run_job(job_id), name=f"guiagent-scheduler-run-{job_id}")
                self._run_tasks.add(task)
                task.add_done_callback(self._run_tasks.discard)

            await asyncio.sleep(self.poll_interval_seconds)

    async def _run_job(self, job_id: str) -> None:
        snapshot: Optional[dict[str, Any]] = None
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            snapshot = job.to_dict()

        status = "failed"
        message = "unknown error"
        output_dir = None
        try:
            result = await self.runner(snapshot)
            status = str(result.get("status") or "success")
            message = str(result.get("reasoning") or "ok")
            output_dir = result.get("output_dir")
        except Exception as exc:
            status = "failed"
            message = str(exc)

        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            now = _now_utc()
            job.running = False
            job.last_run_at = now.isoformat()
            job.last_status = status
            job.last_message = message
            job.last_output_dir = output_dir
            job.updated_at = now.isoformat()
            if job.enabled and not job.next_run_at:
                self._ensure_next_run(job, now=now)
            await self._save_locked()

    def _ensure_next_run(self, job: ScheduleJob, now: Optional[dt.datetime] = None) -> None:
        if not job.enabled:
            job.next_run_at = None
            return
        now_utc = now or _now_utc()
        if str(job.schedule_type).lower() == "interval":
            minutes = max(1, int(job.interval_minutes or 1))
            base = _from_iso(job.last_run_at) or now_utc
            if base < now_utc:
                base = now_utc
            job.next_run_at = _to_iso(base + dt.timedelta(minutes=minutes))
            return

        tz = _safe_timezone(job.timezone, fallback=self.default_timezone)
        local_now = now_utc.astimezone(tz)
        hour = 8
        minute = 0
        time_match = re.match(r"^(\d{1,2}):(\d{1,2})$", str(job.time_of_day or "08:00"))
        if time_match:
            candidate_hour = int(time_match.group(1))
            candidate_minute = int(time_match.group(2))
            if 0 <= candidate_hour <= 23 and 0 <= candidate_minute <= 59:
                hour, minute = candidate_hour, candidate_minute

        local_candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if local_candidate <= local_now:
            local_candidate = local_candidate + dt.timedelta(days=1)
        job.next_run_at = _to_iso(local_candidate.astimezone(dt.timezone.utc))

    async def _save_locked(self) -> None:
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        payload = {
            "jobs": [job.to_dict() for job in self._sorted_jobs_locked()],
            "updated_at": _now_utc().isoformat(),
        }
        with open(self.storage_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _sorted_jobs_locked(self) -> list[ScheduleJob]:
        return sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
