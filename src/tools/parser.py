import sys
import os
import io
import torch
import base64
import gc
from abc import ABC, abstractmethod
from PIL import Image

# Add OmniParser to sys.path
OMNI_PARSER_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../models/OmniParser"))
if OMNI_PARSER_PATH not in sys.path:
    sys.path.append(OMNI_PARSER_PATH)

# Import OmniParser utilities
try:
    from util.utils import get_som_labeled_img, check_ocr_box, get_yolo_model, get_caption_model_processor
except ImportError as e:
    print(f"Warning: Could not import OmniParser dependencies: {e}")


class ScreenParser(ABC):
    @abstractmethod
    def parse(self, screenshot_path):
        pass


class OmniParserLocal(ScreenParser):
    def __init__(self, device=None):
        # 优先使用 CUDA
        desired_device = device or os.getenv("OMNIPARSER_DEVICE", "cuda")
        if desired_device == "cuda" and torch.cuda.is_available():
            self.device = "cuda"
        else:
            self.device = "cpu"
        print(f"[OmniParser] Loading models on {self.device}...")

        weights_path = os.path.join(OMNI_PARSER_PATH, "weights")
        yolo_path = os.path.join(weights_path, "icon_detect/model.pt")

        if not os.path.exists(yolo_path):
            raise FileNotFoundError(f"YOLO model not found at {yolo_path}")

        print("[OmniParser] loading YOLO...")
        self.yolo_model = get_yolo_model(model_path=yolo_path)

        # 由于Fl orence2模型有兼容性问题，暂时禁用语义理解功能
        # 只使用YOLO进行UI元素检测，OCR提供文本信息
        print("[OmniParser] Skipping Florence2 model due to compatibility issues.")
        print("[OmniParser] Will use YOLO + OCR only (no semantic understanding).")
        self.caption_model_processor = None  # 禁用Florence2

        print("[OmniParser] Models loaded.")

    def parse(self, screenshot_path):
        # 每次解析前进行显存深度清理，腾出空间给视觉模型
        if self.device == "cuda":
            gc.collect()
            torch.cuda.empty_cache()

        print(f"[OmniParser] Parsing {screenshot_path}...")
        image = Image.open(screenshot_path).convert('RGB')

        box_overlay_ratio = max(image.size) / 3200
        draw_bbox_config = {
            'text_scale': 0.8 * box_overlay_ratio,
            'text_thickness': max(int(2 * box_overlay_ratio), 1),
            'text_padding': max(int(3 * box_overlay_ratio), 1),
            'thickness': max(int(3 * box_overlay_ratio), 1),
        }

        # 1. OCR Detection
        (text, ocr_bbox), _ = check_ocr_box(
            image,
            display_img=False,
            output_bb_format='xyxy',
            easyocr_args={'text_threshold': 0.8},
            use_paddleocr=False  # 使用之前假定的 FakeOCR 对象避免报错
        )

        # 2. Icon Detection & Captioning
        # 由于Florence2被禁用，我们只使用YOLO进行UI元素检测
        # use_local_semantics=False 意味着不使用Florence2进行图标描述
        dino_labeled_img, label_coordinates, parsed_content_list = get_som_labeled_img(
            image,
            self.yolo_model,
            BOX_TRESHOLD=0.15,
            output_coord_in_ratio=False,
            ocr_bbox=ocr_bbox,
            draw_bbox_config=draw_bbox_config,
            caption_model_processor=self.caption_model_processor,
            ocr_text=text,
            use_local_semantics=False,  # 禁用语义理解，只用YOLO+OCR
            iou_threshold=0.7,
            scale_img=False,
            batch_size=8
        )

        # 保存标注图片
        name, ext = os.path.splitext(screenshot_path)
        annotated_path = f"{name}_labeled{ext}"

        # OmniParser 可能返回 base64 或直接是 Image 对象
        if isinstance(dino_labeled_img, str):
            img_data = base64.b64decode(dino_labeled_img)
            pil_img = Image.open(io.BytesIO(img_data))
            pil_img.save(annotated_path)
        else:
            # 如果 utils.py 返回的是 PIL 对象
            dino_labeled_img.save(annotated_path)

        # 3. 映射 UI 元素信息
        ui_elements = []
        for label_id, bbox in label_coordinates.items():
            # bbox 为 [cx, cy, w, h] 格式
            cx, cy, w, h = bbox
            ui_elements.append({
                "id": int(label_id) if str(label_id).isdigit() else label_id,
                "bbox": [int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2)],
                "center": [int(cx), int(cy)],
                "type": "element"
            })

        # 补充语义内容 (由 Florence-2 生成的描述)
        for i, elem in enumerate(parsed_content_list):
            if i < len(ui_elements):
                ui_elements[i]['content'] = elem.get('content', '')
                ui_elements[i]['source'] = elem.get('source', '')

        # 解析完后再次清理显存，把舞台交给 Brain (Qwen2-VL)
        if self.device == "cuda":
            gc.collect()
            torch.cuda.empty_cache()

        return annotated_path, ui_elements


class OmniParserMock(ScreenParser):
    def parse(self, screenshot_path):
        print(f"[OmniParserMock] Returning mock for {screenshot_path}")
        return screenshot_path, [{"id": 0, "center": [100, 100], "bbox": [0, 0, 200, 200]}]