from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import datetime as dt
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.responses import Response

from .executor import Executor
from .omniparser_service import OmniParserService
from .planner import Planner
from .schemas import ParseRequest, ParseResponse, PlanRequest, PlanResponse, StepRequest, StepResponse
from .schemas import Element, PlanStepsRequest, PlanStepsResponse
from .schemas import TaskSpec, ExtractRequest, ExtractResponse, AppendRowRequest, AppendRowResponse
from .schemas import SaveOutputResponse, FileListResponse, RunExtractionRequest, RunExtractionResponse
from .output_store import OutputStore
from .storage import ensure_dir, timestamp_name
from .extraction_engine import ExtractionEngine
from .visualizer import annotate_screenshot_base64
from .vlm_service import VLMService
from .task_scheduler import ScheduleManager, parse_schedule_hint_from_task
from .download_skill import GenericDownloadSkill, build_download_intent
from .steel_workflow import build_steel_workflow_config
from .postprocess_skill import unzip_archive, find_picture_root, collect_image_filenames_from_dir, embed_images_to_excel
from .workflow_profiles import resolve_workflow_profile, get_download_keywords
from PIL import Image
import io

load_dotenv()

app = FastAPI(title="GUIAgent Local")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

parser_service = None  # 延迟初始化，避免启动时加载 Florence-2


class _ContextScopedProxy:
    """Proxy object that resolves target instance from ContextVar."""

    def __init__(self, context_var: contextvars.ContextVar, default_obj: Any) -> None:
        object.__setattr__(self, "_context_var", context_var)
        object.__setattr__(self, "_default_obj", default_obj)

    def _resolve(self) -> Any:
        current = self._context_var.get()
        return current if current is not None else self._default_obj

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._resolve(), name, value)

def get_parser_service():
    """延迟初始化 OmniParser 服务"""
    global parser_service
    if parser_service is None:
        parser_service = OmniParserService()
    return parser_service

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data", "screenshots"))
ensure_dir(DATA_DIR)

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data", "outputs"))
ensure_dir(OUTPUT_DIR)
SCHEDULES_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data", "schedules.json"))

_executor_ctx: contextvars.ContextVar[Optional[Executor]] = contextvars.ContextVar("executor_ctx", default=None)
_planner_ctx: contextvars.ContextVar[Optional[Planner]] = contextvars.ContextVar("planner_ctx", default=None)
_output_store_ctx: contextvars.ContextVar[Optional[OutputStore]] = contextvars.ContextVar("output_store_ctx", default=None)
_extraction_engine_ctx: contextvars.ContextVar[Optional[ExtractionEngine]] = contextvars.ContextVar("extraction_engine_ctx", default=None)

_default_executor = Executor()
_default_planner = Planner()
_default_output_store = OutputStore(OUTPUT_DIR)

_default_extraction_engine = ExtractionEngine(
    executor=_default_executor,
    parser_service=None,  # 延迟初始化
    planner=_default_planner,
    output_store=_default_output_store,
    data_dir=DATA_DIR,
)


executor = _ContextScopedProxy(_executor_ctx, _default_executor)
planner = _ContextScopedProxy(_planner_ctx, _default_planner)
output_store = _ContextScopedProxy(_output_store_ctx, _default_output_store)
extraction_engine = _ContextScopedProxy(_extraction_engine_ctx, _default_extraction_engine)


@dataclass
class ExecutionSession:
    session_id: str
    executor: Executor
    planner: Planner
    output_store: OutputStore
    extraction_engine: ExtractionEngine
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))


def _create_execution_session(prefix: str = "task") -> ExecutionSession:
    session_id = f"{prefix}_{uuid.uuid4().hex[:8]}"
    session_executor = Executor()
    session_planner = Planner()
    session_output_store = OutputStore(OUTPUT_DIR)
    session_extraction_engine = ExtractionEngine(
        executor=session_executor,
        parser_service=None,
        planner=session_planner,
        output_store=session_output_store,
        data_dir=DATA_DIR,
    )
    return ExecutionSession(
        session_id=session_id,
        executor=session_executor,
        planner=session_planner,
        output_store=session_output_store,
        extraction_engine=session_extraction_engine,
    )


@contextlib.asynccontextmanager
async def _bind_execution_session(session: ExecutionSession):
    tokens = [
        (_executor_ctx, _executor_ctx.set(session.executor)),
        (_planner_ctx, _planner_ctx.set(session.planner)),
        (_output_store_ctx, _output_store_ctx.set(session.output_store)),
        (_extraction_engine_ctx, _extraction_engine_ctx.set(session.extraction_engine)),
    ]
    try:
        yield
    finally:
        for context_var, token in reversed(tokens):
            context_var.reset(token)


async def _close_execution_session(session: ExecutionSession) -> None:
    try:
        await session.executor.stop()
    except Exception as exc:
        logger.warning(f"Failed to close session executor {session.session_id}: {exc}")


def _resolve_schedule_run_date(run_day: str, timezone_name: str = "Asia/Shanghai") -> str:
    try:
        timezone = dt.timezone.utc if not timezone_name else ZoneInfo(timezone_name)
    except Exception:
        timezone = dt.timezone.utc
    local_now = dt.datetime.now(timezone)
    if str(run_day or "").strip().lower() == "yesterday":
        date_value = local_now.date() - dt.timedelta(days=1)
    else:
        date_value = local_now.date()
    return date_value.strftime("%Y-%m-%d")


def _resolve_schedule_time_window_legacy(
    *,
    task: str,
    run_day: str,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[str, str, str]:
    run_date = _resolve_schedule_run_date(run_day=run_day, timezone_name=timezone_name)
    default_start = f"{run_date} 00:00:00"
    default_end = f"{run_date} 23:59:59"

    task_text = str(task or "").strip()
    task_lower = task_text.lower()
    if not task_text:
        return default_start, default_end, run_date

    full_day_markers = ["全天", "整天", "一整天", "全天候", "whole day", "all day"]
    if any(marker in task_lower for marker in full_day_markers):
        return default_start, default_end, run_date

    try:
        parsed_start, parsed_end = _extract_date_range(task_text)
    except Exception as exc:
        logger.warning(f"Failed to parse schedule date range, fallback to full-day: {exc}")
        return default_start, default_end, run_date

    def _parse_datetime_value(value: str) -> Optional[dt.datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                if fmt == "%Y-%m-%d":
                    parsed = parsed.replace(hour=0, minute=0, second=0)
                return parsed
            except ValueError:
                continue
        return None

    start_dt = _parse_datetime_value(parsed_start)
    end_dt = _parse_datetime_value(parsed_end)
    if start_dt is None or end_dt is None:
        return default_start, default_end, run_date

    has_explicit_date = bool(re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", task_text))
    if has_explicit_date:
        start_date_text = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_date_text = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        run_date_token = start_dt.strftime("%Y-%m-%d")
        return start_date_text, end_date_text, run_date_token

    anchor_date = dt.datetime.strptime(run_date, "%Y-%m-%d").date()
    anchored_start = dt.datetime.combine(anchor_date, start_dt.time())
    anchored_end = dt.datetime.combine(anchor_date, end_dt.time())
    if anchored_end < anchored_start:
        anchored_end = anchored_start

    return (
        anchored_start.strftime("%Y-%m-%d %H:%M:%S"),
        anchored_end.strftime("%Y-%m-%d %H:%M:%S"),
        run_date,
    )


def _resolve_schedule_time_window(
    *,
    task: str,
    run_day: str,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[str, str, str]:
    run_date = _resolve_schedule_run_date(run_day=run_day, timezone_name=timezone_name)
    default_start = f"{run_date} 00:00:00"
    default_end = f"{run_date} 23:59:59"
    task_text = str(task or "").strip()
    task_lower = task_text.lower()

    if not task_text:
        return default_start, default_end, run_date

    def _extract_window_segment(text: str) -> str:
        patterns = [
            r"(?:选择|筛选)?(?:日期|时间范围|时间段|时间)\s*(?:为|是|:|：)?\s*([^，。；\n]+)",
            r"(?:date|time)\s*(?:range)?\s*(?:is|=|:)?\s*([^,.;\n]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                segment = str(match.group(1) or "").strip()
                if segment:
                    return segment
        return text

    parse_source = _extract_window_segment(task_text)
    parse_lower = parse_source.lower()

    full_day_markers = ["全天", "整天", "一整天", "whole day", "all day"]
    if any(marker in parse_lower for marker in full_day_markers):
        return default_start, default_end, run_date

    cn_digit_map = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }

    def _cn_to_int(token: str) -> Optional[int]:
        value = str(token or "").strip()
        if not value:
            return None
        if value.isdigit():
            return int(value)
        if value == "十":
            return 10
        if "十" in value:
            left, right = value.split("十", 1)
            left_value = 1 if left == "" else cn_digit_map.get(left)
            right_value = 0 if right == "" else cn_digit_map.get(right)
            if left_value is None or right_value is None:
                return None
            return left_value * 10 + right_value
        return cn_digit_map.get(value)

    def _parse_time_fragment(fragment: str) -> Optional[str]:
        text = str(fragment or "").strip().lower().replace("：", ":")
        if not text:
            return None

        period = ""
        if any(token in text for token in ["下午", "晚上", "傍晚", "pm", "p.m"]):
            period = "pm"
        elif any(token in text for token in ["上午", "早上", "凌晨", "am", "a.m"]):
            period = "am"
        elif "中午" in text:
            period = "noon"

        match_colon = re.search(r"(\d{1,2})\s*:\s*(\d{1,2})(?:\s*:\s*(\d{1,2}))?", text)
        if match_colon:
            hour = int(match_colon.group(1))
            minute = int(match_colon.group(2))
            second = int(match_colon.group(3) or 0)
        else:
            match_cn = re.search(
                r"([零〇一二两三四五六七八九十\d]{1,3})\s*(?:点|时)(?:\s*([零〇一二两三四五六七八九十\d]{1,2})\s*分?)?(?:\s*([零〇一二两三四五六七八九十\d]{1,2})\s*秒?)?",
                text,
            )
            if not match_cn:
                return None
            hour = _cn_to_int(match_cn.group(1) or "")
            minute = _cn_to_int(match_cn.group(2) or "0")
            second = _cn_to_int(match_cn.group(3) or "0")
            if hour is None or minute is None or second is None:
                return None

        if period == "pm" and 1 <= hour <= 11:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        elif period == "noon" and 1 <= hour <= 11:
            hour += 12

        hour = max(0, min(23, hour))
        minute = max(0, min(59, minute))
        second = max(0, min(59, second))
        return f"{hour:02d}:{minute:02d}:{second:02d}"

    time_pattern = re.compile(
        r"(?:上午|下午|中午|凌晨|早上|晚上|傍晚|am|pm|a\.m|p\.m)?\s*[零〇一二两三四五六七八九十\d]{1,3}\s*(?:[:：]\s*\d{1,2}(?:\s*[:：]\s*\d{1,2})?|点|时(?:\s*[零〇一二两三四五六七八九十\d]{1,2}\s*分?)?(?:\s*[零〇一二两三四五六七八九十\d]{1,2}\s*秒?)?)"
    )
    parsed_times = []
    for match in time_pattern.finditer(parse_source):
        parsed = _parse_time_fragment(match.group(0))
        if parsed:
            parsed_times.append(parsed)

    if parsed_times:
        start_time = parsed_times[0]
        end_time = parsed_times[1] if len(parsed_times) >= 2 else parsed_times[0]

        iso_tokens = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", parse_source)
        if iso_tokens:
            def _normalize_date_token(token: str) -> str:
                parts = token.replace("/", "-").split("-")
                return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"

            start_day = _normalize_date_token(iso_tokens[0])
            end_day = _normalize_date_token(iso_tokens[1] if len(iso_tokens) >= 2 else iso_tokens[0])
        else:
            start_day = run_date
            end_day = run_date

        start_text = f"{start_day} {start_time}"
        end_text = f"{end_day} {end_time}"
        if end_text < start_text:
            end_text = start_text
        return start_text, end_text, start_day

    try:
        parsed_start, parsed_end = _extract_date_range(parse_source)
    except Exception as exc:
        logger.warning(f"Failed to parse schedule date range, fallback to full-day: {exc}")
        return default_start, default_end, run_date

    def _parse_datetime_value(value: str) -> Optional[dt.datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                if fmt == "%Y-%m-%d":
                    parsed = parsed.replace(hour=0, minute=0, second=0)
                return parsed
            except ValueError:
                continue
        return None

    start_dt = _parse_datetime_value(parsed_start)
    end_dt = _parse_datetime_value(parsed_end)
    if start_dt is None or end_dt is None:
        return default_start, default_end, run_date

    has_explicit_date = bool(re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", parse_source))
    if has_explicit_date:
        return (
            start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            start_dt.strftime("%Y-%m-%d"),
        )

    anchor_date = dt.datetime.strptime(run_date, "%Y-%m-%d").date()
    anchored_start = dt.datetime.combine(anchor_date, start_dt.time())
    anchored_end = dt.datetime.combine(anchor_date, end_dt.time())
    if anchored_end < anchored_start:
        anchored_end = anchored_start
    return (
        anchored_start.strftime("%Y-%m-%d %H:%M:%S"),
        anchored_end.strftime("%Y-%m-%d %H:%M:%S"),
        run_date,
    )


def _build_schedule_output_dir(job_id: str, run_date: str) -> str:
    safe_job_id = re.sub(r"[^a-zA-Z0-9_-]", "_", job_id or "job")
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = os.path.join(OUTPUT_DIR, "scheduled", safe_job_id, f"{run_date}_{timestamp}")
    ensure_dir(output_dir)
    return output_dir


def _prepare_schedule_payload(
    *,
    task: str,
    target_url: Optional[str],
    auth_data_file: Optional[str],
    max_items: int,
    max_pages: int,
    list_only: bool,
    explicit_payload: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    schedule_hint = parse_schedule_hint_from_task(task)
    if not schedule_hint:
        return None

    payload: dict[str, Any] = {
        "task": task,
        "target_url": target_url,
        "auth_data_file": auth_data_file,
        "max_items": max_items,
        "max_pages": max_pages,
        "list_only": list_only,
        "enabled": True,
    }

    for key in ["schedule_type", "time_of_day", "interval_minutes", "run_day"]:
        if key in schedule_hint:
            payload[key] = schedule_hint[key]

    if explicit_payload:
        for key in [
            "id",
            "schedule_type",
            "time_of_day",
            "interval_minutes",
            "run_day",
            "timezone",
            "enabled",
            "target_url",
            "auth_data_file",
            "max_items",
            "max_pages",
            "list_only",
        ]:
            if key in explicit_payload:
                payload[key] = explicit_payload.get(key)

    return payload


def _classify_runtime_exception(exc: Exception) -> Optional[dict[str, Any]]:
    """Classify runtime exceptions into user-facing actionable categories."""
    exc_name = exc.__class__.__name__
    message = str(exc or "").strip()
    normalized = f"{exc.__class__.__module__}.{exc_name} {message}".lower()
    base_url = os.getenv("VLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    connection_markers = [
        "apiconnectionerror",
        "connecterror",
        "connection refused",
        "failed to establish a new connection",
        "winerror 10061",
        "err_connection_refused",
    ]
    timeout_markers = [
        "apitimeouterror",
        "timeout",
        "timed out",
        "readtimeout",
        "writetimeout",
    ]
    auth_markers = [
        "authenticationerror",
        "unauthorized",
        "invalid api key",
        "incorrect api key",
        "forbidden",
        " 401 ",
    ]

    if any(marker in normalized for marker in connection_markers):
        return {
            "code": "vlm_connection_error",
            "message": message or exc_name,
            "user_message": "VLM 服务连接失败：无法连接到模型服务，请检查网络或服务是否启动。",
            "hint": f"请检查 VLM_BASE_URL（当前: {base_url}）以及目标服务进程是否在运行。",
            "retriable": True,
        }

    if any(marker in normalized for marker in timeout_markers):
        return {
            "code": "vlm_timeout",
            "message": message or exc_name,
            "user_message": "VLM 请求超时：模型响应超时，请稍后重试。",
            "hint": f"可检查网络连通性与模型服务负载（VLM_BASE_URL: {base_url}）。",
            "retriable": True,
        }

    if any(marker in normalized for marker in auth_markers):
        return {
            "code": "vlm_auth_error",
            "message": message or exc_name,
            "user_message": "VLM 鉴权失败：请检查 API Key 与服务权限配置。",
            "hint": "请确认 VLM_API_KEY（或 OPENAI_API_KEY）配置正确且未过期。",
            "retriable": False,
        }

    return None


async def _run_scheduled_task(payload: dict[str, Any]) -> dict[str, Any]:
    task = str(payload.get("task") or "").strip()
    if not task:
        raise RuntimeError("schedule task is empty")
    if not _is_steel_inspection_task(
        task,
        target_url=payload.get("target_url"),
        auth_data_file=payload.get("auth_data_file"),
    ):
        raise RuntimeError("scheduled task currently supports steel flow only")

    run_day = str(payload.get("run_day") or "yesterday")
    timezone_name = str(payload.get("timezone") or "Asia/Shanghai")
    start_date, end_date, run_date = _resolve_schedule_time_window(
        task=task,
        run_day=run_day,
        timezone_name=timezone_name,
    )
    output_dir = _build_schedule_output_dir(str(payload.get("id") or "job"), run_date)

    scheduled_session = _create_execution_session(prefix=f"schedule_{payload.get('id', 'job')}")
    token_executor = _executor_ctx.set(scheduled_session.executor)
    token_planner = _planner_ctx.set(scheduled_session.planner)
    token_output_store = _output_store_ctx.set(scheduled_session.output_store)
    token_extraction_engine = _extraction_engine_ctx.set(scheduled_session.extraction_engine)

    try:
        resolved_auth_data = _resolve_auth_data_file(
            auth_data_file=payload.get("auth_data_file"),
            target_url=payload.get("target_url"),
        )
        target_url = _extract_target_url(task, payload.get("target_url"))
        if not target_url:
            target_url = _extract_target_url_from_auth_data(
                auth_data_file=resolved_auth_data,
                fallback_url=payload.get("target_url"),
            )
        if not target_url:
            raise RuntimeError("scheduled steel task missing target_url")

        run_result = await _run_steel_download_pipeline(
            task=task,
            target_url=target_url,
            max_items=int(payload.get("max_items") or 50),
            max_pages=int(payload.get("max_pages") or 5),
            auth_data_file=resolved_auth_data,
            start_date_override=start_date,
            end_date_override=end_date,
            output_dir_override=output_dir,
            publish_to_output_root=False,
        )
        run_result["output_dir"] = run_result.get("output_dir") or output_dir
        run_result["run_date"] = run_date
        run_result["date_range"] = {
            "start": start_date,
            "end": end_date,
        }
        return run_result
    finally:
        _extraction_engine_ctx.reset(token_extraction_engine)
        _output_store_ctx.reset(token_output_store)
        _planner_ctx.reset(token_planner)
        _executor_ctx.reset(token_executor)
        await _close_execution_session(scheduled_session)


schedule_manager = ScheduleManager(
    storage_path=SCHEDULES_PATH,
    runner=_run_scheduled_task,
    default_timezone=os.getenv("SCHEDULE_TIMEZONE", "Asia/Shanghai"),
    poll_interval_seconds=int(os.getenv("SCHEDULE_POLL_INTERVAL", "20") or "20"),
)


@app.on_event("startup")
async def _startup_scheduler() -> None:
    try:
        await schedule_manager.start()
        logger.info("Schedule manager started")
    except Exception as exc:
        logger.exception(f"Failed to start schedule manager: {exc}")


@app.on_event("shutdown")
async def _shutdown_scheduler() -> None:
    try:
        await schedule_manager.stop()
        logger.info("Schedule manager stopped")
    except Exception as exc:
        logger.exception(f"Failed to stop schedule manager: {exc}")


@app.post("/parse", response_model=ParseResponse)
def parse(request: ParseRequest) -> ParseResponse:
    return get_parser_service().parse(request)


@app.post("/plan", response_model=PlanResponse)
def plan(request: PlanRequest) -> PlanResponse:
    return planner.plan(request)


async def _capture_and_parse() -> tuple[str, ParseResponse]:
    """
    截图并解析元素

    根据环境变量 USE_DOM_ANNOTATION 决定使用：
    - DOM 标注（新方案）：使用 DOM 坐标在截图上绘制标注框
    - OmniParser 标注（旧方案）：使用 YOLO + Florence2 检测元素
    """
    use_dom_annotation = os.getenv("USE_DOM_ANNOTATION", "1").strip().lower() in {"1", "true", "yes", "on"}

    screenshot_path = os.path.join(DATA_DIR, timestamp_name("screenshot"))
    await executor.screenshot(screenshot_path)

    with open(screenshot_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")

    if use_dom_annotation:
        # 新方案：DOM 驱动的视觉标注
        return await _capture_and_annotate_dom(screenshot_path, image_b64)
    else:
        # 旧方案：OmniParser 标注
        parse_resp = get_parser_service().parse(ParseRequest(image_base64=image_b64, use_paddleocr=True))
        return screenshot_path, parse_resp


async def _capture_and_annotate_dom(screenshot_path: str, image_b64: str) -> tuple[str, ParseResponse]:
    """
    使用 DOM 标注方案

    流程：
    1. 调用 DOM 标记（mark_page_elements）
    2. 使用 visualizer 在截图上绘制标注框
    3. 构造 ParseResponse
    """
    import logging
    logger = logging.getLogger(__name__)

    # 1. 获取 DOM 元素
    dom_result = await executor.mark_page_elements()
    dom_elements = dom_result.get('elements', [])
    viewport = dom_result.get('viewport', {})

    logger.info(f"DOM marking found {len(dom_elements)} elements")

    # 2. 使用 visualizer 标注截图
    try:
        annotated_image_b64 = annotate_screenshot_base64(
            image_base64=image_b64,
            elements=dom_elements,
            max_elements=None,  # 标注所有元素
        )

        # 保存标注图片到磁盘（用于 debug）
        annotated_path = screenshot_path.replace('.png', '_annotated.png')
        annotated_data = base64.b64decode(annotated_image_b64)
        with open(annotated_path, 'wb') as f:
            f.write(annotated_data)
        logger.info(f"Saved annotated image to {annotated_path}")

    except Exception as e:
        logger.error(f"Failed to annotate screenshot: {e}")
        # 如果标注失败，使用原始截图
        annotated_image_b64 = image_b64

    # 3. 转换 DOM 元素为 Element 对象
    elements = []
    for idx, elem in enumerate(dom_elements):
        # 提取元素 ID（去掉 "skyvern-" 前缀，只保留数字）
        elem_id_str = elem.get('id', '')
        if isinstance(elem_id_str, str) and '-' in elem_id_str:
            elem_id = int(elem_id_str.split('-')[-1])
        else:
            elem_id = idx

        # 提取坐标
        rect = elem.get('rect', {})
        x = rect.get('x', rect.get('left', 0))
        y = rect.get('y', rect.get('top', 0))
        width = rect.get('width', 0)
        height = rect.get('height', 0)

        # 计算中心点
        center_x = x + width // 2
        center_y = y + height // 2

        # 提取文本内容
        text = elem.get('text', '').strip()

        # 提取标签类型
        tag_name = elem.get('tagName', 'unknown')

        # 确定元素类型
        if tag_name == 'input':
            elem_type = 'dom_input'
        elif tag_name == 'a':
            elem_type = 'dom_link'
        elif tag_name == 'button':
            elem_type = 'dom_button'
        else:
            elem_type = f'dom_{tag_name}'

        # 构造 Element 对象
        element = Element(
            id=elem_id,
            type=elem_type,
            content=text[:200] if text else f"{tag_name}",  # 限制文本长度
            center=(center_x, center_y),
            bbox=(x, y, width, height),
        )
        elements.append(element)

    # 4. 构造 ParseResponse
    image_size = (viewport.get('width', 1920), viewport.get('height', 1080))

    parse_resp = ParseResponse(
        elements=elements,
        annotated_image_base64=annotated_image_b64,
        image_size=image_size,
    )

    logger.info(f"DOM annotation complete: {len(elements)} elements, image_size={image_size}")

    return screenshot_path, parse_resp


def _merge_dom_inputs(parse_resp: ParseResponse, dom_elements: list[dict]) -> None:
    next_id = max([e.id for e in parse_resp.elements], default=-1) + 1
    for dom in dom_elements:
        dom_content = dom.get("content", "").strip()
        if not dom_content:
            dom_content = "input search box"
        parse_resp.elements.append(
            Element(
                id=next_id,
                type=dom["type"],
                content=dom_content,
                center=dom["center"],
                bbox=dom["bbox"],
            )
        )
        next_id += 1


def _extract_query(task: str) -> Optional[str]:
    # Prefer quoted text: "..." or '...'
    match = re.search(r"\"([^\"]+)\"|'([^']+)'", task)
    if match:
        return match.group(1) or match.group(2)
    # Pattern: 在搜索框中输入XXX并回车
    match = re.search(r"搜索框.*?输入\s*([^\s并。]+)", task)
    if match:
        return match.group(1)
    # Keywords with separators (avoid matching 搜索框本身)
    match = re.search(r"(?:搜索|search|查询|检索|搜)[:：\s]+([^\s并。]+)", task, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"(?:输入|input)[:：\s]+([^\s并。]+)", task, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _is_search_task(task: str) -> bool:
    keywords = ["搜索", "search", "查询", "检索", "搜", "输入"]
    task_lower = task.lower()
    return any(k.lower() in task_lower for k in keywords)


def _is_typing_task(task: str) -> bool:
    keywords = ["输入", "type", "键入", "填入", "enter", "press enter"]
    task_lower = task.lower()
    return any(k.lower() in task_lower for k in keywords)


def _canonical_field_name(field: str) -> str:
    field_lower = str(field or "").strip().lower()
    mapping = {
        "name": "title",
        "book_name": "title",
        "movie_name": "title",
        "title": "title",
        "书名": "title",
        "片名": "title",
        "电影名": "title",
        "价格": "price",
        "price": "price",
        "作者": "author",
        "author": "author",
        "director": "author",
        "导演": "author",
        "编剧": "author",
        "vote": "votes",
        "votes": "votes",
        "vote_count": "votes",
        "votes_count": "votes",
        "rating_count": "votes",
        "review_count": "votes",
        "评分人数": "votes",
        "评价人数": "votes",
        "rating": "rating",
        "score": "rating",
        "评分": "rating",
        "链接": "url",
        "link": "url",
        "url": "url",
        "摘要": "summary",
        "summary": "summary",
        "简介": "summary",
        "内容简介": "summary",
        "description": "summary",
        "overview": "summary",
        "plot": "summary",
        "内容": "content",
        "content": "content",
        "时间": "time",
        "time": "time",
        "来源": "source",
        "source": "source",
    }
    return mapping.get(field_lower, field_lower)


def _resolve_requested_fields(task: str, spec_fields: Optional[list]) -> Optional[set[str]]:
    task_lower = (task or "").lower()

    # 显式要求全部字段时不做过滤
    all_fields_markers = ["所有字段", "全部字段", "所有信息", "完整信息", "all fields", "full fields"]
    if any(marker in task_lower for marker in all_fields_markers):
        return None

    requested: set[str] = set()

    if isinstance(spec_fields, list):
        for field in spec_fields:
            canonical = _canonical_field_name(str(field))
            if canonical:
                requested.add(canonical)

    keyword_mapping = {
        "书名": "title",
        "标题": "title",
        "title": "title",
        "name": "title",
        "片名": "title",
        "电影名": "title",
        "价格": "price",
        "price": "price",
        "作者": "author",
        "author": "author",
        "导演": "author",
        "评分": "rating",
        "score": "rating",
        "rating": "rating",
        "评分人数": "votes",
        "评价人数": "votes",
        "投票人数": "votes",
        "投票": "votes",
        "votes": "votes",
        "vote": "votes",
        "简介": "summary",
        "内容简介": "summary",
        "summary": "summary",
        "description": "summary",
        "内容": "content",
        "content": "content",
        "链接": "url",
        "网址": "url",
        "url": "url",
    }

    for keyword, canonical in keyword_mapping.items():
        if keyword in task_lower:
            requested.add(canonical)

    return requested or None


def _prepare_extracted_item(item: dict, requested_fields: Optional[set[str]] = None) -> dict:
    if not isinstance(item, dict):
        return {}

    normalized: dict = {}

    for key, value in item.items():
        key_str = str(key)
        if key_str.startswith("_"):
            continue
        if key_str in {"element_id", "confidence", "click_point"}:
            continue

        canonical_key = _canonical_field_name(key_str)
        if not canonical_key:
            continue

        existing = normalized.get(canonical_key)
        # 同字段冲突时优先保留更完整的文本
        if existing is None:
            normalized[canonical_key] = value
        elif isinstance(value, str) and isinstance(existing, str):
            if len(value.strip()) > len(existing.strip()):
                normalized[canonical_key] = value

    # name/title 归一：最终只保留 title
    if "title" in normalized and isinstance(normalized["title"], str):
        normalized["title"] = normalized["title"].strip()

    if requested_fields:
        filtered = {k: v for k, v in normalized.items() if k in requested_fields}
        if filtered:
            return filtered

    return normalized


def _build_extracted_item_key(item: dict) -> str:
    """构建去重键，优先 URL，其次标题+作者/评分。"""
    if not isinstance(item, dict):
        return ""

    def _norm(value: Any) -> str:
        return str(value or "").strip().lower()

    url_value = _norm(item.get("url") or item.get("link") or item.get("_url"))
    if url_value:
        return f"url::{url_value}"

    title_value = _norm(item.get("title") or item.get("name") or item.get("movie_name") or item.get("书名") or item.get("片名"))
    author_value = _norm(item.get("author") or item.get("director") or item.get("导演"))
    rating_value = _norm(item.get("rating") or item.get("score") or item.get("评分"))

    if title_value:
        return f"title::{title_value}|author::{author_value}|rating::{rating_value}"

    return ""


def _looks_like_steel_target_url(url: Optional[str]) -> bool:
    text = str(url or "").strip().lower()
    if not text:
        return False
    markers = [
        "vision.lg.china-yongfeng.com",
        "packing-tape",
        "#/history",
        "/history",
    ]
    return any(marker in text for marker in markers)


def _resolve_steel_target_and_auth(
    *,
    task: str,
    target_url: Optional[str] = None,
    auth_data_file: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    resolved_auth_data = _resolve_auth_data_file(
        auth_data_file=auth_data_file or _extract_auth_data_path_from_task(task),
        target_url=target_url,
    )
    resolved_target_url = _extract_target_url(task, target_url)
    if not resolved_target_url:
        resolved_target_url = _extract_target_url_from_auth_data(
            auth_data_file=resolved_auth_data,
            fallback_url=target_url,
        )
    return resolved_target_url, resolved_auth_data


def _is_steel_inspection_task(
    task: str,
    *,
    target_url: Optional[str] = None,
    auth_data_file: Optional[str] = None,
) -> bool:
    resolved_target_url, _resolved_auth_data = _resolve_steel_target_and_auth(
        task=task,
        target_url=target_url,
        auth_data_file=auth_data_file,
    )
    if _looks_like_steel_target_url(resolved_target_url):
        return True

    task_lower = (task or "").lower()
    domain_keywords = ["钢铁", "打包", "打包带", "检验", "检验系统"]
    operation_keywords = ["数据导出", "图片下载", "打包状态", "历史记录", "异常数据", "带图excel", "原始图片", "渲染图片"]
    has_domain = any(keyword in task_lower for keyword in domain_keywords)
    has_operation = any(keyword in task_lower for keyword in operation_keywords)
    return has_domain and has_operation


def _extract_date_range(task: str) -> tuple[str, str]:
    task_text = str(task or "").strip()
    today = dt.date.today().strftime("%Y-%m-%d")
    default_range = (f"{today} 00:00:00", f"{today} 23:59:59")
    if not task_text:
        return default_range

    def _has_time_hint(fragment: str) -> bool:
        text = str(fragment or "").lower()
        hints = [":", "点", "时", "分", "秒", "am", "pm", "a.m", "p.m", "上午", "下午", "中午", "凌晨", "早上", "晚上", "傍晚"]
        return any(hint in text for hint in hints)

    def _to_naive_datetime(value: Any) -> Optional[dt.datetime]:
        if isinstance(value, dt.datetime):
            if value.tzinfo is not None:
                value = value.replace(tzinfo=None)
            return value.replace(microsecond=0)
        if isinstance(value, dt.date):
            return dt.datetime(value.year, value.month, value.day, 0, 0, 0)
        return None

    def _format_bound(value: dt.datetime, *, is_end: bool, fragment: str) -> str:
        if _has_time_hint(fragment):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        day_text = value.strftime("%Y-%m-%d")
        return f"{day_text} {'23:59:59' if is_end else '00:00:00'}"

    candidates: list[tuple[str, dt.datetime]] = []
    try:
        from dateparser.search import search_dates

        settings = {
            "DATE_ORDER": "YMD",
            "PREFER_LOCALE_DATE_ORDER": False,
            "PREFER_DATES_FROM": "current_period",
            "TIMEZONE": "Asia/Shanghai",
            "RETURN_AS_TIMEZONE_AWARE": False,
            "RELATIVE_BASE": dt.datetime.now(),
        }
        search_result = search_dates(task_text, languages=["zh", "en"], settings=settings)
        if search_result:
            for item in search_result:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                fragment = str(item[0] or "").strip()
                parsed_dt = _to_naive_datetime(item[1])
                if not fragment or parsed_dt is None:
                    continue
                if parsed_dt.year < 2000 or parsed_dt.year > 2100:
                    continue
                candidates.append((fragment, parsed_dt))
    except Exception as exc:
        logger.debug(f"dateparser search_dates unavailable or failed: {exc}")

    deduped: list[tuple[str, dt.datetime]] = []
    seen: set[tuple[str, str]] = set()
    for fragment, parsed_dt in candidates:
        key = (fragment, parsed_dt.strftime("%Y-%m-%d %H:%M:%S"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((fragment, parsed_dt))

    # 轻量兜底：未安装 dateparser 或抽取失败时，处理“日期 + 到/至 + 时间”类表达
    def _normalize_iso(value: str) -> str:
        parts = value.replace("/", "-").split("-")
        if len(parts) != 3:
            return today
        try:
            year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
            return f"{year:04d}-{month:02d}-{day:02d}"
        except Exception:
            return today

    def _extract_iso_from_fragment(fragment: str) -> Optional[str]:
        match = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", fragment or "")
        if not match:
            return None
        return _normalize_iso(match.group(0))

    cn_digit_map = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }

    def _cn_to_int(text: str) -> Optional[int]:
        token = str(text or "").strip().replace("兩", "两")
        if not token:
            return None
        if token.isdigit():
            return int(token)
        if token == "十":
            return 10
        if "十" in token:
            left, right = token.split("十", 1)
            left_value = 1 if left == "" else cn_digit_map.get(left)
            if left_value is None:
                return None
            right_value = 0 if right == "" else cn_digit_map.get(right)
            if right_value is None:
                return None
            return left_value * 10 + right_value
        return cn_digit_map.get(token)

    def _parse_light_time(fragment: str, *, is_end: bool) -> str:
        raw = str(fragment or "").lower().replace("：", ":")
        raw = re.sub(r"\s+", "", raw)

        # 先去掉日期部分，避免把日期数字误识别为小时（例如 2026-02-11 0点）
        raw = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", "", raw)
        raw = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", "", raw)

        explicit_time_hint = any(
            token in raw
            for token in [":", "点", "时", "am", "pm", "a.m", "p.m", "上午", "下午", "中午", "凌晨", "早上", "晚上", "傍晚"]
        )
        if not explicit_time_hint:
            return "23:59:59" if is_end else "00:00:00"

        period = None
        if any(token in raw for token in ["下午", "晚上", "傍晚", "pm", "p.m"]):
            period = "pm"
        elif any(token in raw for token in ["上午", "早上", "凌晨", "am", "a.m"]):
            period = "am"
        elif "中午" in raw:
            period = "noon"

        colon = re.search(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", raw)
        if colon:
            hour = int(colon.group(1) or 0)
            minute = int(colon.group(2) or 0)
            second = int(colon.group(3) or 0)
        else:
            zh_time = re.search(
                r"(?<!\d)([零〇一二两三四五六七八九十\d]{1,2})[点时](半|([零〇一二两三四五六七八九十\d]{1,2})分?)?(?:([零〇一二两三四五六七八九十\d]{1,2})秒?)?",
                raw,
            )
            if zh_time:
                hour = _cn_to_int(zh_time.group(1) or "") or 0
                minute = 30 if zh_time.group(2) == "半" else (_cn_to_int(zh_time.group(3) or "") or 0)
                second = _cn_to_int(zh_time.group(4) or "") or 0
            else:
                return "23:59:59" if is_end else "00:00:00"

        if period == "pm" and 1 <= hour <= 11:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        elif period == "noon" and 1 <= hour <= 11:
            hour += 12

        hour = max(0, min(23, hour))
        minute = max(0, min(59, minute))
        second = max(0, min(59, second))
        return f"{hour:02d}:{minute:02d}:{second:02d}"

    range_separators = ["到", "至", "~", "～", " to "]
    lowered_text = f" {task_text.lower()} "
    for sep in range_separators:
        if sep not in lowered_text:
            continue
        if sep.strip() in {"到", "至", "~", "～"}:
            left_part, right_part = task_text.split(sep.strip(), 1)
        else:
            left_part, right_part = lowered_text.split(sep, 1)
        left_date = _extract_iso_from_fragment(left_part)
        right_date = _extract_iso_from_fragment(right_part)
        base_date = left_date or right_date
        if not base_date:
            continue

        start_day = left_date or base_date
        end_day = right_date or base_date
        start_time = _parse_light_time(left_part, is_end=False)
        end_time = _parse_light_time(right_part, is_end=True)
        return f"{start_day} {start_time}", f"{end_day} {end_time}"

    if len(deduped) >= 2:
        start_fragment, start_dt = deduped[0]
        end_fragment, end_dt = deduped[1]
        return (
            _format_bound(start_dt, is_end=False, fragment=start_fragment),
            _format_bound(end_dt, is_end=True, fragment=end_fragment),
        )

    if len(deduped) == 1:
        only_fragment, only_dt = deduped[0]
        if _has_time_hint(only_fragment):
            exact = only_dt.strftime("%Y-%m-%d %H:%M:%S")
            return exact, exact
        day_text = only_dt.strftime("%Y-%m-%d")
        return f"{day_text} 00:00:00", f"{day_text} 23:59:59"

    # 兜底：仅提取标准 YYYY-MM-DD 片段（避免复杂正则规则）
    iso_tokens = re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", task_text)
    if iso_tokens:
        start_day = _normalize_iso(iso_tokens[0])
        end_day = _normalize_iso(iso_tokens[1] if len(iso_tokens) > 1 else iso_tokens[0])
        return f"{start_day} 00:00:00", f"{end_day} 23:59:59"

    return default_range


def _extract_status_filter_value(task: str) -> str:
    task_text = str(task or "")
    task_lower = task_text.lower()
    task_compact = re.sub(r"\s+", "", task_lower)

    if any(
        marker in task_compact
        for marker in [
            "不筛选状态",
            "不筛选",
            "无需筛选",
            "不按状态筛选",
            "no_filter",
            "nofilter",
            "withoutstatusfilter",
        ]
    ):
        return ""

    invalid_tokens = {
        "按钮",
        "旁边",
        "旁边的按钮",
        "筛选",
        "过滤",
        "状态",
        "打包状态",
        "记录",
        "数据",
        "导出",
        "excel",
        "xlsx",
        "download",
        "导出excel",
        "导出xlsx",
    }

    def _clean_status_token(value: str) -> str:
        token = str(value or "").strip().strip("\"'“”")
        token = re.sub(r"[，。；,;].*$", "", token).strip()
        token = re.sub(r"^(?:为|是|状态|打包状态|筛选|过滤|选择)+", "", token, flags=re.IGNORECASE).strip()
        token = re.sub(r"(?:按钮|记录|数据|结果|后|然后|并且|并|导出|下载|excel|xlsx).*$", "", token, flags=re.IGNORECASE).strip()
        return token

    explicit_patterns = [
        r"(?:打包状态|状态|status)\s*(?:为|是|=|:|：|选择|筛选为|筛选)?\s*[\"'“”]?([^\s，。；,;\"'“”]+)",
        r"(?:筛选|过滤)\s*(?:打包状态|状态|status)?\s*(?:为|是|=|:|：)?\s*[\"'“”]?([^\s，。；,;\"'“”]+)",
    ]
    for pattern in explicit_patterns:
        match = re.search(pattern, task_text, re.IGNORECASE)
        if not match:
            continue
        token = _clean_status_token(match.group(1))
        if token and token.lower() not in invalid_tokens:
            return token

    if any(keyword in task_lower for keyword in ["异常", "abnormal", "exception"]):
        return "异常"
    if any(keyword in task_lower for keyword in ["正常", "normal"]):
        return "正常"
    if any(keyword in task_lower for keyword in ["全部", "all"]):
        return "全部"

    if "状态" in task_text or "status" in task_lower:
        loose = re.search(r"(?:状态|status).{0,12}?([\u4e00-\u9fa5a-zA-Z0-9_-]{1,12})", task_text, re.IGNORECASE)
        if loose:
            token = _clean_status_token(loose.group(1))
            if token and token.lower() not in {"status", "state", *invalid_tokens}:
                return token

    return ""


def _item_matches_status_filter(item: Any, status_filter: str) -> bool:
    token = str(status_filter or "").strip()
    if not token:
        return True

    token_lower = token.lower()
    if token_lower in {"全部", "all"}:
        return True

    status_text = ""
    if isinstance(item, dict):
        status_text = " ".join(
            str(item.get(key) or "")
            for key in ["status", "state", "result", "pack_status", "review_status", "label"]
        )
    if not status_text:
        status_text = str(item or "")

    status_lower = status_text.lower()

    if token_lower in {"异常", "abnormal", "exception", "error"}:
        return any(marker in status_lower for marker in ["异常", "❌", "abnormal", "exception", "error", "failed"])

    if token_lower in {"正常", "normal", "success", "ok"}:
        has_normal = any(marker in status_lower for marker in ["正常", "normal", "success", "ok", "✅"])
        has_abnormal = any(marker in status_lower for marker in ["异常", "❌", "abnormal", "exception", "error", "failed"])
        return has_normal and not has_abnormal

    return token_lower in status_lower


def _filter_items_by_status(items: list[Any], status_filter: str) -> list[Any]:
    if not status_filter:
        return list(items or [])
    return [item for item in (items or []) if _item_matches_status_filter(item, status_filter)]


def _extract_image_mode(task: str) -> tuple[str, list[str], str]:
    modes = _extract_image_modes(task)
    first = modes[0] if modes else _image_mode_spec("original")
    return (
        str(first.get("mode_key") or "original"),
        list(first.get("labels") or ["原始图片"]),
        str(first.get("column_name") or "原始图片"),
    )


def _format_datetime_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                parsed = parsed.replace(hour=0, minute=0, second=0)
            return parsed.strftime("%Y-%m-%d-%H-%M-%S")
        except Exception:
            continue

    compact = re.sub(r"[^0-9]+", "-", text).strip("-")
    if compact:
        return compact
    return "unknown"


def _build_steel_output_dir_name(start_date: str, end_date: str) -> str:
    start_token = _format_datetime_token(start_date)
    end_token = _format_datetime_token(end_date)
    return f"{start_token}_{end_token}"


def _build_embed_excel_suffix(image_modes: list[dict[str, Any]]) -> str:
    keys = {
        str(item.get("mode_key") or "").strip().lower()
        for item in (image_modes or [])
        if isinstance(item, dict)
    }
    has_original = "original" in keys
    has_rendered = "rendered" in keys

    if has_original and has_rendered:
        return "_orgin_render"
    if has_rendered:
        return "_render"
    if has_original:
        return "_orgin"
    return "_images"


def _sanitize_download_filename(filename: str) -> str:
    name = os.path.basename(str(filename or "").strip())
    if not name:
        return ""
    return re.sub(r"[<>:\"/\\\\|?*]+", "_", name).strip(" .")


def _resolve_download_save_path(
    *,
    save_path: str,
    suggested_filename: Optional[str],
    preserve_download_filename: bool = False,
) -> str:
    if not preserve_download_filename:
        return save_path
    sanitized = _sanitize_download_filename(str(suggested_filename or ""))
    if not sanitized:
        return save_path
    parent = os.path.dirname(save_path) or "."
    return os.path.join(parent, sanitized)


def _sanitize_mode_key(label: str, index: int = 0) -> str:
    raw = str(label or "").strip().lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fa5]+", "_", raw).strip("_")
    if not slug:
        slug = f"mode_{index}"
    return slug[:40]


def _make_custom_mode_spec(label: str, index: int = 0) -> dict[str, Any]:
    clean_label = str(label or "").strip() or f"图片_{index + 1}"
    return {
        "mode_key": f"custom_{_sanitize_mode_key(clean_label, index)}",
        "labels": [clean_label],
        "column_name": clean_label,
    }


def _image_mode_spec(mode_key: str) -> dict[str, Any]:
    key = (mode_key or "").strip().lower()
    if key == "all":
        return {
            "mode_key": "all",
            "labels": ["\u5168\u90e8\u56fe\u7247", "all images", "both images"],
            "column_name": "\u5168\u90e8\u56fe\u7247",
        }
    if key == "rendered":
        return {
            "mode_key": "rendered",
            "labels": ["渲染图片", "渲染图", "效果图", "标注图片", "处理图片"],
            "column_name": "渲染图片",
        }
    return {
        "mode_key": "original",
        "labels": ["原始图片", "原图", "原始图"],
        "column_name": "原始图片",
    }


def _is_all_images_mode_token(value: str) -> bool:
    token_raw = str(value or "").strip()
    if not token_raw:
        return False
    token = token_raw.lower()
    compact = re.sub(r"[\s_\-/:,;|]+", "", token)

    if compact in {
        "all",
        "both",
        "dual",
        "allimages",
        "bothimages",
        "allpics",
        "allpictures",
        "\u5168\u90e8",
        "\u6240\u6709",
        "\u5168\u90e8\u56fe\u7247",
        "\u6240\u6709\u56fe\u7247",
        "\u5168\u90e8\u56fe\u50cf",
    }:
        return True

    has_original = any(marker in token for marker in ["original", "origin", "raw", "\u539f\u59cb", "\u539f\u56fe"])
    has_rendered = any(marker in token for marker in ["render", "rendered", "annotated", "processed", "\u6e32\u67d3", "\u6548\u679c", "\u6807\u6ce8"])
    return has_original and has_rendered


def _normalize_image_mode_key(value: str) -> Optional[str]:
    token = str(value or "").strip().lower()
    if not token:
        return None
    if _is_all_images_mode_token(token):
        return "all"
    if token in {"original", "origin", "raw", "原始", "原始图片", "原图", "原始图"}:
        return "original"
    if token in {
        "rendered",
        "render",
        "processed",
        "annotated",
        "annotation",
        "annotated image",
        "渲染",
        "渲染图片",
        "渲染图",
        "效果图",
        "标注图片",
        "标注图",
        "处理图片",
    }:
        return "rendered"
    return None


def _expand_image_mode_specs(mode_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def _append_mode(mode_spec: dict[str, Any]) -> None:
        key = str(mode_spec.get("mode_key") or "").strip().lower()
        if key and key in seen_keys:
            return
        if key:
            seen_keys.add(key)
        expanded.append(mode_spec)

    for spec in (mode_specs or []):
        if not isinstance(spec, dict):
            continue
        mode_key = str(spec.get("mode_key") or "").strip().lower()
        labels = spec.get("labels") if isinstance(spec.get("labels"), list) else []
        column_name = str(spec.get("column_name") or "").strip()
        sample_text = " ".join([mode_key, column_name, *[str(item or "") for item in labels]])

        if mode_key == "all" or _is_all_images_mode_token(sample_text):
            _append_mode(_image_mode_spec("original"))
            _append_mode(_image_mode_spec("rendered"))
            continue

        _append_mode(spec)

    return expanded


def _clean_image_mode_label(label: str) -> str:
    value = str(label or "").strip().strip("\"'“”()（）[]")
    if not value:
        return ""

    value = re.sub(r"^(?:请|再)?\s*(?:点击|选择|选中|勾选|下载|导出|获取)\s*", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^(?:the\s+)?(?:image|images?)\s*(?:type|types|mode|modes)?\s*(?:is|are|as|to|=|:)?\s*", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"(?:下载|导出|解压|运行|生成).*?$", "", value, flags=re.IGNORECASE).strip()
    value = value.strip("：:,- ")
    return value


def _extract_image_modes(task: str) -> list[dict[str, Any]]:
    task_text = str(task or "")
    task_lower = task_text.lower()

    candidate_labels: list[str] = []

    scoped_patterns = [
        r"(?:图片下载|下载图片)[^。；\n]*?(?:选择|选中|勾选)([^。；\n]+)",
        r"(?:选择|选中|勾选)([^。；\n]+?)(?:图片|图像)",
        r"(?:images?)\s*(?:type|types|mode|modes)?\s*(?:as|to|=|:)?\s*([^.;\n]+)",
    ]
    for pattern in scoped_patterns:
        for match in re.finditer(pattern, task_text, re.IGNORECASE):
            captured = str(match.group(1) or "").strip()
            if captured:
                candidate_labels.append(captured)

    if not candidate_labels:
        candidate_labels.append(task_text)

    splitter = re.compile(r"(?:、|,|，|/|\\|\band\b|和|及|与|并且|并)", re.IGNORECASE)
    skip_tokens = {
        "图片", "图像", "类型", "模式", "选择", "选中", "勾选", "下载", "图片下载", "image", "images", "mode", "modes", "type", "types",
    }

    parsed_specs: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_labels: set[str] = set()

    for chunk in candidate_labels:
        for segment in splitter.split(chunk):
            label = _clean_image_mode_label(segment)
            if not label:
                continue

            normalized_label = label.lower()
            if normalized_label in skip_tokens or len(normalized_label) <= 1:
                continue
            if normalized_label in seen_labels:
                continue
            seen_labels.add(normalized_label)

            normalized_key = _normalize_image_mode_key(label)
            if normalized_key:
                if normalized_key == "all":
                    for dual_key in ["original", "rendered"]:
                        if dual_key in seen_keys:
                            continue
                        seen_keys.add(dual_key)
                        parsed_specs.append(_image_mode_spec(dual_key))
                    continue
                if normalized_key in seen_keys:
                    continue
                seen_keys.add(normalized_key)
                parsed_specs.append(_image_mode_spec(normalized_key))
                continue

            if any(marker in normalized_label for marker in ["图", "图片", "image", "img", "render", "origin", "raw", "annotat", "thumb"]):
                custom_spec = _make_custom_mode_spec(label, len(parsed_specs))
                custom_key = str(custom_spec.get("mode_key") or "")
                if custom_key in seen_keys:
                    continue
                seen_keys.add(custom_key)
                parsed_specs.append(custom_spec)

    if parsed_specs:
        return _expand_image_mode_specs(parsed_specs)

    if _is_all_images_mode_token(task_text):
        return [_image_mode_spec("original"), _image_mode_spec("rendered")]

    if any(keyword in task_lower for keyword in ["渲染图片", "渲染图", "效果图", "标注图片", "处理图片", "rendered"]):
        return [_image_mode_spec("rendered")]
    if any(keyword in task_lower for keyword in ["原始图片", "原图", "原始图", "original"]):
        return [_image_mode_spec("original")]

    return _expand_image_mode_specs([_image_mode_spec("original")])


def _merge_image_modes_with_vlm(
    *,
    task: str,
    vlm_intent: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    fallback = _expand_image_mode_specs(_extract_image_modes(task))
    if not isinstance(vlm_intent, dict):
        return fallback

    candidate_values: list[str] = []
    downloads = vlm_intent.get("downloads")
    if isinstance(downloads, dict):
        images_value = downloads.get("images")
        if isinstance(images_value, list):
            candidate_values.extend(str(item) for item in images_value)
        elif isinstance(images_value, str):
            candidate_values.append(images_value)

    image_modes_value = vlm_intent.get("image_modes")
    if isinstance(image_modes_value, list):
        candidate_values.extend(str(item) for item in image_modes_value)
    elif isinstance(image_modes_value, str):
        candidate_values.append(image_modes_value)

    merged_modes: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_labels: set[str] = set()
    for idx, value in enumerate(candidate_values):
        token = str(value or "").strip()
        if not token:
            continue
        normalized_key = _normalize_image_mode_key(token)
        if normalized_key:
            if normalized_key in seen_keys:
                continue
            seen_keys.add(normalized_key)
            merged_modes.append(_image_mode_spec(normalized_key))
            continue

        normalized_label = token.lower()
        if normalized_label in seen_labels:
            continue
        seen_labels.add(normalized_label)
        custom_spec = _make_custom_mode_spec(token, idx)
        mode_key = str(custom_spec.get("mode_key") or "")
        if mode_key in seen_keys:
            continue
        seen_keys.add(mode_key)
        merged_modes.append(custom_spec)

    if not merged_modes:
        return fallback

    fallback_by_key = {
        str(item.get("mode_key") or "").strip(): item
        for item in fallback
        if isinstance(item, dict) and str(item.get("mode_key") or "").strip()
    }
    merged_by_key = {
        str(item.get("mode_key") or "").strip(): item
        for item in merged_modes
        if isinstance(item, dict) and str(item.get("mode_key") or "").strip()
    }

    combined: list[dict[str, Any]] = list(merged_modes)
    for mode_key, mode_spec in fallback_by_key.items():
        if mode_key and mode_key not in merged_by_key:
            combined.append(mode_spec)
    return _expand_image_mode_specs(combined)


def _safe_extract_steel_task_intent_via_vlm(task: str, target_url: Optional[str]) -> Optional[dict[str, Any]]:
    try:
        vlm = VLMService()
        parsed, _raw = vlm.extract_steel_task_intent(task=task, target_url=target_url)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        logger.warning(f"Failed to parse steel task intent via VLM, fallback to regex rules: {exc}")
        return None


def _safe_choose_dom_ids_via_vlm(
    *,
    task: str,
    objective: str,
    elements: list[dict],
    top_k: int = 3,
) -> list[str]:
    try:
        vlm = VLMService()
        ids, _raw = vlm.choose_dom_element_ids(
            task=task,
            objective=objective,
            elements=elements,
            top_k=top_k,
        )
    except Exception as exc:
        logger.warning(f"Failed to choose DOM ids via VLM, objective={objective}: {exc}")
        return []

    if not isinstance(ids, list):
        return []
    return [str(item).strip() for item in ids if str(item or "").strip()]


def _safe_next_action_via_vlm(
    *,
    task: str,
    objective: str,
    page_state: dict[str, Any],
    elements: list[dict],
    allowed_actions: Optional[list[str]] = None,
) -> dict[str, Any]:
    try:
        vlm = VLMService()
        parsed, _raw = vlm.next_action_from_page_state(
            task=task,
            objective=objective,
            page_state=page_state,
            elements=elements,
            allowed_actions=allowed_actions or ["click_element", "wait", "noop"],
        )
        return parsed if isinstance(parsed, dict) else {"action": "noop"}
    except Exception as exc:
        logger.warning(f"Failed to get next action via VLM, objective={objective}: {exc}")
        return {"action": "noop"}


@dataclass
class SteelTaskIntent:
    start_date: str
    end_date: str
    status_filter: str
    image_mode_key: str
    image_mode_labels: list[str]
    image_column_name: str
    image_modes: list[dict[str, Any]]
    download_images_enabled: bool = True
    embed_images_to_excel: bool = True
    source: str = "rules"


def _build_steel_task_intent(
    *,
    task: str,
    target_url: Optional[str] = None,
    start_date_override: Optional[str] = None,
    end_date_override: Optional[str] = None,
) -> SteelTaskIntent:
    vlm_intent = _safe_extract_steel_task_intent_via_vlm(task=task, target_url=target_url)

    def _parse_optional_bool(value: Any) -> tuple[bool, bool]:
        if isinstance(value, bool):
            return True, bool(value)
        if isinstance(value, (int, float)):
            return True, bool(value)
        if isinstance(value, str):
            token = value.strip().lower()
            if token in {"true", "1", "yes", "y", "on", "是", "需要", "启用", "开启"}:
                return True, True
            if token in {"false", "0", "no", "n", "off", "否", "不要", "不需要", "禁用", "关闭"}:
                return True, False
        return False, False

    def _is_null_token(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip().lower() in {"", "none", "null", "nil", "skip", "no_filter", "false"}
        return False

    def _build_mode_spec_list(mode_payload: Any) -> list[dict[str, Any]]:
        if mode_payload is None:
            return []
        if isinstance(mode_payload, str):
            raw_values = [
                segment.strip()
                for segment in re.split(r"[,\u3001/\|;]|(?:\band\b)|(?:\u548c)|(?:\u4e0e)|(?:\u53ca)", mode_payload, flags=re.IGNORECASE)
                if str(segment or "").strip()
            ] or [mode_payload]
        elif isinstance(mode_payload, list):
            raw_values = [str(item) for item in mode_payload]
        else:
            return []

        mode_specs: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        seen_labels: set[str] = set()

        for idx, raw in enumerate(raw_values):
            label = str(raw or "").strip()
            if not label:
                continue
            norm_key = _normalize_image_mode_key(label)
            if norm_key:
                if norm_key == "all":
                    for dual_key in ["original", "rendered"]:
                        mode_spec = _image_mode_spec(dual_key)
                        key = str(mode_spec.get("mode_key") or "").strip()
                        label_key = str(mode_spec.get("column_name") or "").strip().lower()
                        if key and key in seen_keys:
                            continue
                        if label_key and label_key in seen_labels:
                            continue
                        if key:
                            seen_keys.add(key)
                        if label_key:
                            seen_labels.add(label_key)
                        mode_specs.append(mode_spec)
                    continue
                mode_spec = _image_mode_spec(norm_key)
            else:
                mode_spec = _make_custom_mode_spec(label, idx)
            key = str(mode_spec.get("mode_key") or "").strip()
            label_key = str(mode_spec.get("column_name") or "").strip().lower()
            if key and key in seen_keys:
                continue
            if label_key and label_key in seen_labels:
                continue
            if key:
                seen_keys.add(key)
            if label_key:
                seen_labels.add(label_key)
            mode_specs.append(mode_spec)

        return _expand_image_mode_specs(mode_specs)

    start_date = (start_date_override or "").strip()
    end_date = (end_date_override or "").strip()

    if isinstance(vlm_intent, dict):
        time_range = vlm_intent.get("time_range")
        if isinstance(time_range, dict):
            vlm_start = str(time_range.get("start") or "").strip()
            vlm_end = str(time_range.get("end") or "").strip()
            if vlm_start and not start_date_override:
                start_date = vlm_start
            if vlm_end and not end_date_override:
                end_date = vlm_end

    if not start_date or not end_date:
        parsed_start, parsed_end = _extract_date_range(task)
        if not start_date:
            start_date = parsed_start
        if not end_date:
            end_date = parsed_end

    task_lower = str(task or "").lower()
    task_compact = re.sub(r"\s+", "", task_lower)

    status_filter = ""
    has_status_decision = False

    image_mode_key, image_mode_labels, image_column_name = _extract_image_mode(task)
    image_modes: list[dict[str, Any]] = []
    has_images_decision = False
    download_images_enabled = True

    embed_images_to_excel = True
    has_embed_decision = False

    if isinstance(vlm_intent, dict):
        filters = vlm_intent.get("filters")
        if isinstance(filters, dict):
            has_apply_flag, apply_status_flag = _parse_optional_bool(filters.get("apply_status_filter"))
            raw_status = None
            status_key_present = False
            for key in ["pack_status", "status", "打包状态"]:
                if key in filters:
                    raw_status = filters.get(key)
                    status_key_present = True
                    break

            parsed_status = ""
            has_status_value = False
            if not _is_null_token(raw_status):
                parsed_status = str(raw_status or "").strip()
                has_status_value = bool(parsed_status)

            if has_apply_flag:
                has_status_decision = True
                if apply_status_flag:
                    if has_status_value:
                        status_filter = parsed_status
                    else:
                        has_status_decision = False
                else:
                    status_filter = ""
            elif has_status_value:
                has_status_decision = True
                status_filter = parsed_status
            elif status_key_present and _is_null_token(raw_status):
                has_status_decision = True
                status_filter = ""

        downloads = vlm_intent.get("downloads")
        if isinstance(downloads, dict) and "images" in downloads:
            vlm_image_modes: list[dict[str, Any]] = []
            images_value = downloads.get("images")

            if isinstance(images_value, dict):
                has_enabled_flag, enabled_flag = _parse_optional_bool(images_value.get("enabled"))
                if has_enabled_flag:
                    has_images_decision = True
                    download_images_enabled = enabled_flag

                vlm_image_modes = _build_mode_spec_list(images_value.get("modes"))
                if vlm_image_modes:
                    image_modes = vlm_image_modes
                    if not has_images_decision:
                        has_images_decision = True
                        download_images_enabled = True
                elif "modes" in images_value and has_enabled_flag and not enabled_flag:
                    image_modes = []
            elif isinstance(images_value, list):
                has_images_decision = True
                vlm_image_modes = _build_mode_spec_list(images_value)
                image_modes = vlm_image_modes
                download_images_enabled = len(vlm_image_modes) > 0
            elif isinstance(images_value, str):
                has_images_decision = True
                has_enabled_flag, enabled_flag = _parse_optional_bool(images_value)
                if has_enabled_flag:
                    download_images_enabled = enabled_flag
                else:
                    vlm_image_modes = _build_mode_spec_list(images_value)
                    image_modes = vlm_image_modes
                    download_images_enabled = bool(vlm_image_modes)
            elif isinstance(images_value, bool):
                has_images_decision = True
                download_images_enabled = bool(images_value)

        if isinstance(vlm_intent.get("image_modes"), (list, str)):
            legacy_modes = _build_mode_spec_list(vlm_intent.get("image_modes"))
            if legacy_modes:
                image_modes = legacy_modes
                if not has_images_decision:
                    has_images_decision = True
                    download_images_enabled = True

        output = vlm_intent.get("output")
        if isinstance(output, dict) and "embed_images_to_excel" in output:
            parsed_embed, parsed_embed_value = _parse_optional_bool(output.get("embed_images_to_excel"))
            if parsed_embed:
                has_embed_decision = True
                embed_images_to_excel = parsed_embed_value

    if not has_status_decision:
        status_filter = _extract_status_filter_value(task)
    elif str(status_filter or "").strip() == "":
        # VLM明确要求不筛选状态，保持空
        status_filter = ""

    if has_status_decision and status_filter:
        lowered = status_filter.lower()
        if lowered in {"none", "null", "no_filter", "skip", "false"}:
            status_filter = ""

    if not has_images_decision:
        image_modes = _extract_image_modes(task)
        download_images_enabled = True
        if any(re.search(pattern, task_compact, re.IGNORECASE) for pattern in [
            r"(?:不|别|不要).{0,6}(?:下载|导出|获取)?.{0,6}(?:图片|图像|image|images)",
            r"(?:without|no)\s+(?:images?|pictures?)",
            r"(?:仅|只)(?:导出)?\s*excel",
        ]):
            download_images_enabled = False
    elif download_images_enabled and not image_modes:
        # VLM决定要下载图片但未给出模式，允许从任务文本补全
        image_modes = _extract_image_modes(task)

    if not has_embed_decision:
        embed_images_to_excel = True
        if any(re.search(pattern, task_compact, re.IGNORECASE) for pattern in [
            r"(?:不|别|不要).{0,6}(?:嵌图|带图excel|生成带图excel|embed)",
            r"(?:without|no)\s+(?:embed|embedded)",
        ]):
            embed_images_to_excel = False
        elif any(re.search(pattern, task_compact, re.IGNORECASE) for pattern in [
            r"(?:带图excel|嵌图|embed(?:ded)?)",
            r"(?:生成|输出).{0,6}(?:带图excel|嵌图)",
        ]):
            embed_images_to_excel = True

    if has_images_decision and not download_images_enabled and not has_embed_decision:
        embed_images_to_excel = False

    if embed_images_to_excel:
        download_images_enabled = True

    if download_images_enabled:
        image_modes = _expand_image_mode_specs(image_modes)

    if download_images_enabled and not image_modes:
        image_modes = _extract_image_modes(task)
    if download_images_enabled and not image_modes:
        image_modes = [_image_mode_spec("original")]

    if image_modes:
        first_mode = image_modes[0]
        image_mode_key = str(first_mode.get("mode_key") or image_mode_key)
        image_mode_labels = list(first_mode.get("labels") or image_mode_labels)
        image_column_name = str(first_mode.get("column_name") or image_column_name)

    if not download_images_enabled:
        image_modes = []
        embed_images_to_excel = False

    return SteelTaskIntent(
        start_date=start_date,
        end_date=end_date,
        status_filter=status_filter,
        image_mode_key=image_mode_key,
        image_mode_labels=image_mode_labels,
        image_column_name=image_column_name,
        image_modes=image_modes,
        download_images_enabled=download_images_enabled,
        embed_images_to_excel=embed_images_to_excel,
        source="vlm" if isinstance(vlm_intent, dict) else "rules",
    )


def _extract_target_url(task: str, fallback: Optional[str] = None) -> Optional[str]:
    if fallback:
        return fallback
    match = re.search(r"https?://[^\s，。]+", task or "")
    if match:
        return match.group(0)
    env_url = os.getenv("STEEL_TARGET_URL", "").strip()
    return env_url or None


def _resolve_auth_data_file(auth_data_file: Optional[str] = None, target_url: Optional[str] = None) -> Optional[str]:
    project_root = Path(__file__).resolve().parent.parent

    def _resolve_path(candidate: str) -> Path:
        candidate_path = Path(candidate)
        if not candidate_path.is_absolute():
            candidate_path = project_root / candidate_path
        return candidate_path

    explicit_candidate = (auth_data_file or "").strip() or os.getenv("STEEL_AUTH_DATA_FILE", "").strip()
    if explicit_candidate:
        return str(_resolve_path(explicit_candidate))

    cookies_dir = project_root / "cookies"
    if not cookies_dir.exists() or not cookies_dir.is_dir():
        return None

    host = (urlparse(target_url).hostname or "").strip().lower() if target_url else ""
    candidates: list[Path] = []

    if host:
        for pattern in [f"auth_data_{host}*.json", f"cookies_{host}*.json", f"*{host}*.json"]:
            candidates.extend(cookies_dir.glob(pattern))

    if not candidates:
        candidates.extend(cookies_dir.glob("auth_data_*.json"))

    existing = [p for p in candidates if p.exists() and p.is_file()]
    if not existing:
        return None

    latest = max(existing, key=lambda p: p.stat().st_mtime)
    return str(latest)


def _extract_target_url_from_auth_data(auth_data_file: Optional[str] = None, fallback_url: Optional[str] = None) -> Optional[str]:
    resolved_path = _resolve_auth_data_file(auth_data_file=auth_data_file, target_url=fallback_url)
    if not resolved_path or not os.path.exists(resolved_path):
        return None

    try:
        with open(resolved_path, "r", encoding="utf-8") as f:
            auth_data = json.load(f)
        auth_url = (auth_data.get("url") or "").strip()
        return auth_url or None
    except Exception as exc:
        logger.warning(f"Failed to read target url from auth_data file {resolved_path}: {exc}")
        return None


def _extract_auth_data_path_from_task(task: str) -> Optional[str]:
    match = re.search(r"auth[_-]?data[^\s\"']*\.json", task or "", flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0)


def _parse_cookie_header(cookie_header: str, target_url: str) -> list[dict]:
    parsed = urlparse(target_url)
    host = parsed.hostname
    secure = (parsed.scheme or "").lower() == "https"
    if not host:
        return []

    cookies: list[dict] = []
    for chunk in cookie_header.split(";"):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": value.strip(),
                "domain": host,
                "path": "/",
                "secure": secure,
                "httpOnly": False,
                "sameSite": "Lax",
            }
        )
    return cookies


async def _apply_auth_data_if_available(target_url: str, auth_data_file: Optional[str] = None) -> Optional[str]:
    resolved_path = _resolve_auth_data_file(auth_data_file=auth_data_file, target_url=target_url)
    if not resolved_path:
        return None

    if not os.path.exists(resolved_path):
        raise FileNotFoundError(f"auth_data 文件不存在: {resolved_path}")

    with open(resolved_path, "r", encoding="utf-8") as f:
        auth_data = json.load(f)

    await executor._ensure_page()
    context = executor._context
    page = executor._page

    raw_cookies = auth_data.get("cookies")
    cookies_to_add: list[dict] = []
    if isinstance(raw_cookies, list):
        cookies_to_add = raw_cookies
    elif isinstance(raw_cookies, str) and raw_cookies.strip():
        cookies_to_add = _parse_cookie_header(raw_cookies, target_url)

    if cookies_to_add:
        await context.add_cookies(cookies_to_add)

    local_storage = auth_data.get("localStorage") if isinstance(auth_data.get("localStorage"), dict) else {}
    session_storage = auth_data.get("sessionStorage") if isinstance(auth_data.get("sessionStorage"), dict) else {}

    if local_storage or session_storage:
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        history_url = _build_history_url(target_url)
        candidate_urls = [origin, target_url, history_url]
        visited: set[str] = set()
        storage_applied = False

        for candidate_url in candidate_urls:
            normalized_candidate = str(candidate_url or "").strip()
            if not normalized_candidate or normalized_candidate in visited:
                continue
            visited.add(normalized_candidate)

            try:
                await page.goto(normalized_candidate)
                await page.wait_for_load_state("domcontentloaded")
                await page.evaluate(
                    """
                    (payload) => {
                      const localData = payload.localData || {};
                      const sessionData = payload.sessionData || {};
                      for (const [k, v] of Object.entries(localData)) {
                        localStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v));
                      }
                      for (const [k, v] of Object.entries(sessionData)) {
                        sessionStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v));
                      }
                    }
                    """,
                    {"localData": local_storage, "sessionData": session_storage},
                )
                storage_applied = True
                break
            except Exception as exc:
                logger.warning(f"Failed to open auth storage origin {normalized_candidate}: {exc}")

        if not storage_applied:
            logger.warning("Failed to apply localStorage/sessionStorage; continue with cookies-only auth")

    return resolved_path


def _extract_int_from_task(task: str, patterns: list[str], default: int, minimum: int = 1, maximum: int = 500) -> int:
    task_lower = (task or "").lower()
    for pattern in patterns:
        match = re.search(pattern, task_lower)
        if match:
            value = int(match.group(1))
            return max(minimum, min(maximum, value))
    return default


def _build_history_url(url: str) -> str:
    normalized = (url or "").strip()
    if not normalized:
        return normalized
    lowered = normalized.lower()
    if lowered.startswith("about:blank") or lowered.startswith("data:") or lowered.startswith("blob:"):
        return ""
    parsed = urlparse(normalized)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return ""
    if "#/history" in normalized:
        return normalized
    if "#/" in normalized:
        return re.sub(r"#/.+$", "#/history", normalized)
    if normalized.endswith("/"):
        return f"{normalized}#/history"
    return f"{normalized}/#/history"


async def _ensure_history_page(target_url: str, timeout_ms: int = 15000) -> tuple[str, bool]:
    """确保页面停留在历史记录页（#/history）。"""
    await executor._ensure_page()
    page = executor._page
    try:
        current_url = page.url or ""
    except Exception:
        current_url = ""
    if "#/history" in current_url:
        return current_url, False

    history_url = _build_history_url(target_url or current_url)
    logger.warning(f"Detected non-history route, redirecting to history page: {current_url} -> {history_url}")

    redirected = False
    for attempt in range(2):
        try:
            await page.goto(history_url, wait_until="domcontentloaded", timeout=timeout_ms)
            await executor.wait_for_load(timeout_ms=timeout_ms)
            await executor.wait_for_stable(1200)
            redirected = True
            break
        except Exception as exc:
            logger.warning(f"Redirect to history failed (attempt {attempt + 1}/2): {exc}")
            await asyncio.sleep(0.6)
            await executor._ensure_page()
            page = executor._page
            try:
                current_url = page.url or ""
            except Exception:
                current_url = ""
            if "#/history" in current_url:
                return current_url, True

    try:
        current_url = page.url or ""
    except Exception:
        current_url = history_url
    if "#/history" in current_url:
        return current_url, True

    logger.warning(f"History guard degraded; keep current route for now: {current_url}")
    return current_url, redirected


async def _ensure_active_history_page(target_url: str, timeout_ms: int = 15000) -> tuple[str, bool]:
    """确保执行器当前页是可操作的历史记录页，优先处理 about:blank/新标签页干扰。"""
    await executor._ensure_page()
    switched = False

    try:
        current_url = (executor._page.url or "").strip()
    except Exception:
        current_url = ""

    if (not current_url or current_url.lower().startswith("about:blank")) and getattr(executor, "_context", None):
        try:
            target_host = (urlparse(target_url).hostname or "").lower()
            preferred_page = None
            fallback_page = None
            for candidate in reversed(list(executor._context.pages)):
                try:
                    if candidate.is_closed():
                        continue
                    candidate_url = (candidate.url or "").strip()
                except Exception:
                    continue

                if not candidate_url or candidate_url.lower().startswith("about:blank"):
                    continue
                if "#/history" in candidate_url:
                    preferred_page = candidate
                    break
                if target_host and target_host in candidate_url.lower() and fallback_page is None:
                    fallback_page = candidate

            chosen = preferred_page or fallback_page
            if chosen is not None and chosen is not executor._page:
                executor._page = chosen
                switched = True
                try:
                    await chosen.bring_to_front()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning(f"Failed to switch from about:blank to existing history tab: {exc}")

    ensure_target_url = (target_url or current_url).strip()
    if not ensure_target_url:
        return current_url, switched

    current_url, redirected = await _ensure_history_page(ensure_target_url, timeout_ms=timeout_ms)
    return current_url, (switched or redirected)


async def _cleanup_auxiliary_blank_pages(target_url: Optional[str] = None) -> int:
    """关闭上下文中非当前活动页的 about:blank 页面。"""
    await executor._ensure_page()
    context = getattr(executor, "_context", None)
    active_page = getattr(executor, "_page", None)
    if context is None:
        return 0

    pages = [page for page in list(context.pages) if page is not None]
    active_for_host = active_page

    closed = 0
    for candidate in pages:
        if candidate is None or candidate is active_for_host:
            continue
        try:
            if candidate.is_closed():
                continue
            candidate_url = str(candidate.url or "").strip().lower()
        except Exception:
            continue
        if (not candidate_url) or candidate_url.startswith("about:blank"):
            with contextlib.suppress(Exception):
                await candidate.close()
                closed += 1
    return closed


async def _restore_page_focus_after_download() -> None:
    """下载后恢复页面焦点，降低浏览器下载栏/下载气泡对后续点击的干扰。"""
    # 已解决
    await executor._ensure_page()
    page = executor._page

    async def _click_neutral_blank_area() -> bool:
        with contextlib.suppress(Exception):
            point = await page.evaluate(
                """
                () => {
                  const isInteractive = (el) => {
                    if (!el) return false;
                    const tag = (el.tagName || '').toLowerCase();
                    if (['a', 'button', 'input', 'select', 'textarea', 'label', 'summary'].includes(tag)) return true;
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    if (role.includes('button') || role.includes('menuitem') || role.includes('link')) return true;
                    const cls = (el.className || '').toString().toLowerCase();
                    if (cls.includes('el-button') || cls.includes('el-link') || cls.includes('dropdown')) return true;
                    return false;
                  };

                  const w = Math.max(320, window.innerWidth || 1280);
                  const h = Math.max(240, window.innerHeight || 720);

                  for (let y = Math.min(h - 40, 260); y <= Math.min(h - 30, 620); y += 36) {
                    for (let x = Math.max(220, w - 80); x >= Math.max(220, Math.floor(w * 0.45)); x -= 56) {
                      const el = document.elementFromPoint(x, y);
                      if (!el) continue;
                      if (!isInteractive(el)) return { x, y };
                    }
                  }
                  return null;
                }
                """
            )
            if isinstance(point, dict) and "x" in point and "y" in point:
                await page.mouse.click(int(point["x"]), int(point["y"]))
                return True
        return False

    with contextlib.suppress(Exception):
        await page.bring_to_front()
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
    with contextlib.suppress(Exception):
        await page.evaluate(
            """
            () => {
              try { window.focus(); } catch (_) {}
              try {
                const active = document.activeElement;
                if (active && typeof active.blur === 'function') active.blur();
              } catch (_) {}
              return true;
            }
            """
        )
    clicked_neutral = await _click_neutral_blank_area()
    with contextlib.suppress(Exception):
        if not clicked_neutral:
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            click_x = int(max(100, min((viewport.get("width") or 1280) // 2, 1600)))
            click_y = int(max(120, min((viewport.get("height") or 720) // 2, 900)))
            await page.mouse.click(click_x, click_y)
    with contextlib.suppress(Exception):
        await executor.wait_for_stable(400)


def _is_login_route(url: Optional[str]) -> bool:
    text = (url or "").lower()
    return "#/login" in text or "/login" in text


async def _set_date_inputs(start_date: str, end_date: str) -> None:
    """尽量鲁棒地设置页面上的开始/结束日期输入框。"""
    await executor._ensure_page()
    page = executor._page

    script = """
    (payload) => {
      const startDate = payload.startDate;
      const endDate = payload.endDate;
      const isVisible = (el) => {
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };

      const inFilterBar = (el) => {
        const rect = el.getBoundingClientRect();
        return rect.top >= 50 && rect.top <= 150;
      };

      const formatDateTime = (value, isEnd) => {
        const str = String(value || '');
        if (/\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/.test(str)) return str;
        if (/\d{4}-\d{2}-\d{2}/.test(str)) {
          return `${str} ${isEnd ? '23:59:59' : '00:00:00'}`;
        }
        return str;
      };

      const writeValue = (el, value) => {
        el.focus();
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
        if (setter) {
          setter.call(el, value);
        } else {
          el.value = value;
        }
        el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      };

      const startVal = formatDateTime(startDate, false);
      const endVal = formatDateTime(endDate, true);

      const rangeInputs = Array.from(document.querySelectorAll('input.el-range-input'))
        .filter(el => isVisible(el) && inFilterBar(el));
      if (rangeInputs.length >= 2) {
        writeValue(rangeInputs[0], startVal);
        writeValue(rangeInputs[1], endVal);
        rangeInputs[1].blur();
        return { ok: true, mode: 'el_range_input' };
      }

      const candidates = Array.from(document.querySelectorAll('input'))
        .filter(el => isVisible(el) && inFilterBar(el))
        .filter(el => {
          const attrs = [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), el.type]
            .map(v => (v || '').toLowerCase()).join(' ');
          return /date|日期|start|begin|from|end|to|time|时间/.test(attrs);
        });

      if (candidates.length < 2) {
        return { ok: false, reason: 'date_inputs_not_found', count: candidates.length };
      }

      writeValue(candidates[0], startVal);
      writeValue(candidates[1], endVal);
      candidates[1].blur();
      return { ok: true, mode: 'generic_input' };
    }
    """

    result = await page.evaluate(script, {"startDate": start_date, "endDate": end_date})
    if result.get("ok", False):
        return

    logger.warning(f"Rule-based date input failed, fallback to VLM DOM selection: {result}")

    def _normalize_datetime_input(value: str, is_end: bool) -> str:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", text):
            return text
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return f"{text} {'23:59:59' if is_end else '00:00:00'}"
        return text

    dom_result = await executor.mark_page_elements()
    elements = dom_result.get("elements", []) if isinstance(dom_result, dict) else []
    candidate_ids = _safe_choose_dom_ids_via_vlm(
        task=f"设置时间范围：{start_date} 到 {end_date}",
        objective="选择顶部筛选栏中的开始与结束日期时间输入框，优先日期范围组件，避免点击左侧导航。返回顺序为开始->结束。",
        elements=elements,
        top_k=4,
    )
    if not candidate_ids:
        raise RuntimeError(f"Failed to set date inputs: {result}")

    start_val = _normalize_datetime_input(start_date, False)
    end_val = _normalize_datetime_input(end_date, True)

    async def _type_into_element(element_id: str, text: str) -> bool:
        clicked = await executor.click_element_by_id(element_id)
        if not clicked:
            return False
        await asyncio.sleep(0.12)
        with contextlib.suppress(Exception):
            await executor.press("Control+A")
            await asyncio.sleep(0.05)
            await executor.press("Backspace")
        await executor.type_text(text)
        await asyncio.sleep(0.15)
        return True

    typed_start = False
    typed_end = False
    used_ids: list[str] = []

    if candidate_ids:
        typed_start = await _type_into_element(candidate_ids[0], start_val)
        used_ids.append(candidate_ids[0])

    end_candidates = [item for item in candidate_ids[1:] if item not in used_ids]
    if end_candidates:
        typed_end = await _type_into_element(end_candidates[0], end_val)
    elif typed_start:
        with contextlib.suppress(Exception):
            await executor.press("Tab")
            await asyncio.sleep(0.08)
            with contextlib.suppress(Exception):
                await executor.press("Control+A")
                await executor.press("Backspace")
            await executor.type_text(end_val)
            typed_end = True

    verify = await page.evaluate(
        """
        (payload) => {
          const startVal = String(payload.startVal || '').trim();
          const endVal = String(payload.endVal || '').trim();
          const startDate = startVal.slice(0, 10);
          const endDate = endVal.slice(0, 10);

          const values = Array.from(document.querySelectorAll('input'))
            .map(el => String(el.value || '').trim())
            .filter(Boolean);

          const hasStart = values.some(v => v.includes(startVal) || (startDate && v.includes(startDate)));
          const hasEnd = values.some(v => v.includes(endVal) || (endDate && v.includes(endDate)));
          return { hasStart, hasEnd, sample: values.slice(0, 12), count: values.length };
        }
        """,
        {"startVal": start_val, "endVal": end_val},
    )

    if typed_start and typed_end and isinstance(verify, dict) and verify.get("hasStart") and verify.get("hasEnd"):
        return

    # PageState -> VLM next_action 扩展：在日期输入阶段执行有限轮次单步决策
    for _ in range(4):
        try:
            dom_now = await executor.mark_page_elements()
            elements_now = dom_now.get("elements", []) if isinstance(dom_now, dict) else []
            page_state = await _collect_page_state_summary(max_elements=140, max_rows=20)
            next_action = _safe_next_action_via_vlm(
                task=f"设置时间范围：{start_date} 到 {end_date}",
                objective=f"在顶部筛选区将开始时间设为 {start_val}、结束时间设为 {end_val}。",
                page_state=page_state,
                elements=elements_now,
                allowed_actions=["click_element", "wait", "noop"],
            )
            action_name = str(next_action.get("action") or "noop")
            if action_name == "click_element":
                action_element_id = str(next_action.get("element_id") or "").strip()
                if action_element_id and await executor.click_element_by_id(action_element_id):
                    await asyncio.sleep(0.1)
                    with contextlib.suppress(Exception):
                        await executor.press("Control+A")
                        await executor.press("Backspace")

                    input_text = start_val if not typed_start else end_val
                    await executor.type_text(input_text)
                    await asyncio.sleep(0.15)

                    if not typed_start:
                        typed_start = True
                    elif not typed_end:
                        typed_end = True

                    verify = await page.evaluate(
                        """
                        (payload) => {
                          const startVal = String(payload.startVal || '').trim();
                          const endVal = String(payload.endVal || '').trim();
                          const startDate = startVal.slice(0, 10);
                          const endDate = endVal.slice(0, 10);

                          const values = Array.from(document.querySelectorAll('input'))
                            .map(el => String(el.value || '').trim())
                            .filter(Boolean);

                          const hasStart = values.some(v => v.includes(startVal) || (startDate && v.includes(startDate)));
                          const hasEnd = values.some(v => v.includes(endVal) || (endDate && v.includes(endDate)));
                          return { hasStart, hasEnd, sample: values.slice(0, 12), count: values.length };
                        }
                        """,
                        {"startVal": start_val, "endVal": end_val},
                    )
                    if isinstance(verify, dict) and verify.get("hasStart") and verify.get("hasEnd"):
                        return
                    continue
            elif action_name == "wait":
                wait_ms = int(next_action.get("ms") or 300)
                await asyncio.sleep(max(0.1, min(wait_ms, 3000) / 1000.0))
                continue
            break
        except Exception as exc:
            logger.debug(f"VLM next_action trial failed in date input: {exc}")
            break

    raise RuntimeError(
        f"Failed to set date inputs: {result}; vlm_fallback={{'typed_start': {typed_start}, 'typed_end': {typed_end}, 'verify': {verify}}}"
    )


async def _select_status_filter(status_label: str) -> bool:
    """选择打包状态筛选值，仅在表头筛选与弹层菜单中定位，避免误触左侧菜单。"""
    await executor._ensure_page()
    page = executor._page

    expected_label = (status_label or "").strip()
    if not expected_label:
        return True

    header_cell = page.locator(".el-table__header-wrapper th", has_text="打包状态").first
    header_candidates = [
        header_cell.locator(".column-filter, .el-icon, svg, i").first,
        header_cell,
    ]

    opened = False
    for locator in header_candidates:
        try:
            await locator.wait_for(state="visible", timeout=2500)
            await locator.click(timeout=2500)
            opened = True
            break
        except Exception:
            continue

    if not opened:
        return False

    await asyncio.sleep(0.25)
    option_selectors = [
        page.locator(".el-popper.pop-filter").first.get_by_text(expected_label, exact=True),
        page.locator(".el-popper.pop-filter *", has_text=expected_label).first,
        page.locator(".el-popper .el-dropdown-menu__item", has_text=expected_label).first,
        page.locator(".el-table-filter__list-item", has_text=expected_label).first,
        page.locator(".el-select-dropdown__item", has_text=expected_label).first,
        page.locator(".el-popper li", has_text=expected_label).first,
    ]

    async def _try_click_status_option(timeout_ms: int = 1800) -> bool:
        for locator in option_selectors:
            try:
                await locator.wait_for(state="visible", timeout=timeout_ms)
                await locator.click(timeout=timeout_ms)
                await asyncio.sleep(0.35)
                return True
            except Exception:
                continue

        clicked_in_pop_filter = await page.evaluate(
            """
            (wantedLabel) => {
              const wanted = String(wantedLabel || '').replace(/\s+/g, '');
              if (!wanted) return false;
              const isVisible = (el) => {
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };

              const pop = Array.from(document.querySelectorAll('.el-popper, .el-select-dropdown, .el-table-filter'))
                .find(el => isVisible(el));
              if (!pop) return false;

              const candidates = Array.from(pop.querySelectorAll('button, a, li, div, span, label'));
              for (const el of candidates) {
                if (!isVisible(el)) continue;
                const text = (el.innerText || el.textContent || '').replace(/\s+/g, '');
                if (text === wanted || text.includes(wanted)) {
                  el.click();
                  return true;
                }
              }
              return false;
            }
            """,
            expected_label,
        )
        if clicked_in_pop_filter:
            await asyncio.sleep(0.35)
            return True
        return False

    if await _try_click_status_option(timeout_ms=2500):
        return True

    logger.warning(f"Rule-based status filter selection failed, fallback to VLM DOM selection: {expected_label}")

    with contextlib.suppress(Exception):
        dom_result = await executor.mark_page_elements()
        elements = dom_result.get("elements", []) if isinstance(dom_result, dict) else []
    if 'elements' not in locals():
        elements = []

    trigger_ids = _safe_choose_dom_ids_via_vlm(
        task=f"将打包状态筛选为{expected_label}",
        objective="先点击“打包状态”筛选触发器，位置在表格表头或顶部筛选区；避免左侧菜单与导航链接。",
        elements=elements,
        top_k=5,
    )

    for trigger_id in trigger_ids:
        clicked_trigger = await executor.click_element_by_id(trigger_id)
        if not clicked_trigger:
            continue
        await asyncio.sleep(0.25)

        if await _try_click_status_option(timeout_ms=1200):
            return True

        with contextlib.suppress(Exception):
            dom_after = await executor.mark_page_elements()
            option_ids = _safe_choose_dom_ids_via_vlm(
                task=f"将打包状态筛选为{expected_label}",
                objective=f"在已打开的下拉菜单中点击选项“{expected_label}”。",
                elements=dom_after.get("elements", []) if isinstance(dom_after, dict) else [],
                top_k=5,
            )
            for option_id in option_ids:
                if option_id == trigger_id:
                    continue
                if await executor.click_element_by_id(option_id):
                    await asyncio.sleep(0.35)
                    return True

    # PageState -> VLM next_action 扩展：在筛选阶段进行有限轮次决策尝试
    for _ in range(3):
        try:
            dom_now = await executor.mark_page_elements()
            elements_now = dom_now.get("elements", []) if isinstance(dom_now, dict) else []
            page_state = await _collect_page_state_summary(max_elements=140, max_rows=20)
            next_action = _safe_next_action_via_vlm(
                task=f"将打包状态筛选为{expected_label}",
                objective=f"在当前页面将打包状态设置为“{expected_label}”，如需先打开筛选菜单再点击选项。",
                page_state=page_state,
                elements=elements_now,
                allowed_actions=["click_element", "wait", "noop"],
            )
            action_name = str(next_action.get("action") or "noop")
            if action_name == "click_element":
                action_element_id = str(next_action.get("element_id") or "").strip()
                if action_element_id and await executor.click_element_by_id(action_element_id):
                    await asyncio.sleep(0.25)
                    if await _try_click_status_option(timeout_ms=1000):
                        return True
                    continue
            elif action_name == "wait":
                wait_ms = int(next_action.get("ms") or 300)
                await asyncio.sleep(max(0.1, min(wait_ms, 3000) / 1000.0))
                if await _try_click_status_option(timeout_ms=900):
                    return True
                continue
            break
        except Exception as exc:
            logger.debug(f"VLM next_action trial failed in status filter selection: {exc}")
            break

    return False


async def _click_by_text_and_download(
    text_patterns: list[str],
    save_path: str,
    timeout_ms: int = 45000,
    preserve_download_filename: bool = False,
) -> str:
    """点击文本按钮并捕获下载文件。"""
    await executor._ensure_page()
    page = executor._page

    trigger_script = """
    (patterns) => {
      const isVisible = (el) => {
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };

      const all = Array.from(document.querySelectorAll('button, a, span, div'));
      const lowerPatterns = patterns.map(p => p.toLowerCase());
      const target = all.find(el => {
        const text = (el.innerText || '').toLowerCase().trim();
        if (!text || !isVisible(el)) return false;
        return lowerPatterns.some(p => text.includes(p));
      });

      if (!target) return { ok: false };
      target.click();
      return { ok: true };
    }
    """

    async with page.expect_download(timeout=timeout_ms) as download_info:
        clicked = await page.evaluate(trigger_script, text_patterns)
        if not clicked.get("ok", False):
            raise RuntimeError(f"Failed to click download trigger by text: {text_patterns}")

    download = await download_info.value
    await download.save_as(save_path)
    return save_path


def _unzip_file(zip_path: str, extract_dir: str) -> str:
    return unzip_archive(zip_path, extract_dir)


def _find_picture_root(extract_dir: str) -> Optional[str]:
    return find_picture_root(extract_dir)


async def _collect_row_image_filenames(limit: int = 500) -> list[str]:
    """从当前页面表格行中提取图片文件名，供嵌图映射优先使用。"""
    await executor._ensure_page()
    page = executor._page

    script = """
    (maxCount) => {
      const rows = Array.from(document.querySelectorAll('tr.el-table__row'));
      const names = [];

      const normalizeName = (src) => {
        try {
          const urlObj = new URL(src, window.location.href);
          const path = urlObj.pathname || '';
          const base = path.split('/').filter(Boolean).pop() || '';
          return base.trim();
        } catch (_) {
          const clean = String(src || '').split('?')[0].trim();
          const parts = clean.split('/').filter(Boolean);
          return parts.length ? parts[parts.length - 1] : '';
        }
      };

      for (const row of rows) {
        const img = row.querySelector('img[src]');
        if (!img) continue;
        const src = (img.getAttribute('src') || '').trim();
        if (!src) continue;
        const name = normalizeName(src);
        if (!name) continue;
        names.push(name);
        if (names.length >= Math.max(1, Number(maxCount) || 500)) break;
      }

      return names;
    }
    """

    try:
        result = await page.evaluate(script, int(limit or 500))
    except Exception as exc:
        logger.warning(f"Failed to collect row image filenames from DOM: {exc}")
        return []

    if not isinstance(result, list):
        return []
    return [str(item).strip() for item in result if str(item or "").strip()]


def _collect_image_filenames_from_dir(images_dir: str, limit: int = 5000) -> list[str]:
    return collect_image_filenames_from_dir(images_dir, limit=limit)


async def _collect_page_state_summary(max_elements: int = 150, max_rows: int = 30) -> dict:
    await executor._ensure_page()
    page = executor._page

    try:
        dom_result = await executor.mark_page_elements()
        elements = dom_result.get("elements", []) if isinstance(dom_result, dict) else []
    except Exception:
        elements = []

    element_summaries: list[dict[str, Any]] = []
    for element in elements[: max(1, int(max_elements or 150))]:
        attrs = element.get("attributes") or {}
        element_summaries.append(
            {
                "id": element.get("id"),
                "tag": element.get("tagName"),
                "text": str(element.get("text") or "")[:80],
                "class": str(attrs.get("class") or "")[:80],
                "name": str(attrs.get("name") or "")[:40],
            }
        )

    page_info = await page.evaluate(
        """
        (maxRows) => {
          const title = document.title || '';
          const visibleText = (document.body?.innerText || '').replace(/\s+/g, ' ').slice(0, 3000);

          const controls = Array.from(document.querySelectorAll('button, a, [role="button"], .el-link__inner'))
            .map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
            .filter(Boolean)
            .slice(0, 120);

          const tables = [];
          const tableCandidates = Array.from(document.querySelectorAll('table, .el-table__body-wrapper'));
          for (const candidate of tableCandidates.slice(0, 4)) {
            const table = candidate.tagName?.toLowerCase() === 'table' ? candidate : candidate.querySelector('table');
            if (!table) continue;
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => (th.innerText || '').replace(/\s+/g, ' ').trim());
            const rows = [];
            const trList = Array.from(table.querySelectorAll('tbody tr'));
            for (const tr of trList.slice(0, Math.max(1, Number(maxRows) || 30))) {
              const cells = Array.from(tr.querySelectorAll('td')).map(td => (td.innerText || '').replace(/\s+/g, ' ').trim());
              if (cells.length) rows.push(cells);
            }
            if (headers.length || rows.length) {
              tables.push({ headers, rows });
            }
          }

          return { title, visibleText, controls, tables };
        }
        """,
        int(max_rows or 30),
    )

    return {
        "url": (page.url or "").strip(),
        "title": str(page_info.get("title") or ""),
        "visible_text": str(page_info.get("visibleText") or ""),
        "controls": page_info.get("controls") or [],
        "tables": page_info.get("tables") or [],
        "elements": element_summaries,
    }


def _resolve_preferred_filenames_via_vlm(
    *,
    task: str,
    mode_name: str,
    page_state: dict,
    candidate_filenames: list[str],
) -> list[str]:
    if not candidate_filenames:
        return []
    try:
        vlm = VLMService()
        ordered, _raw = vlm.order_filenames_from_page_state(
            task=task,
            mode_name=mode_name,
            page_state=page_state,
            candidate_filenames=candidate_filenames,
        )
    except Exception as exc:
        logger.warning(f"Failed to resolve filename order via VLM, fallback to default order: {exc}")
        return []

    if not isinstance(ordered, list):
        return []
    normalized = [str(item).strip() for item in ordered if str(item or "").strip()]
    return normalized


def _build_text_blob(element: dict) -> str:
    attrs = element.get("attributes") or {}
    fields = [
        element.get("text") or "",
        element.get("tagName") or "",
        attrs.get("id") or "",
        attrs.get("class") or "",
        attrs.get("name") or "",
        attrs.get("placeholder") or "",
        attrs.get("value") or "",
    ]
    return " ".join(str(v) for v in fields if v).lower()


def _extract_element_rect(element: dict) -> dict:
    rect = element.get("rect") or {}
    left = rect.get("left", rect.get("x"))
    top = rect.get("top", rect.get("y"))
    width = rect.get("width")
    height = rect.get("height")
    return {
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


def _build_dom_candidate_signature(element: dict) -> str:
    attrs = element.get("attributes") or {}
    rect = _extract_element_rect(element)
    parts = [
        str(element.get("id") or "").strip(),
        str(element.get("tagName") or "").strip().lower(),
        str((attrs.get("id") or "")).strip().lower(),
        str((attrs.get("name") or "")).strip().lower(),
        str((attrs.get("class") or "")).strip().lower(),
        str((element.get("text") or "")).strip().lower()[:120],
        str(rect.get("left") or ""),
        str(rect.get("top") or ""),
    ]
    return "|".join(parts)


def _is_element_enabled_for_action(element: dict) -> bool:
    attrs = element.get("attributes") or {}
    class_name = str(attrs.get("class") or "").lower()
    text_blob = _build_text_blob(element)
    blocked_tokens = ["disabled", "is-disabled", "not-allowed"]
    return not any(token in class_name or token in text_blob for token in blocked_tokens)


def _match_element_score(element: dict, include_keywords: list[str], exclude_keywords: Optional[list[str]] = None) -> int:
    text_blob = _build_text_blob(element)
    score = 0

    include_hits = 0
    for keyword in include_keywords:
        k = keyword.lower()
        if k and k in text_blob:
            include_hits += 1
            score += 3

    if include_hits == 0:
        return 0

    score += include_hits * 2
    if exclude_keywords:
        for keyword in exclude_keywords:
            k = keyword.lower()
            if k and k in text_blob:
                score -= 4

    tag = str(element.get("tagName") or "").lower()
    if tag in {"button", "a"}:
        score += 2
    elif tag in {"input", "label"}:
        score += 1

    attrs = element.get("attributes") or {}
    class_name = str(attrs.get("class") or "").lower()
    if any(token in class_name for token in ["menu", "sidebar", "sider", "navigation", "router-link"]):
        score -= 2

    rect = _extract_element_rect(element)
    top = rect.get("top", 9999)
    if isinstance(top, (int, float)):
        if top <= 180:
            score += 2
        elif top >= 300:
            score -= 1
    return score


async def _click_dom_element_by_keywords(
    *,
    include_keywords: list[str],
    exclude_keywords: Optional[list[str]] = None,
    require_enabled: bool = False,
    min_top: Optional[float] = None,
    max_top: Optional[float] = None,
    min_left: Optional[float] = None,
    max_left: Optional[float] = None,
    top_n: int = 3,
    max_rounds: int = 2,
) -> bool:
    rounds = max(1, int(max_rounds or 1))
    tried_signatures: set[str] = set()

    for _ in range(rounds):
        dom_result = await executor.mark_page_elements()
        elements = dom_result.get("elements", [])
        if not elements:
            await asyncio.sleep(0.2)
            continue

        ranked: list[tuple[int, str, dict]] = []
        for element in elements:
            rect = _extract_element_rect(element)
            top = rect.get("top")
            left = rect.get("left")

            if isinstance(top, (int, float)):
                if min_top is not None and top < min_top:
                    continue
                if max_top is not None and top > max_top:
                    continue

            if isinstance(left, (int, float)):
                if min_left is not None and left < min_left:
                    continue
                if max_left is not None and left > max_left:
                    continue

            score = _match_element_score(element, include_keywords, exclude_keywords)
            if score <= 0:
                continue
            if require_enabled and not _is_element_enabled_for_action(element):
                continue

            signature = _build_dom_candidate_signature(element)
            if signature in tried_signatures:
                continue
            ranked.append((score, signature, element))

        if not ranked:
            await asyncio.sleep(0.2)
            continue

        ranked.sort(key=lambda item: item[0], reverse=True)
        for _, signature, element in ranked[:max(1, top_n)]:
            tried_signatures.add(signature)
            element_id = str(element.get("id") or "").strip()
            if not element_id:
                continue

            clicked = await executor.click_element_by_id(element_id)
            if clicked:
                return True

            try:
                center = await executor.get_element_center(element_id)
                if center and "x" in center and "y" in center:
                    await executor.click_point((int(center["x"]), int(center["y"])))
                    return True
            except Exception:
                continue

        await asyncio.sleep(0.2)

    return False


async def _click_dom_element_by_vlm_objective(
    *,
    task: str,
    objective: str,
    top_k: int = 5,
    require_enabled: bool = False,
    min_top: Optional[float] = None,
    max_top: Optional[float] = None,
    min_left: Optional[float] = None,
    max_left: Optional[float] = None,
) -> bool:
    dom_result = await executor.mark_page_elements()
    raw_elements = dom_result.get("elements", []) if isinstance(dom_result, dict) else []
    if not raw_elements:
        return False

    filtered_elements: list[dict] = []
    for element in raw_elements:
        rect = _extract_element_rect(element)
        top = rect.get("top")
        left = rect.get("left")

        if isinstance(top, (int, float)):
            if min_top is not None and top < min_top:
                continue
            if max_top is not None and top > max_top:
                continue

        if isinstance(left, (int, float)):
            if min_left is not None and left < min_left:
                continue
            if max_left is not None and left > max_left:
                continue

        if require_enabled and not _is_element_enabled_for_action(element):
            continue
        filtered_elements.append(element)

    if not filtered_elements:
        return False

    candidate_ids = _safe_choose_dom_ids_via_vlm(
        task=task,
        objective=objective,
        elements=filtered_elements,
        top_k=top_k,
    )
    if not candidate_ids:
        return False

    for element_id in candidate_ids:
        clicked = await executor.click_element_by_id(element_id)
        if clicked:
            return True
        with contextlib.suppress(Exception):
            center = await executor.get_element_center(element_id)
            if center and "x" in center and "y" in center:
                await executor.click_point((int(center["x"]), int(center["y"])))
                return True
    return False


async def _wait_filter_result_ready(timeout_ms: int = 12000) -> bool:
    """等待页面出现筛选结果条（如：已选择筛选结果中 N 条数据）。"""
    await executor._ensure_page()
    page = executor._page
    ready_script = """
    () => {
      const text = (document.body?.innerText || '').replace(/\s+/g, ' ');
      return /已选择筛选结果中\s*\d+\s*条数据/.test(text) || /已选择\s*\d+\s*条数据/.test(text);
    }
    """

    try:
        await page.wait_for_function(ready_script, timeout=timeout_ms)
        return True
    except Exception:
        return False


async def _ensure_status_filter_applied(status_label: str, timeout_ms: int = 7000) -> bool:
    """强校验打包状态筛选是否真正生效。"""
    wanted = str(status_label or "").strip()
    if not wanted:
        return True

    await executor._ensure_page()
    page = executor._page

    verify_script = """
    (wantedLabel) => {
      const normalize = (value) => String(value || '').replace(/\s+/g, '');
      const wanted = normalize(wantedLabel);
      if (!wanted) return { matched: true, reason: 'empty_wanted' };

      const isVisible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };

      const controlSelectors = [
        '.filters', '.el-form', '.el-form-item', '.search', '.toolbar', '.query', '.filter',
        '.el-select', '.el-input', '.el-input__inner'
      ];

      let controlMatched = false;
      const controlNodes = Array.from(document.querySelectorAll(controlSelectors.join(','))).slice(0, 160);
      for (const node of controlNodes) {
        if (!isVisible(node)) continue;
        const text = normalize(node.innerText || node.textContent || '');
        const value = normalize(node.value || node.getAttribute?.('value') || '');
        if ((text.includes('打包状态') && text.includes(wanted)) || value.includes(wanted) || text === wanted) {
          controlMatched = true;
          break;
        }
      }

      const rowNodes = Array.from(document.querySelectorAll('tr.el-table__row')).filter(isVisible).slice(0, 60);
      let rowMatchedCount = 0;
      for (const row of rowNodes) {
        const stateCandidates = Array.from(
          row.querySelectorAll('.state-label .label, .state-label, td, span, div[title]')
        ).slice(0, 60);

        let matchedInRow = false;
        for (const node of stateCandidates) {
          const text = normalize(node.innerText || node.textContent || node.getAttribute('title') || '');
          if (!text) continue;
          if (text === wanted || text.includes(wanted)) {
            matchedInRow = true;
            break;
          }
        }

        if (!matchedInRow) {
          const rowText = normalize(row.innerText || row.textContent || '');
          if (rowText.includes(wanted)) {
            matchedInRow = true;
          }
        }

        if (matchedInRow) {
          rowMatchedCount += 1;
        }
      }

      const matched = controlMatched || rowMatchedCount > 0;
      return {
        matched,
        controlMatched,
        rowMatchedCount,
        rowCount: rowNodes.length,
      };
    }
    """

    deadline = dt.datetime.now() + dt.timedelta(milliseconds=max(500, int(timeout_ms or 7000)))
    last_probe: dict[str, Any] = {}

    while dt.datetime.now() < deadline:
        try:
            probe = await page.evaluate(verify_script, wanted)
            if isinstance(probe, dict):
                last_probe = probe
                if bool(probe.get("matched")):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.3)

    logger.warning(f"Status filter verification failed, expected={wanted}, probe={last_probe}")
    return False


async def _click_top_control_by_keywords(
    *,
    include_keywords: list[str],
    exclude_keywords: Optional[list[str]] = None,
    require_enabled: bool = True,
    top_n: int = 3,
) -> bool:
    return await _click_dom_element_by_keywords(
        include_keywords=include_keywords,
        exclude_keywords=exclude_keywords,
        require_enabled=require_enabled,
        min_top=0,
        max_top=160,
        min_left=120,
        top_n=top_n,
        max_rounds=2,
    )


async def _click_excel_export_button_direct() -> bool:
    await executor._ensure_page()
    page = executor._page
    script = """
    () => {
      const normalize = (v) => (v || '').replace(/\\s+/g, ' ').trim();
      const visible = (el) => {
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      };
      const disabled = (el) => {
        const cls = (el.className || '').toString().toLowerCase();
        if (el.disabled) return true;
        if (cls.includes('disabled') || cls.includes('is-disabled')) return true;
        return false;
      };
      const clickTarget = (el) => {
        const target = el.closest('button,a,[role=\"button\"],.el-button,.el-link') || el;
        if (!target || !visible(target) || disabled(target)) return false;
        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        return true;
      };

      const controls = Array.from(document.querySelectorAll('button,a,[role=\"button\"],.el-button,.el-link,span,div'));
      const topControls = controls
        .filter((el) => {
          const rect = el.getBoundingClientRect();
          return rect.top >= 0 && rect.top <= 220 && rect.left >= 100;
        })
        .slice(0, 600);

      const candidates = topControls
        .map((el) => ({ el, text: normalize(el.innerText || el.textContent || '') }))
        .filter((item) => item.text && (item.text.includes('数据导出') || item.text.includes('导出') || /excel/i.test(item.text)))
        .filter((item) => !item.text.includes('图片') && !item.text.includes('视频'));

      for (const item of candidates) {
        if (clickTarget(item.el)) return true;
      }
      return false;
    }
    """
    try:
        return bool(await page.evaluate(script))
    except Exception:
        return False


async def _click_top_image_download_button() -> bool:
    await executor._ensure_page()
    page = executor._page
    locator_candidates = [
        page.locator(".filters .el-dropdown .el-button", has_text="图片下载").first,
        page.locator(".filters .el-dropdown", has_text="图片下载").first,
        page.locator(".filters button", has_text="图片下载").first,
        page.locator("button", has_text="图片下载").first,
        page.get_by_text("图片下载", exact=True).first,
    ]

    for locator in locator_candidates:
        try:
            await locator.wait_for(state="visible", timeout=1200)
            try:
                enabled = await locator.evaluate(
                    """
                    (el) => {
                      const cls = (el.className || '').toString().toLowerCase();
                      const txt = ((el.innerText || '') + ' ' + (el.value || '')).toLowerCase();
                      if (el.disabled) return false;
                      if (cls.includes('is-disabled') || cls.includes('disabled')) return false;
                      if (txt.includes('disabled')) return false;
                      return true;
                    }
                    """
                )
                if not enabled:
                    continue
            except Exception:
                pass
            await locator.click(timeout=1500)
            return True
        except Exception:
            continue

    if await _click_top_control_by_keywords(
        include_keywords=["图片下载"],
        exclude_keywords=["视频"],
        require_enabled=True,
        top_n=2,
    ):
        return True

    if await _click_dom_element_by_vlm_objective(
        task="点击顶部图片下载按钮",
        objective="点击页面顶部筛选栏区域的“图片下载”按钮，避免左侧导航和表格行内按钮。",
        top_k=4,
        require_enabled=True,
        min_top=0,
        max_top=180,
        min_left=120,
    ):
        return True
    return False


async def _open_top_image_download_dropdown() -> bool:
    clicked = await _click_top_image_download_button()
    if clicked:
        await asyncio.sleep(0.35)
    return clicked


async def _click_image_download_menu_item(
    item_texts: list[str],
    timeout_ms: int = 5000,
) -> bool:
    await executor._ensure_page()
    page = executor._page

    attempts = max(2, int(timeout_ms / 300))

    for _ in range(attempts):
        for text in item_texts:
            try:
                item_locator = page.locator(".el-dropdown-menu__item", has_text=text)
                count = await item_locator.count()
                for idx in range(min(count, 12)):
                    candidate = item_locator.nth(idx)
                    try:
                        if not await candidate.is_visible():
                            continue
                        await candidate.click(timeout=1200)
                        return True
                    except Exception:
                        try:
                            await candidate.click(timeout=1200, force=True)
                            return True
                        except Exception:
                            continue
            except Exception:
                pass

            candidates = [
                page.locator(".el-popper .el-dropdown-menu__item", has_text=text),
                page.locator(".el-dropdown-menu__item", has_text=text),
                page.locator("[role='menuitem']", has_text=text),
                page.locator("li", has_text=text),
            ]
            for locator in candidates:
                try:
                    count = await locator.count()
                except Exception:
                    count = 0
                for idx in range(min(count, 8)):
                    node = locator.nth(idx)
                    try:
                        if not await node.is_visible():
                            continue
                        await node.click(timeout=1000)
                        return True
                    except Exception:
                        try:
                            await node.click(timeout=1000, force=True)
                            return True
                        except Exception:
                            continue

        clicked = await page.evaluate(
            """
            (texts) => {
              const normalize = (s) => String(s || '').replace(/\s+/g, '').toLowerCase();
              const wanted = (texts || []).map(normalize).filter(Boolean);
              if (!wanted.length) return false;

              const isVisible = (el) => {
                if (!el) return false;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };

              const containers = Array.from(document.querySelectorAll('.el-popper, .el-popover, .el-dropdown-menu'))
                .filter(el => isVisible(el))
                .filter(el => {
                  const r = el.getBoundingClientRect();
                  return r.top >= 0 && r.top <= 360;
                })
                .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

              for (const container of containers) {
                const items = Array.from(container.querySelectorAll('.el-dropdown-menu__item, li, span, div'));
                for (const item of items) {
                  if (!isVisible(item)) continue;
                  const txt = normalize(item.innerText || item.textContent || '');
                  if (!txt) continue;
                  if (wanted.some(w => txt === w || txt.includes(w))) {
                    item.click();
                    return true;
                  }
                }
              }
              return false;
            }
            """,
            item_texts,
        )
        if clicked:
            return True

        joined_labels = " / ".join(str(item).strip() for item in item_texts if str(item).strip())
        if joined_labels and await _click_dom_element_by_vlm_objective(
            task=f"点击图片下载菜单项：{joined_labels}",
            objective=f"在已展开的下拉菜单/弹层中点击图片模式选项：{joined_labels}。",
            top_k=5,
            require_enabled=False,
            min_top=0,
            max_top=380,
        ):
            return True

        # PageState -> VLM next_action 试点：在菜单场景中决策单步动作
        if joined_labels:
            try:
                dom_now = await executor.mark_page_elements()
                elements_now = dom_now.get("elements", []) if isinstance(dom_now, dict) else []
                page_state = await _collect_page_state_summary(max_elements=120, max_rows=12)
                next_action = _safe_next_action_via_vlm(
                    task=f"点击图片下载菜单项：{joined_labels}",
                    objective=f"在当前已展开的菜单中点击 {joined_labels}，不要点击导航栏。",
                    page_state=page_state,
                    elements=elements_now,
                    allowed_actions=["click_element", "wait", "noop"],
                )
                action_name = str(next_action.get("action") or "noop")
                if action_name == "click_element":
                    next_id = str(next_action.get("element_id") or "").strip()
                    if next_id and await executor.click_element_by_id(next_id):
                        await asyncio.sleep(0.2)
                        return True
                elif action_name == "wait":
                    wait_ms = int(next_action.get("ms") or 300)
                    await asyncio.sleep(max(0.1, min(wait_ms, 3000) / 1000.0))
            except Exception as exc:
                logger.debug(f"VLM next_action trial failed in image menu selection: {exc}")

        await asyncio.sleep(0.2)

    return False


async def _download_zip_via_top_image_controls(
    save_path: str,
    timeout_ms: int = 30000,
    image_mode_labels: Optional[list[str]] = None,
) -> str:
    """通过顶部“图片下载”菜单下载图片 zip。

    路径 A：点击指定菜单项即触发下载。
    路径 B：菜单项仅切换模式，再点击一次顶部“图片下载”触发下载。
    """
    await executor._ensure_page()
    page = executor._page

    try:
        history_seed_url = page.url or os.getenv("STEEL_TARGET_URL", "")
        current_url, redirected = await _ensure_active_history_page(history_seed_url)
        if redirected:
            logger.info(f"Adjusted active page before opening image menu: {current_url}")
        await executor.wait_for_load(timeout_ms=10000)
        await executor.wait_for_stable(1000)
        page = executor._page
    except Exception as exc:
        logger.warning(f"Failed to stabilize active page before opening image menu: {exc}")

    mode_selected = False
    target_labels = [label for label in (image_mode_labels or []) if str(label).strip()]
    if not target_labels:
        target_labels = ["原始图片", "原图"]

    opened = await _open_top_image_download_dropdown()
    if not opened:
        raise RuntimeError("未能打开顶部“图片下载”菜单")

    direct_timeout = min(timeout_ms, 10000)
    try:
        async with page.expect_download(timeout=direct_timeout) as download_info:
            mode_selected = await _click_image_download_menu_item(target_labels, timeout_ms=5000)
            if not mode_selected:
                raise RuntimeError(f"未找到图片下载菜单项: {target_labels}")
        download = await download_info.value
        await download.save_as(save_path)
        return save_path
    except Exception as exc:
        logger.info(f"Menu-direct zip download not triggered, fallback to second-click path: {exc}")

    if not mode_selected:
        opened = await _open_top_image_download_dropdown()
        if not opened:
            raise RuntimeError("下载失败：无法重新打开“图片下载”菜单")
        mode_selected = await _click_image_download_menu_item(target_labels, timeout_ms=5000)
        if not mode_selected:
            raise RuntimeError(f"下载失败：未找到图片下载菜单项: {target_labels}")

    await asyncio.sleep(0.3)
    async with page.expect_download(timeout=min(timeout_ms, 20000)) as download_info:
        clicked = await _click_top_image_download_button()
        if not clicked:
            raise RuntimeError("下载失败：未能点击顶部“图片下载”按钮触发下载")

    download = await download_info.value
    final_save_path = _resolve_download_save_path(
        save_path=save_path,
        suggested_filename=getattr(download, "suggested_filename", None),
        preserve_download_filename=preserve_download_filename,
    )
    await download.save_as(final_save_path)
    return final_save_path


async def _download_zip_via_generic_skill(
    *,
    save_path: str,
    timeout_ms: int = 60000,
    image_mode_labels: Optional[list[str]] = None,
    profile: Optional[dict[str, Any]] = None,
) -> str:
    skill = GenericDownloadSkill(
        ensure_page=executor._ensure_page,
        get_page=lambda: executor._page,
        logger=logger,
    )

    labels = [str(item).strip() for item in (image_mode_labels or []) if str(item).strip()]
    default_include = ["图片下载", "zip", "download", *labels]
    default_exclude = ["视频", "excel", "数据导出"]
    include, exclude = get_download_keywords(
        profile,
        kind="zip",
        default_include=default_include,
        default_exclude=default_exclude,
    )

    async def _candidate_menu_select_and_download() -> bool:
        try:
            opened = await _open_top_image_download_dropdown()
            if not opened:
                return False
            selected = await _click_image_download_menu_item(labels or ["原始图片", "原图"], timeout_ms=5000)
            if not selected:
                return False
            await asyncio.sleep(0.2)
            return await _click_top_image_download_button()
        except Exception:
            return False

    async def _candidate_menu_select_direct_download() -> bool:
        try:
            opened = await _open_top_image_download_dropdown()
            if not opened:
                return False
            return await _click_image_download_menu_item(labels or ["原始图片", "原图"], timeout_ms=5000)
        except Exception:
            return False

    async def _candidate_zip_text_button() -> bool:
        return await _click_dom_element_by_keywords(
            include_keywords=include,
            exclude_keywords=exclude,
            require_enabled=True,
            top_n=6,
        )

    async def _candidate_vlm() -> bool:
        try:
            dom_now = await executor.mark_page_elements()
            elements_now = dom_now.get("elements", []) if isinstance(dom_now, dict) else []
            page_state = await _collect_page_state_summary(max_elements=160, max_rows=24)
            next_action = _safe_next_action_via_vlm(
                task=f"下载图片压缩包（模式：{','.join(labels) if labels else '默认'}）",
                objective="在当前页面找到能触发图片文件下载的控件并点击；优先图片下载相关操作。",
                page_state=page_state,
                elements=elements_now,
                allowed_actions=["click_element", "wait", "noop"],
            )
            action_name = str(next_action.get("action") or "noop")
            if action_name == "click_element":
                element_id = str(next_action.get("element_id") or "").strip()
                return bool(element_id and await executor.click_element_by_id(element_id))
            if action_name == "wait":
                wait_ms = int(next_action.get("ms") or 300)
                await asyncio.sleep(max(0.1, min(wait_ms, 3000) / 1000.0))
        except Exception as exc:
            logger.debug(f"VLM next_action trial failed in zip generic download click: {exc}")
        return False

    intent = build_download_intent(
        kind="zip",
        include_keywords=include,
        exclude_keywords=exclude,
    )
    return await skill.download_with_click_candidates(
        intent=intent,
        save_path=save_path,
        click_candidates=[
            _candidate_menu_select_direct_download,
            _candidate_menu_select_and_download,
            _candidate_zip_text_button,
            _candidate_vlm,
        ],
        timeout_ms=min(timeout_ms, 12000),
    )


async def _select_first_table_row_for_export() -> bool:
    await executor._ensure_page()
    page = executor._page

    script = """
    () => {
      const table = document.querySelector('.el-table__body-wrapper');
      if (!table) return { ok: false, reason: 'table_not_found' };

      const checked = table.querySelector('.el-checkbox__input.is-checked');
      if (checked) return { ok: true, mode: 'already_checked' };

      const firstInner = table.querySelector('.el-checkbox__inner');
      if (!firstInner) return { ok: false, reason: 'checkbox_not_found' };

      firstInner.click();
      return { ok: true, mode: 'click_first_checkbox' };
    }
    """

    result = await page.evaluate(script)
    if bool(result.get("ok", False)):
        return True

    # DOM 注入兜底：尝试点击与 checkbox 相关的可交互元素
    clicked = await _click_dom_element_by_keywords(
        include_keywords=["checkbox", "el-checkbox"],
        exclude_keywords=["header"],
        require_enabled=False,
        top_n=6,
    )
    if clicked:
        await asyncio.sleep(0.4)
        return True

    # 页面点击兜底：第一行左侧近似坐标
    fallback = await page.evaluate(
        """
        () => {
          const table = document.querySelector('.el-table__body-wrapper');
          if (!table) return false;
          const rect = table.getBoundingClientRect();
          const x = Math.round(rect.left + 18);
          const y = Math.round(rect.top + 24);
          const el = document.elementFromPoint(x, y);
          if (!el) return false;
          el.click();
          return true;
        }
        """
    )
    return bool(fallback)


async def _click_download_by_dom(
    *,
    include_keywords: list[str],
    save_path: str,
    exclude_keywords: Optional[list[str]] = None,
    timeout_ms: int = 60000,
    preserve_download_filename: bool = False,
) -> str:
    await executor._ensure_page()
    page = executor._page

    async with page.expect_download(timeout=timeout_ms) as download_info:
        clicked = await _click_dom_element_by_keywords(
            include_keywords=include_keywords,
            exclude_keywords=exclude_keywords,
            require_enabled=True,
            top_n=5,
        )
        if not clicked:
            raise RuntimeError(f"Failed to click DOM download trigger: {include_keywords}")

    download = await download_info.value
    final_save_path = _resolve_download_save_path(
        save_path=save_path,
        suggested_filename=getattr(download, "suggested_filename", None),
        preserve_download_filename=preserve_download_filename,
    )
    await download.save_as(final_save_path)
    return final_save_path


async def _click_preferred_download_button(
    kind: str,
    save_path: str,
    timeout_ms: int = 60000,
    image_mode_labels: Optional[list[str]] = None,
    profile: Optional[dict[str, Any]] = None,
    target_url: Optional[str] = None,
    preserve_download_filename: bool = False,
) -> str:
    with contextlib.suppress(Exception):
        current_hint = str(getattr(executor._page, "url", "") or "")
        guard_url = (target_url or current_hint or "").strip()
        if guard_url:
            await _ensure_active_history_page(guard_url)
        await _cleanup_auxiliary_blank_pages(target_url=guard_url)
        await _restore_page_focus_after_download()

    skill = GenericDownloadSkill(
        ensure_page=executor._ensure_page,
        get_page=lambda: executor._page,
        logger=logger,
    )

    kind_lower = (kind or "").lower()
    if kind_lower == "excel":
        include_keywords, exclude_keywords = get_download_keywords(
            profile,
            kind="excel",
            default_include=["数据导出", "导出", "excel"],
            default_exclude=["视频", "图片"],
        )

        async def _candidate_direct() -> bool:
            return await _click_excel_export_button_direct()

        async def _candidate_top() -> bool:
            return await _click_top_control_by_keywords(
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                require_enabled=True,
                top_n=3,
            )

        async def _candidate_dom() -> bool:
            return await _click_dom_element_by_keywords(
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                require_enabled=True,
                top_n=5,
            )

        async def _candidate_vlm() -> bool:
            try:
                dom_now = await executor.mark_page_elements()
                elements_now = dom_now.get("elements", []) if isinstance(dom_now, dict) else []
                page_state = await _collect_page_state_summary(max_elements=140, max_rows=20)
                next_action = _safe_next_action_via_vlm(
                    task="下载当前筛选结果的Excel",
                    objective="点击当前页面中与导出Excel最相关的可交互元素，避免图片或视频下载。",
                    page_state=page_state,
                    elements=elements_now,
                    allowed_actions=["click_element", "wait", "noop"],
                )
                action_name = str(next_action.get("action") or "noop")
                if action_name == "click_element":
                    element_id = str(next_action.get("element_id") or "").strip()
                    return bool(element_id and await executor.click_element_by_id(element_id))
                if action_name == "wait":
                    wait_ms = int(next_action.get("ms") or 300)
                    await asyncio.sleep(max(0.1, min(wait_ms, 3000) / 1000.0))
                    return False
            except Exception as exc:
                logger.debug(f"VLM next_action trial failed in excel download click: {exc}")
            return False

        intent = build_download_intent(kind="excel")
        try:
            return await skill.download_with_click_candidates(
                intent=intent,
                save_path=save_path,
                click_candidates=[_candidate_direct, _candidate_top, _candidate_dom, _candidate_vlm],
                timeout_ms=min(timeout_ms, 15000),
                allow_link_probe=False,
                preserve_download_filename=preserve_download_filename,
            )
        except Exception as exc:
            logger.info(f"Generic excel download path failed, fallback to dom-direct: {exc}")
            return await _click_download_by_dom(
                include_keywords=include_keywords,
                exclude_keywords=exclude_keywords,
                save_path=save_path,
                timeout_ms=min(timeout_ms, 20000),
                preserve_download_filename=preserve_download_filename,
            )

    try:
        saved = await _download_zip_via_generic_skill(
            save_path=save_path,
            timeout_ms=min(timeout_ms, 30000),
            image_mode_labels=image_mode_labels,
            profile=profile,
        )
        with contextlib.suppress(Exception):
            await _cleanup_auxiliary_blank_pages(target_url=target_url)
        return saved
    except Exception as exc:
        logger.info(f"Generic zip download failed, fallback to top-controls strategy: {exc}")
        saved = await _download_zip_via_top_image_controls(
            save_path=save_path,
            timeout_ms=min(timeout_ms, 30000),
            image_mode_labels=image_mode_labels,
        )
        with contextlib.suppress(Exception):
            await _cleanup_auxiliary_blank_pages(target_url=target_url)
        return saved


async def _capture_steel_page_snapshot() -> dict:
    """捕获钢铁流程关键页面状态快照，用于动作后验证。"""
    snapshot: dict[str, Any] = {
        "url": "",
        "is_history": False,
        "is_login": False,
        "ready": False,
        "selected_count": None,
    }

    try:
        await executor._ensure_page()
        page = executor._page
    except Exception:
        return snapshot

    try:
        current_url = (page.url or "").strip()
    except Exception:
        current_url = ""

    snapshot["url"] = current_url
    snapshot["is_history"] = "#/history" in current_url
    snapshot["is_login"] = _is_login_route(current_url)

    try:
        page_info = await page.evaluate(
            """
            () => {
              const text = (document.body?.innerText || '').replace(/\s+/g, ' ');
              const m = text.match(/已选择筛选结果中\s*(\d+)\s*条数据/) || text.match(/已选择\s*(\d+)\s*条数据/);
              const selectedCount = m ? Number.parseInt(m[1], 10) : null;
              const ready = Number.isFinite(selectedCount) || /已选择筛选结果中/.test(text);
              return {
                ready,
                selectedCount,
              };
            }
            """
        )
        snapshot["ready"] = bool(page_info.get("ready"))
        snapshot["selected_count"] = page_info.get("selectedCount")
    except Exception:
        pass

    return snapshot


async def _run_steel_action_with_snapshot(action: Callable[[], Awaitable[Any]]) -> dict:
    before = await _capture_steel_page_snapshot()
    payload = await action()
    after = await _capture_steel_page_snapshot()
    return {
        "before": before,
        "after": after,
        "payload": payload,
    }


def _verify_steel_snapshot_transition(
    stage: str,
    result: Any,
    *,
    require_history: bool = False,
    allow_login: bool = False,
    require_ready: bool = False,
) -> tuple[bool, str]:
    if not isinstance(result, dict):
        return False, f"{stage}: invalid stage result"

    before = result.get("before") if isinstance(result.get("before"), dict) else {}
    after = result.get("after") if isinstance(result.get("after"), dict) else {}

    after_url = str(after.get("url") or "")
    after_is_history = bool(after.get("is_history"))
    after_is_login = bool(after.get("is_login"))
    after_ready = bool(after.get("ready"))

    if require_history and not after_is_history:
        return False, f"{stage}: not on history route after action ({after_url})"
    if not allow_login and after_is_login:
        return False, f"{stage}: still on login route after action ({after_url})"
    if require_ready and not after_ready:
        return False, f"{stage}: filter result not ready after action"

    # 附加可观测信息（不强制）
    before_count = before.get("selected_count")
    after_count = after.get("selected_count")
    if stage == "filter" and before_count is not None and after_count is not None:
        logger.info(f"Steel filter selected count transition: {before_count} -> {after_count}")

    return True, "ok"


async def _run_steel_stage(
    *,
    stage: str,
    message: str,
    action: Callable[[], Awaitable[Any]],
    emit_callback: Callable[[dict], Awaitable[None]],
    retries: int = 1,
    retry_stage: Optional[str] = None,
    retry_message: Optional[str] = None,
    verify: Optional[Callable[[Any], bool | Awaitable[bool]]] = None,
    objective: Optional[str] = None,
    observer: Optional[Callable[[], Awaitable[dict[str, Any]]]] = None,
    retry_sleep_seconds: float = 0.8,
) -> Any:
    total_attempts = max(1, int(retries or 1))
    last_error: Optional[Exception] = None

    for attempt in range(1, total_attempts + 1):
        if attempt == 1:
            await emit_callback({"type": "steel_stage", "stage": stage, "message": message})
        else:
            await emit_callback(
                {
                    "type": "steel_stage",
                    "stage": retry_stage or f"{stage}_retry",
                    "message": retry_message or f"{message}（重试 {attempt}/{total_attempts}）",
                }
            )

        if observer is not None:
            try:
                observation_before = await observer()
                await emit_callback(
                    {
                        "type": "steel_observe",
                        "stage": stage,
                        "attempt": attempt,
                        "phase": "before",
                        "objective": objective,
                        "observation": observation_before,
                    }
                )
            except Exception as obs_exc:
                logger.debug(f"Steel observer(before) failed at {stage}: {obs_exc}")

        try:
            result = await action()

            if observer is not None:
                try:
                    observation_after = await observer()
                    await emit_callback(
                        {
                            "type": "steel_observe",
                            "stage": stage,
                            "attempt": attempt,
                            "phase": "after",
                            "objective": objective,
                            "observation": observation_after,
                        }
                    )
                except Exception as obs_exc:
                    logger.debug(f"Steel observer(after) failed at {stage}: {obs_exc}")

            if verify is not None:
                verify_result = verify(result)
                if inspect.isawaitable(verify_result):
                    verify_result = await verify_result

                verify_ok = False
                verify_reason = ""
                if isinstance(verify_result, tuple):
                    verify_ok = bool(verify_result[0])
                    if len(verify_result) >= 2:
                        verify_reason = str(verify_result[1] or "")
                else:
                    verify_ok = bool(verify_result)

                if not verify_ok:
                    reason = verify_reason or f"Stage verification failed: {stage}"
                    await emit_callback(
                        {
                            "type": "steel_stage",
                            "stage": f"{stage}_verify_failed",
                            "message": reason,
                        }
                    )
                    raise RuntimeError(reason)
            return result
        except Exception as exc:
            last_error = exc
            if attempt >= total_attempts:
                break
            await asyncio.sleep(max(0.1, retry_sleep_seconds))

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Stage failed: {stage}")


@dataclass
class SteelStageNode:
    stage: str
    message: str
    action: Callable[[], Awaitable[Any]]
    retries: int = 1
    retry_stage: Optional[str] = None
    retry_message: Optional[str] = None
    verify: Optional[Callable[[Any], Any]] = None
    objective: Optional[str] = None
    observer: Optional[Callable[[], Awaitable[dict[str, Any]]]] = None


def _wrap_steel_action_with_snapshot(action: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[dict]]:
    async def _wrapped() -> dict:
        return await _run_steel_action_with_snapshot(action)

    return _wrapped


def _make_steel_snapshot_verifier(
    *,
    stage: str,
    require_history: bool = True,
    allow_login: bool = False,
    require_ready: bool = False,
) -> Callable[[Any], tuple[bool, str]]:
    def _verify(result: Any) -> tuple[bool, str]:
        return _verify_steel_snapshot_transition(
            stage,
            result,
            require_history=require_history,
            allow_login=allow_login,
            require_ready=require_ready,
        )

    return _verify


async def _run_steel_stage_graph(
    *,
    nodes: list[SteelStageNode],
    emit_callback: Callable[[dict], Awaitable[None]],
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for node in nodes:
        results[node.stage] = await _run_steel_stage(
            stage=node.stage,
            message=node.message,
            action=node.action,
            emit_callback=emit_callback,
            retries=node.retries,
            retry_stage=node.retry_stage,
            retry_message=node.retry_message,
            verify=node.verify,
            objective=node.objective,
            observer=node.observer,
        )
    return results


async def _run_steel_download_pipeline(
    *,
    task: str,
    target_url: str,
    max_items: int,
    max_pages: int,
    auth_data_file: Optional[str] = None,
    stream_callback=None,
    start_date_override: Optional[str] = None,
    end_date_override: Optional[str] = None,
    output_dir_override: Optional[str] = None,
    publish_to_output_root: bool = True,
) -> dict:
    intent = _build_steel_task_intent(
        task=task,
        target_url=target_url,
        start_date_override=start_date_override,
        end_date_override=end_date_override,
    )
    start_date, end_date = intent.start_date, intent.end_date
    status_filter_value = intent.status_filter
    selected_image_modes = intent.image_modes or [_image_mode_spec(intent.image_mode_key)]
    workflow = build_steel_workflow_config(
        status_filter=status_filter_value,
        image_modes=selected_image_modes,
        download_images_enabled=intent.download_images_enabled,
        embed_excel_enabled=intent.embed_images_to_excel,
    )
    profile = resolve_workflow_profile(target_url)
    if not workflow.download_images.enabled:
        selected_image_modes = []
    primary_mode = selected_image_modes[0] if selected_image_modes else _image_mode_spec(intent.image_mode_key)
    image_mode_key = str(primary_mode.get("mode_key") or intent.image_mode_key)
    image_mode_labels = list(primary_mode.get("labels") or intent.image_mode_labels)
    image_column_name = str(primary_mode.get("column_name") or intent.image_column_name)

    async def emit(payload: dict) -> None:
        if stream_callback:
            maybe_awaitable = stream_callback(payload)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

    async def observe_page_state() -> dict[str, Any]:
        return await _capture_steel_page_snapshot()

    output_timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_range_name = _build_steel_output_dir_name(start_date, end_date)
    run_output_dir = output_dir_override or os.path.join(OUTPUT_DIR, output_range_name)
    ensure_dir(run_output_dir)

    raw_excel_path = os.path.join(run_output_dir, "__raw_excel__.xlsx")
    embed_suffix = _build_embed_excel_suffix(selected_image_modes)
    final_excel_path = os.path.join(run_output_dir, f"steel_with_images{embed_suffix}.xlsx")
    mode_zip_paths: dict[str, str] = {}
    mode_unzip_dirs: dict[str, str] = {}
    mode_picture_dirs: dict[str, str] = {}
    mode_image_counts: dict[str, int] = {}
    temporary_embed_outputs: list[str] = []
    items_collected = 0
    pages_processed = 1

    try:
        auth_path = await _apply_auth_data_if_available(target_url=target_url, auth_data_file=auth_data_file)
        if auth_path:
            await emit({"type": "steel_stage", "stage": "auth", "message": f"已加载登录态: {os.path.basename(auth_path)}"})

        async def _guard_history(reason_prefix: str) -> tuple[str, bool]:
            current_url, redirected = await _ensure_history_page(target_url)
            if redirected:
                await emit(
                    {
                        "type": "steel_stage",
                        "stage": "navigation",
                        "message": f"{reason_prefix}已纠偏至历史记录页: {current_url}",
                    }
                )
            return current_url, redirected

        async def _open_target_action() -> None:
            await executor.goto(target_url)
            await executor.wait_for_load()
            await executor.wait_for_stable(2000)
            try:
                current_route = await executor.get_url()
            except Exception:
                current_route = ""

            if _is_login_route(current_route):
                await emit({"type": "steel_stage", "stage": "auth", "message": "检测到登录页，尝试自动恢复登录态"})
                await _apply_auth_data_if_available(target_url=target_url, auth_data_file=auth_data_file)
                await executor.goto(_build_history_url(target_url))
                await executor.wait_for_load()
                await executor.wait_for_stable(1800)
                current_route = await executor.get_url()
                if _is_login_route(current_route):
                    raise RuntimeError("钢铁站点登录态失效：当前仍在登录页，请更新 cookies/auth_data 文件后重试。")

            await _guard_history("检测到路由偏移，")

        navigation_node = SteelStageNode(
            stage="navigation",
            message=f"打开目标站点: {target_url}",
            action=_wrap_steel_action_with_snapshot(_open_target_action),
            retries=2,
            retry_stage="navigation_retry",
            retry_message="打开目标站点失败，重试中",
            verify=_make_steel_snapshot_verifier(stage="navigation"),
            objective="确保当前页面位于历史记录路由并可进行筛选操作",
            observer=observe_page_state,
        )

        async def _set_date_action() -> None:
            await _guard_history("设置日期前")
            await _set_date_inputs(start_date, end_date)
            await asyncio.sleep(1)
            _, redirected = await _ensure_history_page(target_url)
            if redirected:
                current_url = await executor.get_url()
                await emit(
                    {
                        "type": "steel_stage",
                        "stage": "navigation",
                        "message": f"日期设置后发生跳页，已返回历史记录页并重设日期: {current_url}",
                    }
                )
                await _set_date_inputs(start_date, end_date)
                await asyncio.sleep(1)

        date_node = SteelStageNode(
            stage="date",
            message=f"设置日期范围: {start_date} ~ {end_date}",
            action=_wrap_steel_action_with_snapshot(_set_date_action),
            retries=2,
            retry_stage="date_retry",
            retry_message="日期设置失败，重试中",
            verify=_make_steel_snapshot_verifier(stage="date"),
            objective=f"设置时间范围为 {start_date} 到 {end_date}",
            observer=observe_page_state,
        )

        async def _select_filter_action() -> None:
            await _guard_history("筛选前")
            selected = await _select_status_filter(status_filter_value)
            if not selected:
                raise RuntimeError(f"未能在历史记录页找到“打包状态={status_filter_value}”筛选控件")
            applied = await _ensure_status_filter_applied(status_filter_value, timeout_ms=7000)
            if not applied:
                raise RuntimeError(f"打包状态筛选未生效：期望={status_filter_value}")
            await asyncio.sleep(1)

        filter_node = SteelStageNode(
            stage="filter",
            message=f"筛选打包状态={status_filter_value}",
            action=_wrap_steel_action_with_snapshot(_select_filter_action),
            retries=2,
            retry_stage="filter_retry",
            retry_message="筛选设置失败，重试中",
            verify=_make_steel_snapshot_verifier(stage="filter"),
            objective=f"将打包状态筛选为 {status_filter_value}",
            observer=observe_page_state,
        )

        async def _wait_ready_action() -> bool:
            ready = await _wait_filter_result_ready(timeout_ms=12000)
            if not ready:
                await emit({"type": "steel_stage", "stage": "wait_ready", "message": "未检测到筛选结果提示，继续尝试下载"})
            await asyncio.sleep(0.6)
            return bool(ready)

        wait_ready_node = SteelStageNode(
            stage="wait_ready",
            message="等待筛选结果就绪（已选择 N 条数据）",
            action=_wrap_steel_action_with_snapshot(_wait_ready_action),
            retries=1,
            verify=_make_steel_snapshot_verifier(stage="wait_ready"),
            objective="等待筛选结果提示出现并进入可导出状态",
            observer=observe_page_state,
        )

        initial_nodes = [navigation_node, date_node]
        if workflow.filter_status.enabled:
            initial_nodes.append(filter_node)
        else:
            await emit({"type": "steel_stage", "stage": "filter", "message": "未指定状态筛选，跳过该步骤"})
        if workflow.wait_ready.enabled:
            initial_nodes.append(wait_ready_node)

        await _run_steel_stage_graph(
            nodes=initial_nodes,
            emit_callback=emit,
        )

        async def _download_excel_action() -> str:
            nonlocal raw_excel_path, final_excel_path
            await _restore_page_focus_after_download()
            saved_excel = await _click_preferred_download_button(
                "excel",
                raw_excel_path,
                profile=profile,
                target_url=target_url,
                preserve_download_filename=True,
            )
            if not os.path.exists(saved_excel) or os.path.getsize(saved_excel) <= 0:
                raise RuntimeError("Excel 下载失败或文件为空")
            raw_excel_path = saved_excel
            raw_base, raw_ext = os.path.splitext(os.path.basename(raw_excel_path))
            if not raw_ext:
                raw_ext = ".xlsx"
            final_excel_path = os.path.join(run_output_dir, f"{raw_base}{embed_suffix}{raw_ext}")
            await _restore_page_focus_after_download()
            return raw_excel_path

        download_excel_node = SteelStageNode(
            stage="download_excel",
            message="下载数据 Excel",
            action=_download_excel_action,
            retries=2,
            retry_stage="download_excel_retry",
            retry_message="Excel 下载失败，重试中",
            objective="点击数据导出并下载Excel",
            observer=observe_page_state,
        )

        await _run_steel_stage_graph(nodes=[download_excel_node], emit_callback=emit)

        current_excel_for_embed = raw_excel_path
        for mode_index, mode_spec in enumerate(selected_image_modes):
            mode_key = str(mode_spec.get("mode_key") or "original")
            mode_labels = list(mode_spec.get("labels") or [])
            mode_column_name = str(mode_spec.get("column_name") or image_column_name)
            row_image_filenames = await _collect_row_image_filenames(limit=max_items if max_items > 0 else 500)
            items_collected = max(items_collected, len(row_image_filenames))

            if not workflow.download_images.enabled:
                await emit({"type": "steel_stage", "stage": f"download_zip_{mode_key}", "message": f"未启用图片下载，跳过 {mode_column_name}"})
                continue

            zip_path = os.path.join(run_output_dir, f"raw_images_{mode_key}.zip")
            unzip_dir = os.path.join(run_output_dir, f"unzipped_{mode_key}")
            mode_zip_paths[mode_key] = zip_path
            mode_unzip_dirs[mode_key] = unzip_dir

            async def _download_zip_action_for_mode() -> str:
                await _restore_page_focus_after_download()
                await _click_preferred_download_button(
                    "zip",
                    zip_path,
                    image_mode_labels=mode_labels,
                    profile=profile,
                    target_url=target_url,
                )
                if not os.path.exists(zip_path) or os.path.getsize(zip_path) <= 0:
                    raise RuntimeError(f"{mode_column_name} zip 下载失败或文件为空")
                await _restore_page_focus_after_download()
                return zip_path

            download_zip_node = SteelStageNode(
                stage=f"download_zip_{mode_key}",
                message=f"下载{mode_column_name}压缩包",
                action=_download_zip_action_for_mode,
                retries=1,
                retry_stage=f"download_zip_{mode_key}_retry",
                retry_message=f"同页面重试{mode_column_name}下载",
                objective=f"下载{mode_column_name}对应的图片压缩包",
                observer=observe_page_state,
            )

            zip_downloaded = False
            zip_error: Optional[Exception] = None
            try:
                await _run_steel_stage_graph(nodes=[download_zip_node], emit_callback=emit)
                zip_downloaded = True
            except Exception as zip_exc:
                zip_error = zip_exc
                logger.warning(f"Zip download failed on current page for mode={mode_key}: {zip_exc}")

            if not zip_downloaded:
                async def _recover_zip_action_for_mode() -> str:
                    await _apply_auth_data_if_available(target_url=target_url, auth_data_file=auth_data_file)
                    history_url = _build_history_url(target_url)
                    if not history_url:
                        raise RuntimeError("恢复下载失败：无有效历史记录页 URL")
                    await executor.goto(history_url)
                    await executor.wait_for_load()
                    await executor.wait_for_stable(1500)
                    with contextlib.suppress(Exception):
                        await _ensure_active_history_page(target_url)

                    await _set_date_inputs(start_date, end_date)
                    await asyncio.sleep(0.8)
                    if status_filter_value:
                        recovered_selected = await _select_status_filter(status_filter_value)
                        if not recovered_selected:
                            raise RuntimeError(f"恢复后仍无法设置打包状态={status_filter_value}")
                        recovered_applied = await _ensure_status_filter_applied(status_filter_value, timeout_ms=12000)
                        if not recovered_applied:
                            logger.warning(f"恢复后筛选校验未通过，继续尝试下载：期望={status_filter_value}")
                    await asyncio.sleep(0.8)
                    await _wait_filter_result_ready(timeout_ms=10000)

                    await _restore_page_focus_after_download()
                    await _click_preferred_download_button(
                        "zip",
                        zip_path,
                        image_mode_labels=mode_labels,
                        profile=profile,
                        target_url=target_url,
                    )
                    if not os.path.exists(zip_path) or os.path.getsize(zip_path) <= 0:
                        raise RuntimeError(f"{mode_column_name} zip 下载失败或文件为空")
                    await _restore_page_focus_after_download()
                    return zip_path

                recover_zip_node = SteelStageNode(
                    stage=f"recover_{mode_key}",
                    message=f"{mode_column_name}下载失败，尝试恢复并重试: {zip_error}",
                    action=_recover_zip_action_for_mode,
                    retries=0,
                    objective="恢复页面状态并重新触发图片压缩包下载",
                    observer=observe_page_state,
                )
                await _run_steel_stage_graph(nodes=[recover_zip_node], emit_callback=emit)
            if not os.path.exists(zip_path) or os.path.getsize(zip_path) <= 0:
                raise RuntimeError(f"{mode_column_name} zip 下载失败或文件为空")

            async def _unzip_action_for_mode() -> str:
                _unzip_file(zip_path, unzip_dir)
                picture_root_local = _find_picture_root(unzip_dir)
                if not picture_root_local:
                    raise RuntimeError(f"{mode_column_name}解压后未找到图片目录")
                return picture_root_local

            unzip_node = SteelStageNode(
                stage=f"unzip_{mode_key}",
                message=f"解压{mode_column_name}文件",
                action=_unzip_action_for_mode,
                retries=1,
                objective=f"解压{mode_column_name}压缩包并找到图片目录",
            )
            if not workflow.unzip_images.enabled:
                await emit({"type": "steel_stage", "stage": f"unzip_{mode_key}", "message": f"未启用解压步骤，跳过 {mode_column_name}"})
                continue
            unzip_results = await _run_steel_stage_graph(nodes=[unzip_node], emit_callback=emit)
            picture_root = str(unzip_results.get(f"unzip_{mode_key}") or "")
            if not picture_root:
                raise RuntimeError(f"{mode_column_name}解压后未找到图片目录")
            mode_picture_dirs[mode_key] = picture_root

            current_mode_images = _collect_image_filenames_from_dir(picture_root)
            mode_image_counts[mode_key] = len(current_mode_images)

            preferred_filenames = list(row_image_filenames)
            if not preferred_filenames:
                candidate_filenames = current_mode_images
                if candidate_filenames:
                    page_state: dict[str, Any] = {}
                    try:
                        page_state = await _collect_page_state_summary(max_elements=150, max_rows=30)
                    except Exception as exc:
                        logger.warning(f"Collect page state for filename ordering failed: {exc}")
                    vlm_preferred = _resolve_preferred_filenames_via_vlm(
                        task=task,
                        mode_name=mode_column_name,
                        page_state=page_state,
                        candidate_filenames=candidate_filenames,
                    )
                    if vlm_preferred:
                        preferred_filenames = vlm_preferred
                        logger.info(
                            "Use VLM-resolved filename order for mode=%s, count=%d",
                            mode_key,
                            len(preferred_filenames),
                        )

            is_last_mode = mode_index == (len(selected_image_modes) - 1)
            if is_last_mode:
                output_for_mode = final_excel_path
            else:
                output_for_mode = os.path.join(run_output_dir, f".tmp_embed_{mode_key}_{output_timestamp}.xlsx")
                temporary_embed_outputs.append(output_for_mode)

            async def _embed_action_for_mode() -> str:
                return str(
                    embed_images_to_excel(
                        excel_path=current_excel_for_embed,
                        images_dir=picture_root,
                        output_path=output_for_mode,
                        preferred_filenames=preferred_filenames,
                        column_name=mode_column_name,
                        image_width=160,
                        image_height=120,
                    )
                )

            embed_node = SteelStageNode(
                stage=f"embed_{mode_key}",
                message=f"生成{mode_column_name}列",
                action=_embed_action_for_mode,
                retries=1,
                objective=f"将{mode_column_name}嵌入Excel并生成输出文件",
            )
            if not workflow.embed_excel.enabled:
                await emit({"type": "steel_stage", "stage": f"embed_{mode_key}", "message": f"未启用嵌图步骤，跳过 {mode_column_name}"})
                continue
            embed_results = await _run_steel_stage_graph(nodes=[embed_node], emit_callback=emit)
            embedded_excel_path = str(embed_results.get(f"embed_{mode_key}") or "")
            if not embedded_excel_path:
                raise RuntimeError(f"生成{mode_column_name}列失败")
            current_excel_for_embed = embedded_excel_path

        final_output = current_excel_for_embed
        if not final_output:
            raise RuntimeError("生成带图 Excel 失败")

        with contextlib.suppress(Exception):
            await _ensure_active_history_page(target_url)
            await _cleanup_auxiliary_blank_pages(target_url=target_url)

        final_file_name = os.path.basename(str(final_output))
        if publish_to_output_root and str(final_output).startswith(run_output_dir):
            # 拷贝到可下载目录
            publish_path = os.path.join(OUTPUT_DIR, final_file_name)
            shutil.copy2(final_output, publish_path)
            final_output = publish_path

        await emit({"type": "steel_stage", "stage": "done", "message": "钢铁任务完成"})

        for temp_output in temporary_embed_outputs:
            with contextlib.suppress(Exception):
                if temp_output and os.path.exists(temp_output):
                    os.remove(temp_output)

        images_downloaded = sum(mode_image_counts.values())
        raw_zips_map = {k: os.path.basename(v) for k, v in mode_zip_paths.items() if v}
        unzip_dirs_map = {k: v for k, v in mode_unzip_dirs.items() if v}

        return {
            "status": "success",
            "final_excel": os.path.basename(str(final_output)),
            "raw_excel": os.path.basename(raw_excel_path),
            "raw_zip": os.path.basename(mode_zip_paths.get(image_mode_key, "")) if mode_zip_paths else None,
            "raw_zips": raw_zips_map,
            "zip_files": raw_zips_map,
            "picture_dir": mode_picture_dirs.get(image_mode_key),
            "picture_dirs": mode_picture_dirs,
            "unzipped_dir": mode_unzip_dirs.get(image_mode_key),
            "unzipped_dirs": unzip_dirs_map,
            "output_dir": run_output_dir,
            "date_range": {"start": start_date, "end": end_date},
            "status_filter": status_filter_value,
            "image_mode": image_mode_key,
            "image_modes": [str(item.get("mode_key") or "") for item in selected_image_modes],
            "items_collected": int(items_collected),
            "images_downloaded": int(images_downloaded),
            "image_counts": mode_image_counts,
            "pages_processed": int(pages_processed),
            "intent_source": intent.source,
            "reasoning": "Steel pipeline completed successfully",
        }
    except Exception as exc:
        logger.exception(f"Steel pipeline failed: {exc}")
        await emit({"type": "steel_stage", "stage": "failed", "message": str(exc)})
        with contextlib.suppress(Exception):
            await _ensure_active_history_page(target_url)
            await _cleanup_auxiliary_blank_pages(target_url=target_url)
        for temp_output in temporary_embed_outputs:
            with contextlib.suppress(Exception):
                if temp_output and os.path.exists(temp_output):
                    os.remove(temp_output)
        raw_zips_map = {k: os.path.basename(v) for k, v in mode_zip_paths.items() if os.path.exists(v)}
        unzip_dirs_map = {k: v for k, v in mode_unzip_dirs.items() if os.path.exists(v)}
        images_downloaded = sum(
            len(_collect_image_filenames_from_dir(path))
            for path in mode_picture_dirs.values()
            if path and os.path.isdir(path)
        )
        return {
            "status": "failed",
            "final_excel": None,
            "raw_excel": os.path.basename(raw_excel_path) if os.path.exists(raw_excel_path) else None,
            "raw_zip": os.path.basename(mode_zip_paths.get(image_mode_key, "")) if mode_zip_paths and mode_zip_paths.get(image_mode_key) else None,
            "raw_zips": raw_zips_map,
            "zip_files": raw_zips_map,
            "picture_dir": None,
            "picture_dirs": mode_picture_dirs,
            "unzipped_dir": mode_unzip_dirs.get(image_mode_key),
            "unzipped_dirs": unzip_dirs_map,
            "output_dir": run_output_dir,
            "date_range": {"start": start_date, "end": end_date},
            "status_filter": status_filter_value,
            "image_mode": image_mode_key,
            "image_modes": [str(item.get("mode_key") or "") for item in selected_image_modes],
            "items_collected": int(items_collected),
            "images_downloaded": int(images_downloaded),
            "pages_processed": int(pages_processed),
            "intent_source": intent.source,
            "reasoning": str(exc),
        }


def _finish_check(task: str, parse_resp: ParseResponse, current_url: str) -> Optional[dict]:
    planner.should_finish(
        task,
        parse_resp.elements,
        current_url=current_url,
        annotated_image_base64=parse_resp.annotated_image_base64,
        image_size=parse_resp.image_size,
    )
    return getattr(planner, "_last_finish_debug", None)


@app.post("/step", response_model=StepResponse)
async def step(request: StepRequest) -> StepResponse:
    if request.override_point is not None:
        old_url = await executor.get_url()
        await executor.click_point(request.override_point)
        await executor.wait_for_url_change(old_url)
        await executor.wait_for_load()
        await executor.wait_for_stable(500)
        try:
            screenshot_path, parse_resp = await _capture_and_parse()
            dom_elements = await executor.get_dom_elements()
            _merge_dom_inputs(parse_resp, dom_elements)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Parse failed: {exc}") from exc
        current_url = await executor.get_url()
        finish_debug = _finish_check(request.task, parse_resp, current_url)
        return StepResponse(
            action="click",
            target_id=None,
            reason="manual",
            screenshot_path=screenshot_path,
            annotated_image_base64=parse_resp.annotated_image_base64,
            elements=parse_resp.elements,
            current_url=current_url,
            finish_debug=finish_debug,
        )

    # 1) Screenshot + parse
    try:
        screenshot_path, parse_resp = await _capture_and_parse()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Parse failed: {exc}") from exc

    # 1.1) Merge DOM input boxes
    dom_elements = await executor.get_dom_elements()
    _merge_dom_inputs(parse_resp, dom_elements)

    # 1.2) Finish check (for debug display)
    current_url = await executor.get_url()
    finish_debug = _finish_check(request.task, parse_resp, current_url)

    # 2) Choose target
    target_id = request.override_target_id
    target_point = request.override_point
    action_tool = None
    action_text = None
    action_key = None
    action_ms = None
    action_url = None
    action_scroll = None
    reason = "override"
    llm_query = None
    vlm_conversation = None  # v2.2: VLM 对话详情

    if target_id is None and request.override_point is None:
        plan_resp = planner.plan(
            PlanRequest(
                task=request.task,
                elements=parse_resp.elements,
                image_size=parse_resp.image_size,
                annotated_image_base64=parse_resp.annotated_image_base64,
                plan_context=request.plan_context,
            )
        )
        target_id = plan_resp.target_id
        target_point = plan_resp.target_point
        action_tool = plan_resp.action_tool
        action_text = plan_resp.action_text
        action_key = plan_resp.action_key
        action_ms = plan_resp.action_ms
        action_url = plan_resp.action_url
        action_scroll = plan_resp.action_scroll
        reason = plan_resp.reason
        llm_query = plan_resp.query
        planner_debug = plan_resp.debug

        # v2.2: 构造 VLM 对话详情（用于前端 debug）
        if planner_debug and planner_debug.get('provider') in {'qwen3-vl', 'dashscope'}:
            vlm_conversation = {
                'request': {
                    'task': request.task,
                    'elements_count': len(parse_resp.elements),
                    'elements': [
                        {
                            'id': e.id,
                            'type': e.type,
                            'content': e.content[:50] + '...' if len(e.content) > 50 else e.content,
                        }
                        for e in parse_resp.elements[:20]  # 只显示前20个元素
                    ],
                    'image_size': parse_resp.image_size,
                    'annotated_image': parse_resp.annotated_image_base64,  # 标注图片
                },
                'response': planner_debug.get('response', {}),
                'response_raw': planner_debug.get('response_raw', ''),
            }
    else:
        planner_debug = None

    # If the step is typing but tool isn't, coerce to type with best-effort text.
    if _is_typing_task(request.task) and (action_tool is None or action_tool == "click"):
        action_tool = "type"
        if not action_text:
            action_text = llm_query or _extract_query(request.task)
        if action_key is None and any(k in request.task.lower() for k in ["enter", "回车"]):
            action_key = "Enter"
        if target_id is None and target_point is None:
            first_input = next((e for e in parse_resp.elements if e.type == "dom_input"), None)
            if first_input:
                target_id = first_input.id

    # 3) Execute tool action when provided
    if action_tool:
        resolved_point = target_point
        if resolved_point is None and target_id is not None:
            elem = next((e for e in parse_resp.elements if e.id == target_id), None)
            if elem:
                resolved_point = elem.center
        if action_tool == "wait":
            await executor.wait_for_stable(action_ms or 1000)
        elif action_tool == "press":
            await executor.press(action_key or "Enter")
            await executor.wait_for_load()
        elif action_tool == "scroll":
            await executor.scroll_by(action_scroll or 600)
            await executor.wait_for_stable(800)
        elif action_tool == "goto":
            if not action_url:
                return StepResponse(
                    action="noop",
                    target_id=target_id,
                    target_point=target_point,
                    action_tool=action_tool,
                    action_text=action_text,
                    action_key=action_key,
                    action_ms=action_ms,
                    action_url=action_url,
                    action_scroll=action_scroll,
                    reason="no_url",
                    screenshot_path=screenshot_path,
                    annotated_image_base64=parse_resp.annotated_image_base64,
                    elements=parse_resp.elements,
                    current_url=current_url,
                    planner_debug=planner_debug,
                    finish_debug=finish_debug,
                )
            await executor.goto(action_url)
            await executor.wait_for_load()
        elif action_tool == "type":
            text = action_text or llm_query
            if not text:
                return StepResponse(
                    action="noop",
                    target_id=target_id,
                    target_point=target_point,
                    action_tool=action_tool,
                    action_text=action_text,
                    action_key=action_key,
                    action_ms=action_ms,
                    action_scroll=action_scroll,
                    reason="no_text",
                    screenshot_path=screenshot_path,
                    annotated_image_base64=parse_resp.annotated_image_base64,
                    elements=parse_resp.elements,
                    current_url=current_url,
                    planner_debug=planner_debug,
                    finish_debug=finish_debug,
                )
            if resolved_point is not None:
                await executor.click_point(resolved_point)
            await executor.type_text(text)
            if action_key:
                await executor.press(action_key)
            await executor.wait_for_load()
        elif action_tool == "copy":
            if resolved_point is not None:
                await executor.click_point(resolved_point)
            await executor.press("Control+C")
            await executor.wait_for_stable(300)
        else:
            if resolved_point is not None:
                await executor.click_point(resolved_point)
            await executor.wait_for_load()

        try:
            new_path, new_parse = await _capture_and_parse()
            dom_elements = await executor.get_dom_elements()
            _merge_dom_inputs(new_parse, dom_elements)
        except Exception:
            new_path, new_parse = screenshot_path, parse_resp
        new_url = await executor.get_url()
        new_finish_debug = _finish_check(request.task, new_parse, new_url)
        return StepResponse(
            action=action_tool,
            target_id=target_id,
            target_point=target_point,
            action_tool=action_tool,
            action_text=action_text,
            action_key=action_key,
            action_ms=action_ms,
            action_url=action_url,
            action_scroll=action_scroll,
            reason=reason,
            screenshot_path=new_path,
            annotated_image_base64=new_parse.annotated_image_base64,
            elements=new_parse.elements,
            current_url=new_url,
            planner_debug=planner_debug,
            finish_debug=new_finish_debug,
            vlm_conversation=vlm_conversation,  # v2.2: VLM 对话详情
        )

    # 4) Legacy click / search chain
    if request.override_point is not None:
        await executor.click_point(request.override_point)
    elif target_id is not None:
        elem = next((e for e in parse_resp.elements if e.id == target_id), None)
        if elem is None:
            raise HTTPException(status_code=404, detail=f"Element id {target_id} not found")
        if _is_typing_task(request.task) and elem.type == "dom_input":
            query = llm_query
            if not query:
                return StepResponse(
                    action="noop",
                    target_id=target_id,
                    reason="no_query",
                    screenshot_path=screenshot_path,
                    annotated_image_base64=parse_resp.annotated_image_base64,
                    elements=parse_resp.elements,
                    current_url=current_url,
                    planner_debug=planner_debug,
                    finish_debug=finish_debug,
                )
            old_url = await executor.get_url()
            await executor.click_center(elem.center)
            await executor.type_text(query)
            await executor.press("Enter")
            await executor.wait_for_url_change(old_url)
            await executor.wait_for_load()
            # Refresh parse after navigation so UI shows the new page
            try:
                new_path, new_parse = await _capture_and_parse()
                dom_elements = await executor.get_dom_elements()
                _merge_dom_inputs(new_parse, dom_elements)
            except Exception:
                new_path, new_parse = screenshot_path, parse_resp
            new_url = await executor.get_url()
            new_finish_debug = _finish_check(request.task, new_parse, new_url)
            return StepResponse(
                action="search",
                target_id=target_id,
                target_point=target_point,
                action_tool=action_tool,
                action_text=action_text,
                action_key=action_key,
                action_ms=action_ms,
                action_url=action_url,
                action_scroll=action_scroll,
                reason=reason,
                screenshot_path=new_path,
                annotated_image_base64=new_parse.annotated_image_base64,
                elements=new_parse.elements,
                current_url=new_url,
                planner_debug=planner_debug,
                finish_debug=new_finish_debug,
                vlm_conversation=vlm_conversation,  # v2.2: VLM 对话详情
            )
        old_url = await executor.get_url()
        await executor.click_center(elem.center)
        await executor.wait_for_url_change(old_url)
        await executor.wait_for_load()
        await executor.wait_for_stable(800)
        try:
            new_path, new_parse = await _capture_and_parse()
            dom_elements = await executor.get_dom_elements()
            _merge_dom_inputs(new_parse, dom_elements)
        except Exception:
            new_path, new_parse = screenshot_path, parse_resp
        new_url = await executor.get_url()
        new_finish_debug = _finish_check(request.task, new_parse, new_url)
        return StepResponse(
            action="click",
            target_id=target_id,
            target_point=target_point,
            action_tool=action_tool,
            action_text=action_text,
            action_key=action_key,
            action_ms=action_ms,
            action_url=action_url,
            action_scroll=action_scroll,
            reason=reason,
            screenshot_path=new_path,
            annotated_image_base64=new_parse.annotated_image_base64,
            elements=new_parse.elements,
            current_url=new_url,
            planner_debug=planner_debug,
            finish_debug=new_finish_debug,
            vlm_conversation=vlm_conversation,  # v2.2: VLM 对话详情
        )
    elif target_point is not None:
        old_url = await executor.get_url()
        await executor.click_point(target_point)
        await executor.wait_for_url_change(old_url)
        await executor.wait_for_load()
        await executor.wait_for_stable(800)
        try:
            new_path, new_parse = await _capture_and_parse()
            dom_elements = await executor.get_dom_elements()
            _merge_dom_inputs(new_parse, dom_elements)
        except Exception:
            new_path, new_parse = screenshot_path, parse_resp
        new_url = await executor.get_url()
        new_finish_debug = _finish_check(request.task, new_parse, new_url)
        return StepResponse(
            action="click",
            target_id=None,
            target_point=target_point,
            action_tool=action_tool,
            action_text=action_text,
            action_key=action_key,
            action_ms=action_ms,
            action_url=action_url,
            action_scroll=action_scroll,
            reason=reason,
            screenshot_path=new_path,
            annotated_image_base64=new_parse.annotated_image_base64,
            elements=new_parse.elements,
            current_url=new_url,
            planner_debug=planner_debug,
            finish_debug=new_finish_debug,
        )
    else:
        return StepResponse(
            action="noop",
            target_id=None,
            target_point=None,
            action_tool=action_tool,
            action_text=action_text,
            action_key=action_key,
            action_ms=action_ms,
            action_url=action_url,
            action_scroll=action_scroll,
            reason="no_target",
            screenshot_path=screenshot_path,
            annotated_image_base64=parse_resp.annotated_image_base64,
            elements=parse_resp.elements,
            current_url=current_url,
            planner_debug=planner_debug,
            finish_debug=finish_debug,
        )

    return StepResponse(
        action="click",
        target_id=target_id,
        target_point=target_point,
        action_tool=action_tool,
        action_text=action_text,
        action_key=action_key,
        action_ms=action_ms,
        action_url=action_url,
        action_scroll=action_scroll,
        reason=reason,
        screenshot_path=screenshot_path,
        annotated_image_base64=parse_resp.annotated_image_base64,
        elements=parse_resp.elements,
        current_url=current_url,
        planner_debug=planner_debug,
        finish_debug=finish_debug,
    )


@app.post("/mark_elements")
async def mark_elements() -> dict:
    """兼容旧前端：返回当前页面标注元素和截图。"""
    try:
        screenshot_path, parse_resp = await _capture_and_parse()
        current_url = await executor.get_url()
        return {
            "elements": [e.model_dump() for e in parse_resp.elements],
            "annotated_image_base64": parse_resp.annotated_image_base64,
            "current_url": current_url,
            "screenshot_path": screenshot_path,
        }
    except Exception as exc:
        logger.exception(f"mark_elements failed: {exc}")
        raise HTTPException(status_code=500, detail=f"mark_elements failed: {exc}") from exc


@app.post("/plan_steps", response_model=PlanStepsResponse)
def plan_steps(request: PlanStepsRequest) -> PlanStepsResponse:
    steps = planner.plan_steps(
        request.task,
        max_steps=request.max_steps or 6,
        annotated_image_base64=request.annotated_image_base64,
    )
    debug = getattr(planner, "_last_plan_debug", None)
    return PlanStepsResponse(steps=steps, debug=debug)


@app.post("/step_once", response_model=StepResponse)
async def step_once(request: StepRequest) -> StepResponse:
    # Execute exactly one step string (request.task) and return screenshot + URL.
    return await step(request)


@app.post("/run")
async def run(request: dict) -> dict:
    max_steps = int(request.get("max_steps") or os.getenv("GUIAGENT_MAX_STEPS", "5"))
    task = request.get("task", "")
    steps: list[StepResponse] = []
    finished = False
    finish_reason = ""

    for _ in range(max_steps):
        # Wait for page to settle before capturing
        await executor.wait_for_load()
        await executor.wait_for_stable(3000)

        # Capture + parse current page
        try:
            screenshot_path, parse_resp = await _capture_and_parse()
            dom_elements = await executor.get_dom_elements()
            _merge_dom_inputs(parse_resp, dom_elements)
        except Exception as exc:
            steps.append(
                StepResponse(
                    action="noop",
                    reason=f"parse_failed:{exc}",
                )
            )
            break

        current_url = await executor.get_url()
        if planner.should_finish(
            task,
            parse_resp.elements,
            current_url=current_url,
            annotated_image_base64=parse_resp.annotated_image_base64,
            image_size=parse_resp.image_size,
        ):
            finished = True
            finish_reason = "llm"
            break

        # Plan + execute one action
        step_resp = await step(StepRequest(task=task))
        steps.append(step_resp)

    if not finished and not finish_reason:
        finish_reason = "max_steps"

    return {
        "finished": finished,
        "finish_reason": finish_reason,
        "steps": [s.model_dump() for s in steps],
    }


@app.post("/task_spec", response_model=ExtractResponse)
def task_spec(request: dict) -> ExtractResponse:
    task = str(request.get("task", "")).strip()
    if not task:
        raise HTTPException(status_code=400, detail="task is required")
    spec, debug = planner.extract_task_spec(task)
    return ExtractResponse(data=spec, debug=debug, spec=spec)


@app.post("/extract", response_model=ExtractResponse)
async def extract(request: ExtractRequest) -> ExtractResponse:
    try:
        screenshot_path, parse_resp = await _capture_and_parse()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Parse failed: {exc}") from exc
    current_url = await executor.get_url()
    data, debug = planner.extract_from_page(
        task=request.task,
        mode=request.mode,
        annotated_image_base64=parse_resp.annotated_image_base64,
        current_url=current_url,
    )
    output_store.set_last_extract(data)
    return ExtractResponse(data=data, debug=debug, spec=request.spec.model_dump())


@app.post("/append_row", response_model=AppendRowResponse)
def append_row(request: AppendRowRequest) -> AppendRowResponse:
    output_store.append_row(request.row)
    return AppendRowResponse(count=len(output_store.rows))


@app.post("/save_output", response_model=SaveOutputResponse)
def save_output(request: dict) -> SaveOutputResponse:
    file_name = request.get("file_name")
    path = output_store.save_excel(file_name)
    return SaveOutputResponse(file=os.path.basename(path))


@app.get("/files", response_model=FileListResponse)
def list_files() -> FileListResponse:
    return FileListResponse(files=output_store.list_files())


@app.get("/schedules")
async def list_schedules() -> dict:
    jobs = await schedule_manager.list_jobs()
    return {"jobs": jobs}


@app.get("/schedules/{job_id}")
async def get_schedule(job_id: str) -> dict:
    job = await schedule_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="schedule not found")
    return job


@app.post("/schedules")
async def create_schedule(request: dict) -> dict:
    payload = dict(request or {})
    task_text = str(payload.get("task") or "").strip()
    if not task_text:
        raise HTTPException(status_code=400, detail="task is required")

    hint = parse_schedule_hint_from_task(task_text) or {}
    for key in ["schedule_type", "time_of_day", "interval_minutes", "run_day"]:
        if key in hint and key not in payload:
            payload[key] = hint[key]

    try:
        job = await schedule_manager.add_job(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job


@app.patch("/schedules/{job_id}")
async def update_schedule(job_id: str, request: dict) -> dict:
    payload = dict(request or {})
    try:
        job = await schedule_manager.update_job(job_id, payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return job


@app.delete("/schedules/{job_id}")
async def delete_schedule(job_id: str) -> dict:
    removed = await schedule_manager.remove_job(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"ok": True, "id": job_id}


@app.post("/schedules/{job_id}/trigger")
async def trigger_schedule(job_id: str) -> dict:
    try:
        job = await schedule_manager.trigger_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc
    return job


@app.get("/files/{name}")
def download_file(name: str):
    path = output_store.get_file_path(name)
    if not path:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path)


@app.post("/run_extraction", response_model=RunExtractionResponse)
async def run_extraction(request: RunExtractionRequest) -> RunExtractionResponse:
    """
    运行完整的数据提取流程

    自动执行：
    1. 解析任务规格
    2. 导航到目标网站
    3. 循环��取列表数据（支持滚动/翻页）
    4. 可选：��入详情页提取详细数据
    5. 保存到Excel

    新增：use_reflection 参数
    - 启用后，翻页时会使用反思机制（验证+重试）
    - 默认启用，确保翻页可靠性

    示例任务:
    - "进入新浪新闻，复制今天的前10条新闻标题和内容"
    - "打开哔哩哔哩，随机进入一个视频，复制最上方的一条评论"
    - "访问https://books.toscrape.com，翻到第二页，提取2本书"
    """
    # 从请求中获取 use_reflection 参数（默认为 True）
    use_reflection = getattr(request, 'use_reflection', True)

    result = await extraction_engine.run_extraction(
        task=request.task,
        max_items=request.max_items,
        strategy=request.strategy,
        use_omniparser=request.use_omniparser,
        use_reflection=use_reflection,
    )
    return RunExtractionResponse(**result)


@app.get("/extraction_progress")
def get_extraction_progress() -> dict:
    """
    获取当前提取进度

    返回:
    {
        "is_extracting": bool,
        "progress": list[dict]
    }
    """
    return {
        "is_extracting": extraction_engine.is_extracting,
        "progress": extraction_engine.current_progress,
    }


@app.post("/run_with_reflection")
async def run_with_reflection(request: dict) -> dict:
    """
    使用反思机制执行任务（类似 Skyvern）

    流程：规划 → 执行 → 验�� → 决策（重试/继续/完成）

    请求参数:
    {
        "task": str,  # 任务描述
        "max_steps": int,  # 最大步数（默认10）
        "max_retries_per_step": int  # 每步最大重试次数（默认3）
    }

    返回:
    {
        "status": "success" | "partial" | "failed",
        "steps": [
            {
                "step_index": int,
                "retry_index": int,
                "description": str,
                "action": str,
                "before_url": str,
                "after_url": str,
                "verification": {...},
                "status": "success" | "failed"
            }
        ],
        "final_url": str,
        "reasoning": str,
        "plan": [str]
    }
    """
    from .reflection_engine import ReflectionEngine

    task = request.get("task", "")
    max_steps = request.get("max_steps", 10)
    max_retries_per_step = request.get("max_retries_per_step", 3)

    if not task:
        raise HTTPException(status_code=400, detail="task is required")

    # 创建反思引擎
    reflection_engine = ReflectionEngine(
        executor=executor,
        planner=planner,
        vlm=planner.vlm,
        max_steps=max_steps,
        max_retries_per_step=max_retries_per_step,
    )

    # 执行任务
    result = await reflection_engine.run_task_with_reflection(task=task)

    return result


@app.get("/run_task_stream")
async def run_task_stream(
    task: str,
    target_url: Optional[str] = None,
    auth_data_file: Optional[str] = None,
    session_id: Optional[str] = None,
    max_steps: int = 20,
    max_retries_per_step: int = 3,
    list_only: bool = False,
    max_items: int = 50,
    max_pages: int = 5
):
    """SSE 流式返回任务执行进度"""
    from .reflection_engine import ReflectionEngine
    from .vlm_service import VLMService

    async def event_generator():
        session = _create_execution_session(prefix=session_id or "stream")
        token_executor = _executor_ctx.set(session.executor)
        token_planner = _planner_ctx.set(session.planner)
        token_output_store = _output_store_ctx.set(session.output_store)
        token_extraction_engine = _extraction_engine_ctx.set(session.extraction_engine)
        try:
            if _is_steel_inspection_task(task, target_url=target_url, auth_data_file=auth_data_file):
                yield f"data: {json.dumps({'type': 'start', 'task': task})}\n\n"

                event_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

                async def _callback(payload: dict):
                    normalized = payload if isinstance(payload, dict) else {"type": "steel_stage", "message": str(payload)}
                    await event_queue.put(f"data: {json.dumps(normalized)}\n\n")

                steel_target_url, resolved_auth_data = _resolve_steel_target_and_auth(
                    task=task,
                    target_url=target_url,
                    auth_data_file=auth_data_file,
                )
                if not steel_target_url:
                    raise RuntimeError("钢铁任务缺少目标网址：请在任务中包含 http(s) 链接，或提供可读的 cookies/auth_data 文件。")

                auto_schedule_payload = _prepare_schedule_payload(
                    task=task,
                    target_url=steel_target_url,
                    auth_data_file=resolved_auth_data,
                    max_items=max_items,
                    max_pages=max_pages,
                    list_only=list_only,
                )
                if auto_schedule_payload:
                    try:
                        created_job = await schedule_manager.add_job(auto_schedule_payload)
                        yield f"data: {json.dumps({'type': 'scheduled', 'status': 'created', 'job': created_job})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'status': 'success', 'reasoning': 'schedule created', 'user_message': '定时任务已创建', 'final_url': '', 'extracted_items': [], 'excel_file': None, 'schedule_job': created_job, 'steel_result': {'scheduled': True}})}\n\n"
                    except Exception as schedule_exc:
                        yield f"data: {json.dumps({'type': 'scheduled', 'status': 'failed', 'message': str(schedule_exc)})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'status': 'failed', 'reasoning': str(schedule_exc), 'user_message': str(schedule_exc), 'final_url': '', 'extracted_items': [], 'excel_file': None, 'steel_result': {'scheduled': True}})}\n\n"
                    return

                pipeline_task = asyncio.create_task(
                    _run_steel_download_pipeline(
                        task=task,
                        target_url=steel_target_url,
                        max_items=max_items,
                        max_pages=max_pages,
                        auth_data_file=resolved_auth_data,
                        stream_callback=_callback,
                    )
                )

                while True:
                    if pipeline_task.done() and event_queue.empty():
                        break
                    try:
                        line = await asyncio.wait_for(event_queue.get(), timeout=0.2)
                        if line:
                            yield line
                    except asyncio.TimeoutError:
                        continue

                steel_result = await pipeline_task

                done_status = steel_result.get("status", "failed")
                reasoning = steel_result.get("reasoning", "")
                excel_file = steel_result.get("final_excel")
                try:
                    final_url = await executor.get_url()
                    if not final_url or str(final_url).lower().startswith("about:blank"):
                        final_url = _build_history_url(steel_target_url)
                except Exception as url_exc:
                    logger.warning(f"Failed to get final URL in steel stream done event: {url_exc}")
                    final_url = _build_history_url(steel_target_url)
                yield f"data: {json.dumps({'type': 'done', 'status': done_status, 'reasoning': reasoning, 'user_message': None if done_status == 'success' else reasoning, 'final_url': final_url, 'extracted_items': [], 'excel_file': excel_file, 'steel_result': steel_result})}\n\n"
                return

            vlm = VLMService()
            engine = ReflectionEngine(
                executor=executor,
                planner=planner,
                vlm=vlm,
                max_steps=max_steps,
                max_retries_per_step=max_retries_per_step
            )

            # 发送开始事件
            yield f"data: {json.dumps({'type': 'start', 'task': task})}\n\n"

            # 检测是否需要提取数据
            extract_keywords = ["提取", "采集", "收集", "抓取", "extract", "collect", "scrape", "复制", "copy"]
            extract_data = any(keyword in task.lower() for keyword in extract_keywords)

            # 规划步骤
            steps_list, plan_debug = vlm.plan_steps(task=task, max_steps=max_steps)
            yield f"data: {json.dumps({'type': 'plan', 'steps': steps_list})}\n\n"

            # 执行步骤
            current_step_index = 0
            all_steps = []
            while current_step_index < len(steps_list):
                step_description = steps_list[current_step_index]

                # 发送步骤开始事件
                yield f"data: {json.dumps({'type': 'step_start', 'index': current_step_index, 'description': step_description})}\n\n"

                # 执行步骤
                step_result = await engine._execute_step_with_retry(
                    task=task,
                    step_description=step_description,
                    step_index=current_step_index
                )

                all_steps.append(step_result)

                # 发送步骤完成事件
                yield f"data: {json.dumps({'type': 'step_complete', 'result': step_result})}\n\n"

                if step_result["status"] == "terminated":
                    break

                current_step_index += 1

            # 数据提取
            extracted_items = []
            excel_file = None
            seen_item_keys: set[str] = set()

            terminated_step = next((step for step in reversed(all_steps) if step.get("status") == "terminated"), None)

            if extract_data and not terminated_step:
                yield f"data: {json.dumps({'type': 'extract_start'})}\n\n"

                target_items = max_items
                requested_fields: Optional[set[str]] = None
                try:
                    spec, _ = planner.extract_task_spec(task)
                    requested_count = int(spec.get("count", max_items))
                    if requested_count > 0:
                        target_items = min(max_items, requested_count)
                    requested_fields = _resolve_requested_fields(task, spec.get("fields", []))
                except Exception as spec_error:
                    logger.warning(f"Failed to parse target item count from task, fallback to max_items={max_items}: {spec_error}")

                logger.info(f"Extraction target count: {target_items} (max_items={max_items})")

                pages_processed = 0
                while pages_processed < max_pages and len(extracted_items) < target_items:
                    # 标记元素
                    dom_result = await executor.mark_page_elements()
                    elements = dom_result.get('elements', [])

                    # 截图
                    screenshot_path = os.path.join(DATA_DIR, f"extract_page_{pages_processed}.png")
                    await executor.screenshot(screenshot_path)

                    with open(screenshot_path, "rb") as f:
                        image_b64 = base64.b64encode(f.read()).decode("ascii")

                    current_url = await executor.get_url()
                    extracted_data, _ = planner.extract_from_page(
                        task=task,
                        mode="list",
                        annotated_image_base64=image_b64,
                        current_url=current_url,
                        elements=elements
                    )

                    items = extracted_data.get("items", []) if isinstance(extracted_data, dict) else []
                    logger.info(f"Extracted {len(items)} items from list page")

                    unique_items: list[dict] = []
                    for item in items:
                        item_key = _build_extracted_item_key(item)
                        if item_key and item_key in seen_item_keys:
                            continue
                        unique_items.append(item)
                    if len(unique_items) != len(items):
                        logger.info(f"Deduplicated list items: {len(items)} -> {len(unique_items)}")
                    items = unique_items

                    remaining_slots = max(0, target_items - len(extracted_items))
                    items_to_process = items[:remaining_slots]

                    # 如果不是 list_only，进入详情页提取
                    if not list_only and items_to_process:
                        for item in items_to_process:
                            if len(extracted_items) >= target_items:
                                break

                            list_page_url = await executor.get_url()
                            detail_url = item.get("url") or item.get("_url")
                            detail_element_id = item.get("element_id") or item.get("_saved_element_id")

                            # URL 不可见时，尝试通过 element_id 从 DOM 里提取 href
                            if not detail_url and detail_element_id:
                                for elem in elements:
                                    if elem.get("id") == detail_element_id:
                                        href = (elem.get("attributes") or {}).get("href")
                                        if href:
                                            detail_url = urljoin(list_page_url, href) if href.startswith("/") else href
                                        break

                            detail_success = False
                            last_detail_error: Optional[Exception] = None
                            candidate_item: Optional[dict] = None

                            for attempt in range(1, 4):
                                try:
                                    if attempt > 1:
                                        logger.info(f"Retrying detail extraction ({attempt}/3)")
                                        await executor.goto(list_page_url)
                                        await asyncio.sleep(0.6)
                                        await executor.wait_for_stable(600)

                                    navigated = False

                                    if detail_url:
                                        if isinstance(detail_url, str) and detail_url.startswith("/"):
                                            detail_url = urljoin(list_page_url, detail_url)
                                        logger.info(f"Navigating to detail page by URL: {detail_url} (attempt {attempt}/3)")
                                        await executor.goto(detail_url)
                                        navigated = True
                                    elif detail_element_id:
                                        logger.info(f"Navigating to detail page by element_id: {detail_element_id} (attempt {attempt}/3)")
                                        click_success = await executor.click_element_by_id(detail_element_id)
                                        if not click_success:
                                            raise RuntimeError(f"Failed to click detail element {detail_element_id}")
                                        navigated = True

                                    if not navigated:
                                        raise RuntimeError("No detail navigation target available")

                                    await asyncio.sleep(1)
                                    await executor.wait_for_stable(1000)

                                    detail_screenshot = os.path.join(DATA_DIR, f"detail_{len(extracted_items)}_{attempt}.png")
                                    await executor.screenshot(detail_screenshot)

                                    with open(detail_screenshot, "rb") as f:
                                        detail_image_b64 = base64.b64encode(f.read()).decode("ascii")

                                    detail_current_url = await executor.get_url()
                                    detail_data, _ = planner.extract_from_page(
                                        task=task,
                                        mode="detail",
                                        annotated_image_base64=detail_image_b64,
                                        current_url=detail_current_url
                                    )

                                    detail_fields = detail_data.get("data", {}) if isinstance(detail_data, dict) else {}
                                    if not detail_fields:
                                        raise RuntimeError("Detail extraction returned empty fields")

                                    merged_item = {**item, **detail_fields}
                                    cleaned_item = _prepare_extracted_item(merged_item, requested_fields)
                                    candidate_item = cleaned_item if cleaned_item else merged_item
                                    detail_success = True
                                    break

                                except Exception as e:
                                    last_detail_error = e
                                    logger.warning(f"Detail extraction attempt {attempt}/3 failed: {e}")

                            if not detail_success:
                                logger.error(f"Failed to extract detail page after 3 retries, fallback to list item: {last_detail_error}")
                                cleaned_item = _prepare_extracted_item(item, requested_fields)
                                candidate_item = cleaned_item if cleaned_item else item

                            if candidate_item is None:
                                candidate_item = item

                            dedup_key = _build_extracted_item_key(candidate_item)
                            if dedup_key and dedup_key in seen_item_keys:
                                logger.info(f"Skip duplicated extracted item in detail branch: {dedup_key}")
                            else:
                                extracted_items.append(candidate_item)
                                if dedup_key:
                                    seen_item_keys.add(dedup_key)

                            try:
                                await executor.go_back()
                                await asyncio.sleep(0.8)
                                await executor.wait_for_stable(500)
                            except Exception as back_error:
                                logger.warning(f"go_back failed after detail extraction, fallback to goto: {back_error}")
                                try:
                                    await executor.goto(list_page_url)
                                    await asyncio.sleep(1)
                                    await executor.wait_for_stable(500)
                                except Exception as goto_back_error:
                                    logger.warning(f"Failed to navigate back to list page: {goto_back_error}")

                            yield f"data: {json.dumps({'type': 'extract_progress', 'count': len(extracted_items)})}\n\n"
                    else:
                        # list_only 模式，直接添加列表数据
                        for item in items_to_process:
                            if len(extracted_items) >= target_items:
                                break
                            cleaned_item = _prepare_extracted_item(item, requested_fields)
                            final_item = cleaned_item if cleaned_item else item
                            dedup_key = _build_extracted_item_key(final_item)
                            if dedup_key and dedup_key in seen_item_keys:
                                continue
                            extracted_items.append(final_item)
                            if dedup_key:
                                seen_item_keys.add(dedup_key)
                        yield f"data: {json.dumps({'type': 'extract_progress', 'count': len(extracted_items)})}\n\n"

                    pages_processed += 1

                    if len(extracted_items) >= target_items:
                        break

                    # 检查是否需要翻页/滚动
                    next_action_raw = str(extracted_data.get("next", "stop") or "stop").strip().lower()
                    if next_action_raw in {"next page", "nextpage", "next"}:
                        next_action = "next_page"
                    elif next_action_raw in {"scroll", "scroll_down", "scroll down"}:
                        next_action = "scroll"
                    else:
                        next_action = next_action_raw

                    should_try_scroll = (
                        next_action == "scroll"
                        or (next_action in {"stop", "", "continue", "more"} and len(extracted_items) < target_items)
                    )

                    logger.info(
                        f"Pagination decision: action={next_action_raw}->{next_action}, "
                        f"collected={len(extracted_items)}/{target_items}, page={pages_processed}/{max_pages}"
                    )

                    if next_action == "next_page":
                        next_page_element_id = extracted_data.get("next_page_element_id")
                        paged = False

                        if next_page_element_id:
                            try:
                                paged = await executor.click_element_by_id(next_page_element_id)
                                if paged:
                                    await asyncio.sleep(1)
                                    await executor.wait_for_stable(1200)
                            except Exception as click_error:
                                logger.warning(f"Failed to click next page element {next_page_element_id}: {click_error}")
                                paged = False

                        if not paged:
                            try:
                                scroll_y = await executor.scroll_to_next_page(need_overlap=True)
                                paged = bool(scroll_y and scroll_y > 0)
                                if paged:
                                    await asyncio.sleep(1)
                            except Exception as scroll_error:
                                logger.warning(f"Fallback scroll pagination failed: {scroll_error}")
                                paged = False

                        if not paged:
                            logger.info("No further pagination available, stop extraction")
                            break
                    elif should_try_scroll:
                        paged = False
                        try:
                            scroll_y = await executor.scroll_to_next_page(need_overlap=True)
                            paged = bool(scroll_y and scroll_y > 0)
                            if paged:
                                await asyncio.sleep(1)
                                await executor.wait_for_stable(800)
                        except Exception as scroll_error:
                            logger.warning(f"Scroll pagination failed: {scroll_error}")
                            paged = False

                        if not paged:
                            logger.info("Scroll did not advance page, stop extraction")
                            break
                    else:
                        break

                # 保存到 Excel
                if extracted_items:
                    output_store.rows = []
                    for item in extracted_items:
                        cleaned_item = _prepare_extracted_item(item, requested_fields)
                        output_store.append_row(cleaned_item)
                    excel_path = output_store.save_excel()
                    excel_file = os.path.basename(excel_path)
                    yield f"data: {json.dumps({'type': 'extract_done', 'count': len(extracted_items), 'file': excel_file})}\n\n"

            # 发送完成事件
            try:
                final_url = await executor.get_url()
            except Exception as url_exc:
                logger.warning(f"Failed to get final URL in stream done event: {url_exc}")
                final_url = ""
            if terminated_step:
                done_status = "terminated"
                done_reasoning = terminated_step.get("termination_reason") or terminated_step.get("verification", {}).get("reasoning", "")
                done_user_message = terminated_step.get("user_message")
            elif any(step.get("status") == "failed" for step in all_steps):
                done_status = "failed"
                done_reasoning = "One or more steps failed"
                done_user_message = None
            else:
                done_status = "success"
                done_reasoning = "Task completed"
                done_user_message = None

            yield f"data: {json.dumps({'type': 'done', 'status': done_status, 'reasoning': done_reasoning, 'user_message': done_user_message, 'final_url': final_url, 'extracted_items': extracted_items, 'excel_file': excel_file})}\n\n"

        except Exception as e:
            classified = _classify_runtime_exception(e)
            if classified:
                logger.warning(f"Stream runtime classified error [{classified.get('code')}]: {classified.get('message')}")
                try:
                    final_url = await executor.get_url()
                except Exception:
                    final_url = ""
                yield f"data: {json.dumps({'type': 'error', 'error_code': classified.get('code'), 'message': classified.get('message'), 'user_message': classified.get('user_message'), 'hint': classified.get('hint'), 'retriable': classified.get('retriable', False)})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'status': 'failed', 'reasoning': classified.get('message'), 'user_message': classified.get('user_message'), 'final_url': final_url, 'extracted_items': [], 'excel_file': None, 'error_code': classified.get('code')})}\n\n"
            else:
                logger.exception("Stream error")
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            _extraction_engine_ctx.reset(token_extraction_engine)
            _output_store_ctx.reset(token_output_store)
            _planner_ctx.reset(token_planner)
            _executor_ctx.reset(token_executor)
            await _close_execution_session(session)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/run_task")
async def run_task(request: dict) -> dict:
    """
    Unified non-stream task execution API.

    Notes:
    - UI primarily uses `/run_task_stream`.
    - This endpoint is kept for backward compatibility and debugging.
    - Return schema follows stream done semantics where possible.
    """
    from .reflection_engine import ReflectionEngine
    from .vlm_service import VLMService

    task = request.get("task", "")
    max_steps = request.get("max_steps", 20)
    max_retries_per_step = request.get("max_retries_per_step", 3)
    extract_data = request.get("extract_data", None)  # None means auto detect
    max_items = request.get("max_items", 50)
    max_pages = request.get("max_pages", 5)
    list_only = request.get("list_only", False)
    session_id = request.get("session_id")

    session = _create_execution_session(prefix=session_id or "run")
    token_executor = _executor_ctx.set(session.executor)
    token_planner = _planner_ctx.set(session.planner)
    token_output_store = _output_store_ctx.set(session.output_store)
    token_extraction_engine = _extraction_engine_ctx.set(session.extraction_engine)

    try:
        if not task:
            raise HTTPException(status_code=400, detail="task is required")

        logger.info(f"Starting unified task: {task}")

        request_target_url = request.get("target_url")
        request_auth_data_file = request.get("auth_data_file")
        if _is_steel_inspection_task(
            task,
            target_url=request_target_url,
            auth_data_file=request_auth_data_file,
        ):
            target_url, resolved_auth_data = _resolve_steel_target_and_auth(
                task=task,
                target_url=request_target_url,
                auth_data_file=request_auth_data_file,
            )
            if not target_url:
                return {
                    "status": "failed",
                    "steps": [],
                    "extracted_items": [],
                    "excel_file": None,
                    "termination_reason": "missing_target_url",
                    "user_message": "Steel task missing target URL: include http(s) URL in task text, or provide readable cookies/auth_data file.",
                    "final_url": await executor.get_url(),
                    "reasoning": "missing target url",
                    "plan": [],
                    "steel_result": None,
                }

            auto_schedule_payload = _prepare_schedule_payload(
                task=task,
                target_url=target_url,
                auth_data_file=resolved_auth_data,
                max_items=max_items,
                max_pages=max_pages,
                list_only=list_only,
                explicit_payload=request.get("schedule"),
            )
            if auto_schedule_payload:
                try:
                    created_job = await schedule_manager.add_job(auto_schedule_payload)
                    return {
                        "status": "scheduled",
                        "steps": [],
                        "extracted_items": [],
                        "excel_file": None,
                        "termination_reason": None,
                        "user_message": "Schedule created",
                        "final_url": "",
                        "reasoning": "schedule created",
                        "plan": [],
                        "steel_result": None,
                        "schedule_job": created_job,
                    }
                except ValueError as schedule_error:
                    return {
                        "status": "failed",
                        "steps": [],
                        "extracted_items": [],
                        "excel_file": None,
                        "termination_reason": "schedule_create_failed",
                        "user_message": str(schedule_error),
                        "final_url": "",
                        "reasoning": str(schedule_error),
                        "plan": [],
                        "steel_result": None,
                    }

            steel_result = await _run_steel_download_pipeline(
                task=task,
                target_url=target_url,
                max_items=max_items,
                max_pages=max_pages,
                auth_data_file=resolved_auth_data,
            )

            done_status = steel_result.get("status", "failed")
            done_reasoning = steel_result.get("reasoning", "")
            done_excel = steel_result.get("final_excel")

            return {
                "status": "success" if done_status == "success" else "failed",
                "steps": [],
                "extracted_items": [],
                "excel_file": done_excel,
                "termination_reason": None if done_status == "success" else "steel_pipeline_failed",
                "user_message": None if done_status == "success" else done_reasoning,
                "final_url": await executor.get_url(),
                "reasoning": done_reasoning,
                "plan": [],
                "steel_result": steel_result,
            }

        if not hasattr(planner, '_vlm') or planner._vlm is None:
            planner._vlm = VLMService()

        reflection_engine = ReflectionEngine(
            executor=executor,
            planner=planner,
            vlm=planner._vlm,
            max_steps=max_steps,
            max_retries_per_step=max_retries_per_step,
        )

        if extract_data is None:
            extract_keywords = ["\u63d0\u53d6", "\u91c7\u96c6", "\u6536\u96c6", "\u6293\u53d6", "extract", "collect", "scrape", "\u590d\u5236", "copy"]
            extract_data = any(keyword in task.lower() for keyword in extract_keywords)

        logger.info(f"Extract data mode: {extract_data}, list_only: {list_only}")

        try:
            result = await reflection_engine.run_task_with_reflection(task=task)

            terminated_steps = [s for s in result["steps"] if s.get("status") == "terminated"]
            if terminated_steps:
                last_terminated = terminated_steps[-1]
                return {
                    "status": "terminated",
                    "steps": result["steps"],
                    "extracted_items": [],
                    "excel_file": None,
                    "termination_reason": last_terminated.get("termination_reason"),
                    "user_message": last_terminated.get("user_message"),
                    "final_url": result["final_url"],
                    "reasoning": result["reasoning"],
                }

            extracted_items = []
            excel_file = None

            if extract_data:
                logger.info("Starting data extraction...")
                requested_fields: Optional[set[str]] = None
                target_items = max_items

                try:
                    spec, _ = planner.extract_task_spec(task)
                    requested_count = int(spec.get("count", max_items))
                    if requested_count > 0:
                        target_items = min(max_items, requested_count)
                    requested_fields = _resolve_requested_fields(task, spec.get("fields", []))
                except Exception as spec_error:
                    logger.warning(f"Failed to parse extraction spec in run_task: {spec_error}")

                logger.info(f"run_task extraction target count: {target_items}")

                pages_processed = 0
                while pages_processed < max_pages and len(extracted_items) < target_items:
                    dom_result = await executor.mark_page_elements()
                    elements = dom_result.get('elements', [])

                    screenshot_path = os.path.join(DATA_DIR, timestamp_name("extract"))
                    await executor.screenshot(screenshot_path)

                    with open(screenshot_path, "rb") as f:
                        image_base64 = base64.b64encode(f.read()).decode("ascii")

                    current_url = await executor.get_url()

                    extracted_data, extract_debug = planner.extract_from_page(
                        task=task,
                        mode="list",
                        annotated_image_base64=image_base64,
                        current_url=current_url,
                        elements=elements,
                    )

                    items = extracted_data.get("items", [])
                    logger.info(f"Extracted {len(items)} items from current page")

                    for item in items:
                        if len(extracted_items) >= target_items:
                            break
                        cleaned_item = _prepare_extracted_item(item, requested_fields)
                        extracted_items.append(cleaned_item if cleaned_item else item)

                    logger.info(f"Total collected: {len(extracted_items)}/{target_items}")

                    next_action = extracted_data.get("next", "stop")
                    if next_action == "next_page" and len(extracted_items) < target_items:
                        logger.info("Need to go to next page")

                        next_page_element_id = extracted_data.get("next_page_element_id")
                        if next_page_element_id:
                            pagination_step = await reflection_engine._execute_step_with_retry(
                                task="go to next page",
                                step_description="click next page button",
                                step_index=len(result["steps"]),
                            )

                            if pagination_step["status"] == "success":
                                logger.info("Pagination successful")
                                pages_processed += 1
                                continue
                            else:
                                logger.error(f"Pagination failed: {pagination_step.get('verification', {}).get('reasoning', '')}")
                                break
                        else:
                            logger.warning("No next page button found")
                            break
                    else:
                        logger.info("Extraction complete")
                        break

                if extracted_items:
                    logger.info(f"Saving {len(extracted_items)} items to Excel")
                    output_store.reset()

                    for item in extracted_items:
                        cleaned_item = _prepare_extracted_item(item, requested_fields)
                        output_store.append_row(cleaned_item)

                    excel_path = output_store.save_excel()
                    excel_file = os.path.basename(excel_path)
                    logger.info(f"Saved to {excel_file}")

            return {
                "status": result["status"],
                "steps": result["steps"],
                "extracted_items": extracted_items,
                "excel_file": excel_file,
                "termination_reason": None,
                "user_message": None,
                "final_url": result["final_url"],
                "reasoning": result["reasoning"],
                "plan": result.get("plan", []),
            }

        except Exception as e:
            classified = _classify_runtime_exception(e)
            if classified:
                logger.warning(f"Task runtime classified error [{classified.get('code')}]: {classified.get('message')}")
                return {
                    "status": "failed",
                    "steps": [],
                    "extracted_items": [],
                    "excel_file": None,
                    "termination_reason": classified.get("code", "exception"),
                    "user_message": classified.get("user_message"),
                    "hint": classified.get("hint"),
                    "final_url": await executor.get_url(),
                    "reasoning": classified.get("message") or str(e),
                }

            logger.exception(f"Task execution failed: {e}")
            return {
                "status": "failed",
                "steps": [],
                "extracted_items": [],
                "excel_file": None,
                "termination_reason": "exception",
                "user_message": f"Task execution failed: {str(e)}",
                "final_url": await executor.get_url(),
                "reasoning": str(e),
            }
    finally:
        _extraction_engine_ctx.reset(token_extraction_engine)
        _output_store_ctx.reset(token_output_store)
        _planner_ctx.reset(token_planner)
        _executor_ctx.reset(token_executor)
        await _close_execution_session(session)


@app.post("/run_steel_inspection_task")
async def run_steel_inspection_task(request: dict) -> dict:
    """
    专门用于钢铁异常采集任务的 API

    整合了：规划、执行、反思、滚动、翻页、数据提取、图片下载、Excel导出

    请求参数:
    {
        "task": str,  # 任务描述，例如："采集2025-12-25的所有异常钢铁数据"
        "target_url": str,  # 目标网站URL
        "date": str,  # 日期，例如："2025-12-25"
        "filter_type": str,  # 筛选类型，例如："异常"
        "max_items": int,  # 最大采集数量（默认100）
        "download_images": bool,  # 是否下载图片（默认True）
        "max_pages": int,  # 最大翻页数（默认10）
    }

    返回:
    {
        "status": "success" | "failed",
        "items_collected": int,
        "excel_file": str,
        "images_downloaded": int,
        "execution_log": [...]
    }
    """
    if not bool(request.get("legacy_flow")):
        task_text = (request.get("task") or "").strip()
        target_url_value = (request.get("target_url") or "").strip()
        auth_data_file_value = (request.get("auth_data_file") or "").strip() or None
        max_items_value = int(request.get("max_items", 100) or 100)
        max_pages_value = int(request.get("max_pages", 10) or 10)
        session_id_value = (request.get("session_id") or "").strip()

        date_value = (request.get("date") or "").strip()
        start_date_value = (request.get("start_date") or "").strip()
        end_date_value = (request.get("end_date") or "").strip()
        if date_value and (not start_date_value and not end_date_value):
            start_date_value = f"{date_value} 00:00:00"
            end_date_value = f"{date_value} 23:59:59"

        filter_type_value = (request.get("filter_type") or "").strip()
        image_mode_value = (request.get("image_mode") or "").strip()

        task_parts: list[str] = [task_text] if task_text else []
        if filter_type_value:
            task_parts.append(f"筛选打包状态为{filter_type_value}")
        if image_mode_value:
            task_parts.append(f"下载{image_mode_value}")
        normalized_task = "，".join([part for part in task_parts if part]).strip()
        if not normalized_task:
            normalized_task = "进入钢铁历史记录页，设置日期，按需求筛选，下载Excel和图片并生成带图Excel"

        resolved_target_url = _extract_target_url(normalized_task, target_url_value)
        resolved_auth_data = _resolve_auth_data_file(
            auth_data_file=auth_data_file_value or _extract_auth_data_path_from_task(normalized_task),
            target_url=resolved_target_url,
        )
        if not resolved_target_url:
            resolved_target_url = _extract_target_url_from_auth_data(
                auth_data_file=resolved_auth_data,
                fallback_url=target_url_value,
            )
        if not resolved_target_url:
            raise HTTPException(status_code=400, detail="steel task missing target_url")

        execution_log: list[dict] = []

        async def _capture_stage(payload: dict) -> None:
            if isinstance(payload, dict):
                execution_log.append(payload)

        session = _create_execution_session(prefix=session_id_value or "steel")
        token_executor = _executor_ctx.set(session.executor)
        token_planner = _planner_ctx.set(session.planner)
        token_output_store = _output_store_ctx.set(session.output_store)
        token_extraction_engine = _extraction_engine_ctx.set(session.extraction_engine)

        try:
            steel_result = await _run_steel_download_pipeline(
                task=normalized_task,
                target_url=resolved_target_url,
                max_items=max_items_value,
                max_pages=max_pages_value,
                auth_data_file=resolved_auth_data,
                stream_callback=_capture_stage,
                start_date_override=start_date_value or None,
                end_date_override=end_date_value or None,
            )

            done_status = str(steel_result.get("status") or "failed").lower()
            done_reasoning = str(steel_result.get("reasoning") or "")
            excel_file = steel_result.get("final_excel")

            try:
                final_url = await executor.get_url()
                if not final_url or str(final_url).lower().startswith("about:blank"):
                    final_url = _build_history_url(resolved_target_url)
            except Exception:
                final_url = _build_history_url(resolved_target_url)

            response: dict[str, Any] = {
                "status": "success" if done_status == "success" else "failed",
                "items_collected": int(steel_result.get("items_collected") or 0),
                "excel_file": excel_file,
                "images_downloaded": int(steel_result.get("images_downloaded") or 0),
                "pages_processed": int(steel_result.get("pages_processed") or 0),
                "execution_log": execution_log,
                "final_url": final_url,
                "reasoning": done_reasoning,
                "steel_result": steel_result,
            }
            if done_status != "success":
                response["error"] = done_reasoning
            return response
        finally:
            _extraction_engine_ctx.reset(token_extraction_engine)
            _output_store_ctx.reset(token_output_store)
            _planner_ctx.reset(token_planner)
            _executor_ctx.reset(token_executor)
            await _close_execution_session(session)

    from .reflection_engine import ReflectionEngine
    from .vlm_service import VLMService

    task = request.get("task", "")
    target_url = request.get("target_url", "")
    date = request.get("date", "")
    filter_type = str(request.get("filter_type", "") or "").strip()
    max_items = request.get("max_items", 100)
    download_images = request.get("download_images", True)
    max_pages = request.get("max_pages", 10)

    if not task and not target_url:
        raise HTTPException(status_code=400, detail="task or target_url is required")

    logger.info(f"Starting steel inspection task: {task}")
    logger.info(f"Parameters: date={date}, filter_type={filter_type}, max_items={max_items}")

    # 初始化 VLM 服务
    if not hasattr(planner, '_vlm') or planner._vlm is None:
        planner._vlm = VLMService()

    # 创建反思引擎
    reflection_engine = ReflectionEngine(
        executor=executor,
        planner=planner,
        vlm=planner._vlm,
        max_steps=50,  # 钢铁任务可能需要更多步骤
        max_retries_per_step=3,
    )

    collected_items = []
    execution_log = []
    images_downloaded = 0

    try:
        # 阶段1: 导航到目标网站
        if target_url:
            logger.info(f"Navigating to {target_url}")
            await executor.goto(target_url)
            await executor.wait_for_load()
            await executor.wait_for_stable(2000)
            execution_log.append({"stage": "navigation", "status": "success", "url": target_url})

        # 阶段2: 构建任务步骤
        # 根据参数自动生成任务描述
        if not task:
            filter_part = f"，筛选{filter_type}记录" if filter_type else ""
            task = f"在打包带检验系统中，选择日期{date}{filter_part}，提取所有数据并下载图片"

        # 使用反思引擎规划和执行任务
        logger.info("Planning task steps...")
        steps_list, plan_debug = reflection_engine.vlm.plan_steps(
            task=task,
            max_steps=20,
        )
        logger.info(f"Planned steps: {steps_list}")
        execution_log.append({"stage": "planning", "status": "success", "steps": steps_list})

        # 阶段3: 执行步骤循环（带数据提取）
        current_step_index = 0
        pages_processed = 0

        while current_step_index < len(steps_list) and pages_processed < max_pages:
            step_description = steps_list[current_step_index]
            logger.info(f"Executing step {current_step_index + 1}: {step_description}")

            # 执行步骤（带反思）
            step_result = await reflection_engine._execute_step_with_retry(
                task=task,
                step_description=step_description,
                step_index=current_step_index,
            )

            execution_log.append(step_result)

            # 如果步骤涉及数据提取
            if any(keyword in step_description.lower() for keyword in ["提取", "extract", "采集", "收集"]):
                logger.info("Extracting data from current page...")

                # 标记元素
                dom_result = await executor.mark_page_elements()
                elements = dom_result.get('elements', [])

                # 截图
                screenshot_path = os.path.join(DATA_DIR, timestamp_name("extract"))
                await executor.screenshot(screenshot_path)

                with open(screenshot_path, "rb") as f:
                    image_base64 = base64.b64encode(f.read()).decode("ascii")

                current_url = await executor.get_url()

                # 使用 VLM 提取数据
                extracted_data, extract_debug = planner.extract_from_page(
                    task=task,
                    mode="list",
                    annotated_image_base64=image_base64,
                    current_url=current_url,
                    elements=elements,
                )

                items = extracted_data.get("items", [])
                logger.info(f"Extracted {len(items)} items from current page")

                # 通用状态过滤（如用户指定 filter_type）
                if filter_type:
                    filtered_items = _filter_items_by_status(items, str(filter_type))
                    logger.info(f"Filtered by status '{filter_type}': {len(items)} -> {len(filtered_items)}")
                    items = filtered_items

                # 添加到收集列表
                for item in items:
                    if len(collected_items) >= max_items:
                        break

                    # 下载图片（如果需要）
                    if download_images and item.get("image_url"):
                        try:
                            # 这里可以添加图片下载逻辑
                            # 暂时跳过，因为需要实现下载功能
                            pass
                        except Exception as e:
                            logger.error(f"Failed to download image: {e}")

                    collected_items.append(item)

                logger.info(f"Total collected: {len(collected_items)}/{max_items}")

                # 检查是否需要翻页
                next_action = extracted_data.get("next", "stop")
                if next_action == "next_page" and len(collected_items) < max_items:
                    logger.info("Need to go to next page")

                    # 使用反思机制翻页
                    next_page_element_id = extracted_data.get("next_page_element_id")
                    if next_page_element_id:
                        logger.info(f"Clicking next page button: {next_page_element_id}")

                        # 捕获翻页前状态
                        before_url = await executor.get_url()
                        before_screenshot = await reflection_engine._capture_screenshot()
                        before_elements = await reflection_engine._get_page_elements()

                        # 点击翻页按钮
                        success = await executor.click_element_by_id(next_page_element_id)

                        if success:
                            # 等待页面加载
                            await asyncio.sleep(2)
                            await executor.wait_for_stable(2000)

                            # 捕获翻页后状态
                            after_url = await executor.get_url()
                            after_screenshot = await reflection_engine._capture_screenshot()
                            after_elements = await reflection_engine._get_page_elements()

                            # 验证翻页是否成功
                            verification, _ = reflection_engine.vlm.verify_step_success(
                                task="翻页到下一页",
                                step_description="click next page button",
                                action_taken="click",
                                before_url=before_url,
                                after_url=after_url,
                                before_image_base64=before_screenshot,
                                after_image_base64=after_screenshot,
                                elements_before=before_elements,
                                elements_after=after_elements,
                            )

                            if verification.get("success", False):
                                logger.info("Pagination successful")
                                pages_processed += 1
                                # 继续提取下一页（不增加 step_index）
                                continue
                            else:
                                logger.error(f"Pagination failed: {verification.get('reasoning', '')}")
                                break
                        else:
                            logger.error("Failed to click next page button")
                            break
                    else:
                        logger.warning("No next page button found")
                        break
                elif next_action == "stop" or len(collected_items) >= max_items:
                    logger.info("Extraction complete")
                    break

            # 继续下一步
            if step_result["status"] == "success":
                current_step_index += 1
            else:
                logger.error(f"Step failed: {step_result.get('verification', {}).get('reasoning', '')}")
                break

        # 阶段4: 保存到 Excel
        logger.info(f"Saving {len(collected_items)} items to Excel")
        output_store.reset()

        for item in collected_items:
            # 清理内部字段
            cleaned_item = {k: v for k, v in item.items() if not k.startswith('_')}
            output_store.append_row(cleaned_item)

        excel_file = output_store.save_excel(f"steel_inspection_{date}.xlsx")
        logger.info(f"Saved to {excel_file}")

        execution_log.append({
            "stage": "save_excel",
            "status": "success",
            "file": excel_file,
            "items": len(collected_items)
        })

        return {
            "status": "success",
            "items_collected": len(collected_items),
            "excel_file": os.path.basename(excel_file),
            "images_downloaded": images_downloaded,
            "pages_processed": pages_processed,
            "execution_log": execution_log,
        }

    except Exception as e:
        logger.exception(f"Steel inspection task failed: {e}")
        execution_log.append({
            "stage": "error",
            "status": "failed",
            "error": str(e)
        })

        return {
            "status": "failed",
            "items_collected": len(collected_items),
            "excel_file": None,
            "images_downloaded": images_downloaded,
            "pages_processed": pages_processed,
            "execution_log": execution_log,
            "error": str(e),
        }
