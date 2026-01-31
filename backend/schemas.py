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


class PlanResponse(BaseModel):
    target_id: Optional[int] = None
    reason: str = ""
    query: Optional[str] = None


class StepRequest(BaseModel):
    task: str
    # If provided, bypass LLM selection
    override_target_id: Optional[int] = None
    override_point: Optional[Tuple[int, int]] = None


class StepResponse(BaseModel):
    action: str
    target_id: Optional[int] = None
    reason: str = ""
    screenshot_path: Optional[str] = None
    annotated_image_base64: Optional[str] = None
    elements: Optional[List[Element]] = None
