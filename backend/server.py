from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin

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
planner = Planner()
executor = Executor()

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
output_store = OutputStore(OUTPUT_DIR)

extraction_engine = ExtractionEngine(
    executor=executor,
    parser_service=None,  # 延迟初始化
    planner=planner,
    output_store=output_store,
    data_dir=DATA_DIR,
)


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
        "title": "title",
        "书名": "title",
        "价格": "price",
        "price": "price",
        "作者": "author",
        "author": "author",
        "vote": "votes",
        "votes": "votes",
        "rating": "votes",
        "评分": "votes",
        "链接": "url",
        "link": "url",
        "url": "url",
        "摘要": "summary",
        "summary": "summary",
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
        "价格": "price",
        "price": "price",
        "作者": "author",
        "author": "author",
        "评分": "votes",
        "投票": "votes",
        "votes": "votes",
        "rating": "votes",
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
        try:
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

                            try:
                                navigated = False

                                if detail_url:
                                    if isinstance(detail_url, str) and detail_url.startswith("/"):
                                        detail_url = urljoin(list_page_url, detail_url)
                                    logger.info(f"Navigating to detail page by URL: {detail_url}")
                                    await executor.goto(detail_url)
                                    navigated = True
                                elif detail_element_id:
                                    logger.info(f"Navigating to detail page by element_id: {detail_element_id}")
                                    click_success = await executor.click_element_by_id(detail_element_id)
                                    if not click_success:
                                        raise RuntimeError(f"Failed to click detail element {detail_element_id}")
                                    navigated = True

                                if not navigated:
                                    cleaned_item = _prepare_extracted_item(item, requested_fields)
                                    extracted_items.append(cleaned_item if cleaned_item else item)
                                    yield f"data: {json.dumps({'type': 'extract_progress', 'count': len(extracted_items)})}\n\n"
                                    continue

                                await asyncio.sleep(1)
                                await executor.wait_for_stable(1000)

                                detail_screenshot = os.path.join(DATA_DIR, f"detail_{len(extracted_items)}.png")
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
                                merged_item = {**item, **detail_fields}
                                cleaned_item = _prepare_extracted_item(merged_item, requested_fields)
                                extracted_items.append(cleaned_item if cleaned_item else merged_item)

                            except Exception as e:
                                logger.error(f"Failed to extract detail page: {e}")
                                cleaned_item = _prepare_extracted_item(item, requested_fields)
                                extracted_items.append(cleaned_item if cleaned_item else item)
                            finally:
                                try:
                                    await executor.goto(list_page_url)
                                    await asyncio.sleep(1)
                                    await executor.wait_for_stable(500)
                                except Exception as back_error:
                                    logger.warning(f"Failed to navigate back to list page: {back_error}")

                            yield f"data: {json.dumps({'type': 'extract_progress', 'count': len(extracted_items)})}\n\n"
                    else:
                        # list_only 模式，直接添加列表数据
                        for item in items_to_process:
                            if len(extracted_items) >= target_items:
                                break
                            cleaned_item = _prepare_extracted_item(item, requested_fields)
                            extracted_items.append(cleaned_item if cleaned_item else item)
                        yield f"data: {json.dumps({'type': 'extract_progress', 'count': len(extracted_items)})}\n\n"

                    pages_processed += 1

                    if len(extracted_items) >= target_items:
                        break

                    # 检查是否需要翻页
                    next_action = extracted_data.get("next", "stop")
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
            final_url = await executor.get_url()
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
            logger.exception("Stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/run_task")
async def run_task(request: dict) -> dict:
    """
    统一任务执行 API（合并 Run 和 Extract Data）

    功能：
    - 规划任务步骤（VLM 自动拆分）
    - 逐步执行（click/type/scroll/goto）
    - 每步反思验证（成功/重试/终止）
    - 自动处理特殊情况（登录、广告、验证码、错误页面）
    - 数据提取（如果任务涉及提取）
    - 自动翻页（带反思验证）
    - 导出到 Excel

    请求参数:
    {
        "task": str,  # 任务描述，例如："打开百度，搜索Python，提取前5个结果"
        "max_steps": int,  # 最大步骤数（默认20）
        "max_retries_per_step": int,  # 每步最大重试次数（默认3）
        "extract_data": bool,  # 是否提取数据（默认自动检测）
        "max_items": int,  # 最大提取数量（默认50）
        "max_pages": int,  # 最大翻页数（默认5）
    }

    返回:
    {
        "status": "success" | "failed" | "terminated",
        "steps": [...],  # 执行步骤详情
        "extracted_items": [...],  # 提取的数据（如果有）
        "excel_file": str | null,  # Excel 文件名（如果有）
        "termination_reason": str | null,  # 终止原因（login_required/captcha/error_page）
        "user_message": str | null,  # 给用户的消息
        "final_url": str,
        "reasoning": str
    }
    """
    from .reflection_engine import ReflectionEngine
    from .vlm_service import VLMService

    task = request.get("task", "")
    max_steps = request.get("max_steps", 20)
    max_retries_per_step = request.get("max_retries_per_step", 3)
    extract_data = request.get("extract_data", None)  # None = 自动检测
    max_items = request.get("max_items", 50)
    max_pages = request.get("max_pages", 5)
    list_only = request.get("list_only", False)

    if not task:
        raise HTTPException(status_code=400, detail="task is required")

    logger.info(f"Starting unified task: {task}")

    # 初始化 VLM 服务
    if not hasattr(planner, '_vlm') or planner._vlm is None:
        planner._vlm = VLMService()

    # 创建反思引擎
    reflection_engine = ReflectionEngine(
        executor=executor,
        planner=planner,
        vlm=planner._vlm,
        max_steps=max_steps,
        max_retries_per_step=max_retries_per_step,
    )

    # 自动检测是否需要提取数据
    if extract_data is None:
        extract_keywords = ["提取", "采集", "收集", "抓取", "extract", "collect", "scrape", "复制", "copy"]
        extract_data = any(keyword in task.lower() for keyword in extract_keywords)

    logger.info(f"Extract data mode: {extract_data}, list_only: {list_only}")

    try:
        # 执行任务（带反思）
        result = await reflection_engine.run_task_with_reflection(task=task)

        # 检查是否被终止
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

        # 如果需要提取数据
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

                # 添加到收集列表
                for item in items:
                    if len(extracted_items) >= target_items:
                        break
                    cleaned_item = _prepare_extracted_item(item, requested_fields)
                    extracted_items.append(cleaned_item if cleaned_item else item)

                logger.info(f"Total collected: {len(extracted_items)}/{target_items}")

                # 检查是否需要翻页
                next_action = extracted_data.get("next", "stop")
                if next_action == "next_page" and len(extracted_items) < target_items:
                    logger.info("Need to go to next page")

                    # 使用反思机制翻页
                    next_page_element_id = extracted_data.get("next_page_element_id")
                    if next_page_element_id:
                        # 执行翻页步骤（带反思）
                        pagination_step = await reflection_engine._execute_step_with_retry(
                            task="翻页到下一页",
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

            # 保存到 Excel
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
        logger.exception(f"Task execution failed: {e}")
        return {
            "status": "failed",
            "steps": [],
            "extracted_items": [],
            "excel_file": None,
            "termination_reason": "exception",
            "user_message": f"任务执行失败: {str(e)}",
            "final_url": await executor.get_url(),
            "reasoning": str(e),
        }


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
    from .reflection_engine import ReflectionEngine
    from .vlm_service import VLMService

    task = request.get("task", "")
    target_url = request.get("target_url", "")
    date = request.get("date", "")
    filter_type = request.get("filter_type", "异常")
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
            task = f"在打包带检验系统中，选择日期{date}，筛选{filter_type}记录，提取所有数据并下载图片"

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

                # 过滤异常数据（如果需要）
                if filter_type == "异常":
                    items = [item for item in items if "异常" in str(item.get("status", "")) or "❌" in str(item.get("status", ""))]
                    logger.info(f"Filtered to {len(items)} abnormal items")

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
