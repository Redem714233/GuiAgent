from __future__ import annotations

import base64
import json
import os
import re
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

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
