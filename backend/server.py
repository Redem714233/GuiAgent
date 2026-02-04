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

from backend.executor import Executor
from backend.omniparser_service import OmniParserService
from backend.planner import Planner
from backend.schemas import ParseRequest, ParseResponse, PlanRequest, PlanResponse, StepRequest, StepResponse
from backend.schemas import Element, PlanStepsRequest, PlanStepsResponse
from backend.schemas import TaskSpec, ExtractRequest, ExtractResponse, AppendRowRequest, AppendRowResponse
from backend.schemas import SaveOutputResponse, FileListResponse, RunExtractionRequest, RunExtractionResponse
from backend.output_store import OutputStore
from backend.storage import ensure_dir, timestamp_name
from backend.extraction_engine import ExtractionEngine

load_dotenv()

app = FastAPI(title="GUIAgent Local")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

parser_service = OmniParserService()
planner = Planner()
executor = Executor()

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data", "screenshots"))
ensure_dir(DATA_DIR)

OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data", "outputs"))
ensure_dir(OUTPUT_DIR)
output_store = OutputStore(OUTPUT_DIR)

extraction_engine = ExtractionEngine(
    executor=executor,
    parser_service=parser_service,
    planner=planner,
    output_store=output_store,
    data_dir=DATA_DIR,
)


@app.post("/parse", response_model=ParseResponse)
def parse(request: ParseRequest) -> ParseResponse:
    return parser_service.parse(request)


@app.post("/plan", response_model=PlanResponse)
def plan(request: PlanRequest) -> PlanResponse:
    return planner.plan(request)


async def _capture_and_parse() -> tuple[str, ParseResponse]:
    screenshot_path = os.path.join(DATA_DIR, timestamp_name("screenshot"))
    await executor.screenshot(screenshot_path)
    with open(screenshot_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")
    parse_resp = parser_service.parse(ParseRequest(image_base64=image_b64, use_paddleocr=True))
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
    3. 循环提取列表数据（支持滚动/翻页）
    4. 可选：进入详情页提取详细数据
    5. 保存到Excel

    示例任务:
    - "进入新浪新闻，复制今天的前10条新闻标题和内容"
    - "打开哔哩哔哩，随机进入一个视频，复制最上方的一条评论"
    """
    result = await extraction_engine.run_extraction(
        task=request.task,
        max_items=request.max_items,
        strategy=request.strategy,
        use_omniparser=request.use_omniparser,
    )
    return RunExtractionResponse(**result)

