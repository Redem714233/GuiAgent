from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class Element(BaseModel):
    id: int
    type: str
    content: str = ""
    center: Tuple[int, int]
    bbox: Tuple[int, int, int, int]


class ParseResponse(BaseModel):
    annotated_image_base64: str
    elements: List[Element]
    image_size: Tuple[int, int]


class ParseRequest(BaseModel):
    image_path: Optional[str] = None
    # Base64-encoded image bytes (png/jpg)
    image_base64: Optional[str] = None
    box_threshold: float = Field(0.15, ge=0.01, le=1.0)
    iou_threshold: float = Field(0.7, ge=0.01, le=1.0)
    use_paddleocr: bool = True
    imgsz: int = 640


class PlanRequest(BaseModel):
    task: str
    elements: List[Element]
    image_size: Optional[Tuple[int, int]] = None
    annotated_image_base64: Optional[str] = None
    plan_context: Optional[str] = None


class PlanResponse(BaseModel):
    target_id: Optional[int] = None
    target_point: Optional[Tuple[int, int]] = None
    action_tool: Optional[str] = None
    action_text: Optional[str] = None
    action_key: Optional[str] = None
    action_ms: Optional[int] = None
    action_url: Optional[str] = None
    action_scroll: Optional[int] = None
    reason: str = ""
    query: Optional[str] = None
    debug: Optional[dict] = None


class PlanStepsRequest(BaseModel):
    task: str
    max_steps: Optional[int] = 6
    annotated_image_base64: Optional[str] = None


class PlanStepsResponse(BaseModel):
    steps: List[str]
    debug: Optional[dict] = None


class StepRequest(BaseModel):
    task: str
    # If provided, bypass LLM selection
    override_target_id: Optional[int] = None
    override_point: Optional[Tuple[int, int]] = None
    plan_context: Optional[str] = None


class StepResponse(BaseModel):
    action: str
    target_id: Optional[int] = None
    target_point: Optional[Tuple[int, int]] = None
    action_tool: Optional[str] = None
    action_text: Optional[str] = None
    action_key: Optional[str] = None
    action_ms: Optional[int] = None
    action_url: Optional[str] = None
    action_scroll: Optional[int] = None
    reason: str = ""
    screenshot_path: Optional[str] = None
    annotated_image_base64: Optional[str] = None
    elements: Optional[List[Element]] = None
    current_url: Optional[str] = None
    planner_debug: Optional[dict] = None
    finish_debug: Optional[dict] = None
    extracted: Optional[dict] = None
    # v2.2: VLM 对话详情（用于 debug）
    vlm_conversation: Optional[dict] = None


class TaskSpec(BaseModel):
    target_site: Optional[str] = None
    count: int = Field(10, ge=1)
    fields: List[str] = Field(default_factory=lambda: ["title", "url", "content", "time", "source"])
    filters: Optional[dict] = None
    output: Optional[dict] = None
    strategy: Optional[dict] = None


class ExtractRequest(BaseModel):
    task: str
    spec: TaskSpec
    mode: str = "list"


class ExtractResponse(BaseModel):
    data: dict
    debug: Optional[dict] = None
    spec: Optional[dict] = None


class AppendRowRequest(BaseModel):
    row: dict


class AppendRowResponse(BaseModel):
    count: int


class SaveOutputResponse(BaseModel):
    file: str


class FileListResponse(BaseModel):
    files: List[str]


class RunExtractionRequest(BaseModel):
    task: str
    max_items: int = Field(10, ge=1, le=100)
    strategy: Optional[dict] = None
    use_omniparser: bool = Field(False, description="是否使用OmniParser标注图片（False则使用原始截图）")
    use_reflection: bool = Field(True, description="是否使用反思机制进行翻页验证和重试（默认启用）")


class RunExtractionResponse(BaseModel):
    status: str  # 'success' | 'partial' | 'failed'
    items_extracted: int
    target_count: int
    file_path: Optional[str] = None
    progress: List[dict]
    errors: List[str]
    items: List[dict]  # 提取的数据供前端预览
