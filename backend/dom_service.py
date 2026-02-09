"""
DOM 元素标记服务

基于 Skyvern 的方法：
1. 注入 JavaScript 到页面
2. 给所有可交互元素分配 unique_id
3. 提取元素列表
4. 通过 unique_id 定位和点击元素
"""
import logging
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class DOMService:
    """DOM 元素标记和定位服务"""

    def __init__(self):
        # 读取 DOM 标记 JavaScript
        dom_js_path = Path(__file__).parent / "dom_marker.js"
        with open(dom_js_path, 'r', encoding='utf-8') as f:
            self.dom_marker_js = f.read()

    async def mark_page_elements(self, page) -> Dict[str, Any]:
        """
        标记页面上的所有可交互元素

        Args:
            page: Playwright page 对象

        Returns:
            {
                'elements': [...],  # 元素列表
                'count': int,       # 元素数量
                'viewport': {...}   # 视口信息
            }
        """
        try:
            # 注入 JavaScript
            await page.evaluate(self.dom_marker_js)

            # 执行标记和提取
            result = await page.evaluate("markAndExtractElements()")

            logger.info(f"Marked {result['count']} interactive elements")
            return result

        except Exception as e:
            logger.error(f"Failed to mark page elements: {e}")
            return {
                'elements': [],
                'count': 0,
                'viewport': {'width': 0, 'height': 0}
            }

    async def click_element_by_id(self, page, element_id: str) -> bool:
        """
        通过 unique_id 点击元素
        使用多层级降级策略，参考 Skyvern 的实现

        Args:
            page: Playwright page 对象
            element_id: 元素的 unique_id

        Returns:
            是否成功点击
        """
        try:
            # 注入 JavaScript（如果还没有）
            await page.evaluate(self.dom_marker_js)

            # 先滚动到元素位置（确保元素可见）
            try:
                await page.evaluate(f"""
                    (function() {{
                        const element = document.querySelector('[unique_id="{element_id}"]');
                        if (element) {{
                            element.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                            return true;
                        }}
                        return false;
                    }})()
                """)
                # 等待滚动完成
                await page.wait_for_timeout(500)
            except Exception as scroll_error:
                logger.warning(f"Failed to scroll to element {element_id}: {scroll_error}")

            # 方法1: 尝试使用 JavaScript 点击
            success = await page.evaluate(f"clickElementById('{element_id}')")

            if success:
                logger.info(f"Successfully clicked element: {element_id}")
                return True
            else:
                logger.warning(f"JavaScript click failed for element: {element_id}, trying coordinate click")

                # 方法2: 降级到坐标点击
                center = await page.evaluate(f"getElementCenter('{element_id}')")
                if center and 'x' in center and 'y' in center:
                    x, y = center['x'], center['y']
                    logger.info(f"Clicking at coordinates ({x}, {y}) for element {element_id}")
                    await page.mouse.click(x, y)
                    return True
                else:
                    logger.error(f"Could not get element center for {element_id}")
                    return False

        except Exception as e:
            logger.error(f"Failed to click element {element_id}: {e}")

            # 方法3: 最后尝试使用 Playwright 的 locator 点击
            try:
                logger.info(f"Trying Playwright locator click for element {element_id}")
                locator = page.locator(f"[unique_id='{element_id}']")
                await locator.scroll_into_view_if_needed(timeout=5000)
                await locator.click(timeout=5000)
                logger.info(f"Playwright locator click succeeded for {element_id}")
                return True
            except Exception as locator_error:
                logger.error(f"Playwright locator click also failed: {locator_error}")
                return False

    async def get_element_center(self, page, element_id: str) -> Optional[Dict[str, int]]:
        """
        获取元素的中心坐标

        Args:
            page: Playwright page 对象
            element_id: 元素的 unique_id

        Returns:
            {'x': int, 'y': int} 或 None
        """
        try:
            # 注入 JavaScript（如果还没有）
            await page.evaluate(self.dom_marker_js)

            # 获取中心坐标
            center = await page.evaluate(f"getElementCenter('{element_id}')")

            return center

        except Exception as e:
            logger.error(f"Failed to get element center {element_id}: {e}")
            return None

    def elements_to_html(self, elements: List[Dict[str, Any]]) -> str:
        """
        将元素列表转换为 HTML 格式（供 VLM 理解）

        Args:
            elements: 元素列表

        Returns:
            HTML 字符串
        """
        html_parts = []

        for elem in elements:
            # 构建元素描述
            tag = elem['tagName']
            elem_id = elem['id']
            text = elem.get('text', '')[:100]  # 限制文本长度

            # 获取关键属性
            attrs = elem.get('attributes', {})
            attr_str = []
            if attrs.get('id'):
                attr_str.append(f"id='{attrs['id']}'")
            if attrs.get('class'):
                attr_str.append(f"class='{attrs['class']}'")
            if attrs.get('type'):
                attr_str.append(f"type='{attrs['type']}'")

            # 构建 HTML
            attr_text = ' '.join(attr_str)
            if text:
                html = f"<{tag} unique_id='{elem_id}' {attr_text}>{text}</{tag}>"
            else:
                html = f"<{tag} unique_id='{elem_id}' {attr_text} />"

            html_parts.append(html)

        return '\n'.join(html_parts)
