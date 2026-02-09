"""
滚动截图工具 - 参考 Skyvern 的实现
"""

import base64
from typing import List, Tuple
import logging

logger = logging.getLogger(__name__)


async def take_scrolling_screenshot(executor, max_scrolls: int = 5) -> Tuple[str, List[dict]]:
    """
    滚动截图：截取整个页面并标记所有元素

    策略：
    1. 使用 Playwright 的全页面截图（fullPage=True）
    2. 滚动页面，在每个位置标记元素
    3. 合并所有元素列表（去重）

    Args:
        executor: Executor 实例
        max_scrolls: 最大滚动次数

    Returns:
        (base64_image, all_elements)
    """

    logger.info("Taking full page screenshot")

    # 1. 使用 Playwright 的全页面截图
    screenshot_path = "/tmp/full_page_screenshot.png"
    await executor._page.screenshot(path=screenshot_path, full_page=True)

    with open(screenshot_path, "rb") as f:
        image_base64 = base64.b64encode(f.read()).decode("ascii")

    # 2. 滚动页面并标记所有元素
    all_elements = []

    # 检查页面是否可滚动
    is_scrollable = await executor.is_page_scrollable()

    if not is_scrollable:
        # 页面不可滚动，直接标记当前视口的元素
        logger.info("Page is not scrollable, marking current viewport")
        dom_result = await executor.mark_page_elements()
        all_elements = dom_result.get('elements', [])
    else:
        # 页面可滚动，滚动并标记每个视口的元素
        logger.info("Page is scrollable, scrolling to mark all elements")

        # 滚动到顶部
        await executor.scroll_to_top()
        await executor.wait_for_stable(500)

        scroll_count = 0
        prev_scroll_y = -100

        while scroll_count < max_scrolls:
            # 获取当前滚动位置
            scroll_info = await executor.get_scroll_position()
            current_scroll_y = scroll_info.get('scrollY', 0)

            logger.info(f"Scroll {scroll_count + 1}: position={current_scroll_y}")

            # 标记当前视口的元素
            dom_result = await executor.mark_page_elements()
            viewport_elements = dom_result.get('elements', [])
            logger.info(f"Marked {len(viewport_elements)} elements at scroll position {current_scroll_y}")

            # 为每个元素添加滚动位置信息（用于后续去重）
            for elem in viewport_elements:
                elem['_scroll_y'] = current_scroll_y

            all_elements.extend(viewport_elements)

            # 检查是否到达底部（滚动位置没有变化）
            if abs(current_scroll_y - prev_scroll_y) < 25:
                logger.info(f"Reached bottom at scroll position {current_scroll_y}")
                break

            prev_scroll_y = current_scroll_y
            scroll_count += 1

            # 滚动到下一页（带 200px 重叠）
            await executor.scroll_to_next_page(need_overlap=True)
            await executor.wait_for_stable(500)

        # 滚动回顶部
        await executor.scroll_to_top()
        await executor.wait_for_stable(500)

    # 3. 去重元素（同一个元素可能在多个视口中被标记）
    unique_elements = _deduplicate_elements(all_elements)
    logger.info(f"Total elements after deduplication: {len(unique_elements)}")

    return image_base64, unique_elements


def _deduplicate_elements(elements: List[dict]) -> List[dict]:
    """
    去重元素：同一个元素可能在多个视口中被标记

    使用以下规则去重：
    1. 相同的 tagName + text + href -> 同一个元素
    2. 保留第一次出现的元素
    """
    seen = set()
    unique = []

    for elem in elements:
        # 生成唯一标识
        tag = elem.get('tagName', '')
        text = (elem.get('text', '') or '')[:50]  # 只取前50个字符
        href = elem.get('attributes', {}).get('href', '') or ''

        key = f"{tag}|{text}|{href}"

        if key not in seen:
            seen.add(key)
            unique.append(elem)

            # 调试：记录包含 "next" 的元素
            if 'next' in text.lower() or 'next' in href.lower():
                logger.info(f"Found 'next' element: {elem.get('id')} - text: '{text}' - href: {href}")

    return unique
