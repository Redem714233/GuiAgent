from __future__ import annotations

import base64
import io
import os
import sys
from typing import List

# Environment setup needs to happen before importing OmniParser utilities.
# Keep it generic and configurable (no hardcoded user paths).
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("FLAGS_use_onednn", "0")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("FLAGS_selected_gpus", "0")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

extra_paths = os.getenv("OMNIPARSER_EXTRA_PATHS")
if extra_paths:
    for p in extra_paths.split(";"):
        if p:
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")

try:
    import torch

    torch_lib_path = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.exists(torch_lib_path) and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(torch_lib_path)

    try:
        import site

        for path in site.getsitepackages():
            cudnn_path = os.path.join(path, "nvidia", "cudnn", "bin")
            if os.path.exists(cudnn_path):
                os.environ["PATH"] = cudnn_path + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(cudnn_path)
    except Exception:
        pass
except Exception:
    torch = None

from PIL import Image

# Reuse OmniParser repo code via sys.path
OMNI_PARSER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../models/OmniParser"))
if OMNI_PARSER_PATH not in sys.path:
    sys.path.append(OMNI_PARSER_PATH)

from util.utils import check_ocr_box, get_caption_model_processor, get_som_labeled_img, get_yolo_model

from backend.schemas import Element, ParseRequest, ParseResponse


class OmniParserService:
    def __init__(self, device: str | None = None) -> None:
        if device is None:
            if torch is not None and getattr(torch.cuda, "is_available", lambda: False)():
                device = "cuda"
            else:
                device = "cpu"
        self.device = device

        # Optional: set Paddle device when available
        try:
            import paddle

            paddle.set_device("gpu" if self.device == "cuda" else "cpu")
        except Exception:
            pass

        weights_path = os.path.join(OMNI_PARSER_PATH, "weights")
        yolo_path = os.path.join(weights_path, "icon_detect", "model.pt")
        caption_path = os.path.join(weights_path, "icon_caption_florence")

        if not os.path.exists(yolo_path):
            raise FileNotFoundError(f"YOLO model not found at {yolo_path}")

        self.yolo_model = get_yolo_model(model_path=yolo_path)
        # Florence2 is required (user confirmed working)
        self.caption_model_processor = get_caption_model_processor(
            model_name="florence2",
            model_name_or_path=caption_path,
            device=self.device,
        )

    def parse(self, request: ParseRequest) -> ParseResponse:
        image = self._load_image(request)
        image = image.convert("RGB")

        box_overlay_ratio = image.size[0] / 3200
        draw_bbox_config = {
            "text_scale": 0.8 * box_overlay_ratio,
            "text_thickness": max(int(2 * box_overlay_ratio), 1),
            "text_padding": max(int(3 * box_overlay_ratio), 1),
            "thickness": max(int(3 * box_overlay_ratio), 1),
        }

        try:
            ocr_bbox_result, _ = check_ocr_box(
                image,
                display_img=False,
                output_bb_format="xyxy",
                goal_filtering=None,
                easyocr_args={"paragraph": False, "text_threshold": 0.9},
                use_paddleocr=request.use_paddleocr,
            )
            text, ocr_bbox = ocr_bbox_result
        except Exception:
            # OCR sometimes returns None on blank pages; fall back to empty results.
            text, ocr_bbox = [], []

        labeled_b64, label_coordinates, parsed_content_list = get_som_labeled_img(
            image,
            self.yolo_model,
            BOX_TRESHOLD=request.box_threshold,
            output_coord_in_ratio=False,
            ocr_bbox=ocr_bbox,
            draw_bbox_config=draw_bbox_config,
            caption_model_processor=self.caption_model_processor,
            ocr_text=text,
            iou_threshold=request.iou_threshold,
            imgsz=request.imgsz,
        )

        annotated_image_base64 = labeled_b64
        elements = self._to_elements(label_coordinates, parsed_content_list)
        return ParseResponse(
            annotated_image_base64=annotated_image_base64,
            elements=elements,
            image_size=(image.size[0], image.size[1]),
        )

    def _load_image(self, request: ParseRequest) -> Image.Image:
        if request.image_path:
            return Image.open(request.image_path)
        if request.image_base64:
            raw = base64.b64decode(request.image_base64)
            return Image.open(io.BytesIO(raw))
        raise ValueError("Either image_path or image_base64 must be provided.")

    def _to_elements(self, label_coordinates: dict, parsed_content_list: list) -> List[Element]:
        elements: List[Element] = []
        for label_id, bbox in label_coordinates.items():
            x, y, w, h = bbox
            element_id = int(label_id) if str(label_id).isdigit() else len(elements)
            elements.append(
                Element(
                    id=element_id,
                    type="element",
                    content="",
                    center=(int(x + w / 2), int(y + h / 2)),
                    bbox=(int(x), int(y), int(w), int(h)),
                )
            )

        for idx, elem in enumerate(parsed_content_list):
            if idx < len(elements):
                content = elem.get("content", "") if isinstance(elem, dict) else str(elem)
                elements[idx].content = content or ""
                elem_type = elem.get("type") if isinstance(elem, dict) else None
                if elem_type:
                    elements[idx].type = elem_type

        return elements
