"""
DOM-based Visual Annotator

替代 OmniParser 的视觉标注方案：
- 使用 DOM 坐标在截图上绘制标注框
- 为每个可交互元素标注 ID
- 生成供 VLM 理解的标注图片

参考: Skyvern 的视觉标注方案
"""

import base64
import io
from typing import List, Dict, Optional, Tuple
from PIL import Image, ImageDraw, ImageFont
import logging

logger = logging.getLogger(__name__)


class DOMVisualizer:
    """DOM 驱动的视觉标注器"""

    def __init__(
        self,
        box_color: str = "red",
        box_width: int = 3,  # 增加边框宽度，更明显
        label_bg_color: str = "red",
        label_text_color: str = "white",
        font_size: int = 20,  # 增大字体，更清晰
        opacity: int = 200,  # 0-255, 200 = 78% opacity
    ):
        """
        初始化标注器

        Args:
            box_color: 边框颜色
            box_width: 边框宽度
            label_bg_color: 标签背景色
            label_text_color: 标签文字颜色
            font_size: 字体大小
            opacity: 透明度 (0-255)
        """
        self.box_color = box_color
        self.box_width = box_width
        self.label_bg_color = label_bg_color
        self.label_text_color = label_text_color
        self.font_size = font_size
        self.opacity = opacity

        # 尝试加载字体
        try:
            # Windows 系统字体
            self.font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            try:
                # Linux 系统字体
                self.font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
            except Exception:
                # 使用默认字体
                self.font = ImageFont.load_default()
                logger.warning("Could not load TrueType font, using default font")

    def annotate(
        self,
        image: Image.Image,
        elements: List[Dict],
        max_elements: Optional[int] = None,
    ) -> Image.Image:
        """
        在图片上标注 DOM 元素

        Args:
            image: PIL Image 对象
            elements: DOM 元素列表，每个元素包含 id, rect, text 等信息
            max_elements: 最多标注多少个元素（None = 全部）

        Returns:
            标注后的 PIL Image 对象
        """
        # 创建副本，避免修改原图
        annotated = image.copy()
        draw = ImageDraw.Draw(annotated, 'RGBA')

        # 过滤和排序元素
        valid_elements = self._filter_elements(elements)
        if max_elements:
            valid_elements = valid_elements[:max_elements]

        logger.info(f"Annotating {len(valid_elements)} elements on screenshot")

        # 绘制每个元素
        for element in valid_elements:
            self._draw_element(draw, element)

        return annotated

    def _filter_elements(self, elements: List[Dict]) -> List[Dict]:
        """
        过滤和排序元素

        优先级：
        1. 可见且可交互
        2. 有有效的坐标
        3. 按优先级分数排序（如果有）
        """
        valid = []

        for elem in elements:
            # 检查是否可见和可交互
            if not elem.get('isVisible', True):
                continue
            if not elem.get('isInteractable', True):
                continue

            # 检查是否有有效的坐标
            rect = elem.get('rect')
            if not rect:
                continue

            # 检查坐标是否有效
            if rect.get('width', 0) <= 0 or rect.get('height', 0) <= 0:
                continue

            valid.append(elem)

        # 按优先级排序（如果有）
        if valid and 'priority' in valid[0]:
            valid.sort(key=lambda x: x.get('priority', 0), reverse=True)

        return valid

    def _draw_element(self, draw: ImageDraw.Draw, element: Dict) -> None:
        """
        绘制单个元素的标注

        Args:
            draw: ImageDraw 对象
            element: 元素信息
        """
        rect = element['rect']
        element_id = element.get('id', '?')

        # 提取坐标
        x = rect.get('x', rect.get('left', 0))
        y = rect.get('y', rect.get('top', 0))
        width = rect.get('width', 0)
        height = rect.get('height', 0)

        # 绘制边框（半透明红框）
        box_coords = [
            (x, y),
            (x + width, y + height)
        ]

        # 使用半透明颜色
        box_color_rgba = self._hex_to_rgba(self.box_color, self.opacity)
        draw.rectangle(
            box_coords,
            outline=box_color_rgba,
            width=self.box_width
        )

        # 绘制 ID 标签（左上角）
        self._draw_label(draw, element_id, x, y)

    def _draw_label(self, draw: ImageDraw.Draw, text: str, x: int, y: int) -> None:
        """
        绘制 ID 标签（优化版）

        改进：
        - 更大的字体和背景
        - 标签放在框内左上角（避免被遮挡）
        - 添加阴影效果

        Args:
            draw: ImageDraw 对象
            text: 标签文本（通常是 ID）
            x: X 坐标
            y: Y 坐标
        """
        # 提取数字部分（如 "skyvern-48" -> "48"）
        if isinstance(text, str) and '-' in text:
            text = text.split('-')[-1]

        label_text = str(text)

        # 计算文本大小
        try:
            # PIL 10.0.0+ 使用 textbbox
            bbox = draw.textbbox((0, 0), label_text, font=self.font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
        except AttributeError:
            # 旧版本 PIL 使用 textsize
            text_width, text_height = draw.textsize(label_text, font=self.font)

        # 标签背景位置（放在框内左上角，避免被遮挡）
        padding = 4  # 增加 padding
        label_x = x + 2  # 稍微偏移到框内
        label_y = y + 2  # 稍微偏移到框内

        # 绘制阴影（增强可读性）
        shadow_offset = 2
        shadow_color = (0, 0, 0, 180)
        draw.rectangle(
            [
                (label_x + shadow_offset, label_y + shadow_offset),
                (label_x + text_width + padding * 2 + shadow_offset,
                 label_y + text_height + padding * 2 + shadow_offset)
            ],
            fill=shadow_color
        )

        # 绘制标签背景（不透明）
        bg_color_rgba = self._hex_to_rgba(self.label_bg_color, 255)
        draw.rectangle(
            [
                (label_x, label_y),
                (label_x + text_width + padding * 2, label_y + text_height + padding * 2)
            ],
            fill=bg_color_rgba
        )

        # 绘制标签文字
        text_color_rgba = self._hex_to_rgba(self.label_text_color, 255)
        draw.text(
            (label_x + padding, label_y + padding),
            label_text,
            fill=text_color_rgba,
            font=self.font
        )

    def _hex_to_rgba(self, hex_color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
        """
        将十六进制颜色转换为 RGBA 元组

        Args:
            hex_color: 十六进制颜色（如 "red", "#FF0000"）
            alpha: 透明度 (0-255)

        Returns:
            (R, G, B, A) 元组
        """
        # 预定义颜色
        color_map = {
            'red': (255, 0, 0),
            'green': (0, 255, 0),
            'blue': (0, 0, 255),
            'yellow': (255, 255, 0),
            'white': (255, 255, 255),
            'black': (0, 0, 0),
        }

        if hex_color.lower() in color_map:
            r, g, b = color_map[hex_color.lower()]
        elif hex_color.startswith('#'):
            # 解析十六进制
            hex_color = hex_color.lstrip('#')
            if len(hex_color) == 6:
                r = int(hex_color[0:2], 16)
                g = int(hex_color[2:4], 16)
                b = int(hex_color[4:6], 16)
            else:
                r, g, b = 255, 0, 0  # 默认红色
        else:
            r, g, b = 255, 0, 0  # 默认红色

        return (r, g, b, alpha)

    def annotate_from_base64(
        self,
        image_base64: str,
        elements: List[Dict],
        max_elements: Optional[int] = None,
    ) -> str:
        """
        从 Base64 图片标注并返回 Base64

        Args:
            image_base64: Base64 编码的图片
            elements: DOM 元素列表
            max_elements: 最多标注多少个元素

        Returns:
            Base64 编码的标注图片
        """
        # 解码图片
        image_data = base64.b64decode(image_base64)
        image = Image.open(io.BytesIO(image_data))

        # 标注
        annotated = self.annotate(image, elements, max_elements)

        # 编码回 Base64
        buffer = io.BytesIO()
        annotated.save(buffer, format='PNG')
        annotated_base64 = base64.b64encode(buffer.getvalue()).decode('ascii')

        return annotated_base64

    def annotate_from_file(
        self,
        image_path: str,
        elements: List[Dict],
        output_path: Optional[str] = None,
        max_elements: Optional[int] = None,
    ) -> str:
        """
        从文件标注并保存

        Args:
            image_path: 输入图片路径
            elements: DOM 元素列表
            output_path: 输出图片路径（None = 自动生成）
            max_elements: 最多标注多少个元素

        Returns:
            输出图片路径
        """
        # 读取图片
        image = Image.open(image_path)

        # 标注
        annotated = self.annotate(image, elements, max_elements)

        # 生成输出路径
        if output_path is None:
            import os
            base, ext = os.path.splitext(image_path)
            output_path = f"{base}_annotated{ext}"

        # 保存
        annotated.save(output_path)
        logger.info(f"Saved annotated image to {output_path}")

        return output_path


# 全局实例（单例模式）
_visualizer_instance = None


def get_visualizer() -> DOMVisualizer:
    """获取全局 visualizer 实例"""
    global _visualizer_instance
    if _visualizer_instance is None:
        _visualizer_instance = DOMVisualizer()
    return _visualizer_instance


# 便捷函数
def annotate_screenshot(
    image: Image.Image,
    elements: List[Dict],
    max_elements: Optional[int] = None,
) -> Image.Image:
    """
    便捷函数：标注截图

    Args:
        image: PIL Image 对象
        elements: DOM 元素列表
        max_elements: 最多标注多少个元素

    Returns:
        标注后的 PIL Image 对象
    """
    visualizer = get_visualizer()
    return visualizer.annotate(image, elements, max_elements)


def annotate_screenshot_base64(
    image_base64: str,
    elements: List[Dict],
    max_elements: Optional[int] = None,
) -> str:
    """
    便捷函数：标注 Base64 图片

    Args:
        image_base64: Base64 编码的图片
        elements: DOM 元素列表
        max_elements: 最多标注多少个元素

    Returns:
        Base64 编码的标注图片
    """
    visualizer = get_visualizer()
    return visualizer.annotate_from_base64(image_base64, elements, max_elements)
