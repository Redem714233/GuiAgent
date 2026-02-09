# 翻页功能说明

## 概述

GUIAgent 现在支持智能翻页功能，可以自动识别和处理网页的分页内容。该功能参考了 Skyvern 的实现，支持两种翻页方式：

1. **按钮点击翻页** - 识别并点击"下一页"按钮
2. **滚动翻页** - 智能滚动到下一页内容

## 核心特性

### 1. 三层翻页按钮识别机制

**第一层：VLM 智能识别**
- VLM 在提取列表数据时自动识别翻页按钮
- 返回 `next_page_element_id` 字段
- 直接点击该元素

**第二层：关键词匹配**
- 搜索常见的"下一页"按钮文本
- 支持的关键词：
  - 中文：下一页、下页、翻页、更多
  - 英文：next page、next、load more、more
  - 符号：›、»、→

**第三层：VLM 专项分析**
- 使用 `find_next_page_button()` 方法
- 专门寻找翻页按钮
- 返回置信度评分

### 2. 智能滚动翻页（参考 Skyvern）

**核心特性**：
- ✅ **可滚动性检测** - 自动检测页面是否可滚动
- ✅ **底部检测** - 25px 阈值判定是否到达底部
- ✅ **重叠滚动** - 200px 重叠确保内容连续性
- ✅ **滚动距离验证** - 检测滚动是否有效

**工作流程**：
```
1. 检查页面是否可滚动
   ↓
2. 获取当前滚动位置
   ↓
3. 检查是否已到达底部
   ↓
4. 滚动到下一页（保留 200px 重叠）
   ↓
5. 验证滚动距离 > 25px
   ↓
6. 返回成功/失败
```

## 使用方法

### 自动模式（推荐）

翻页功能在 `run_extraction()` 中自动启用：

```python
result = await extraction_engine.run_extraction(
    task="提取豆瓣电影Top250的前50部电影",
    max_items=50,
    strategy={
        "max_scrolls": 10  # 最大翻页次数
    }
)
```

### VLM 控制

VLM 会在 `extract_from_page()` 返回中指定翻页策略：

```json
{
  "items": [...],
  "next": "next_page",  // 或 "scroll" 或 "stop"
  "next_page_element_id": "skyvern-48"  // 可选
}
```

## 实现文件

### 1. DOM 层面（dom_marker.js）

新增函数：
- `isWindowScrollable()` - 检查页面是否可滚动
- `scrollToNextPage(needOverlap)` - 滚动到下一页
- `safeScrollToTop()` - 滚动到顶部
- `getScrollWidthAndHeight()` - 获取滚动信息
- `isAtPageBottom(threshold)` - 检测是否到达底部

### 2. Executor 层面（executor.py）

新增方法：
- `is_page_scrollable()` - 检查可滚动性
- `scroll_to_next_page(need_overlap)` - 执行滚动翻页
- `scroll_to_top()` - 滚动到顶部
- `get_scroll_position()` - 获取滚动位置
- `is_at_page_bottom(threshold)` - 检测底部

### 3. VLM 层面（vlm_service.py）

新增方法：
- `find_next_page_button()` - 专门识别翻页按钮

增强功能：
- `extract_from_page()` 自动识别 `next_page_element_id`

### 4. 引擎层面（extraction_engine.py）

新增方法：
- `_click_next_page_button()` - 三层识别机制
- `_find_next_page_button_with_vlm()` - VLM 专项分析
- `_smart_scroll_pagination()` - 智能滚动翻页

## 支持的分页类型

1. **传统分页按钮**
   ```html
   <a href="?page=2">下一页</a>
   <button onclick="nextPage()">Next</button>
   ```

2. **数字分页**
   ```html
   <a href="?page=3">3</a>
   <a href="?page=4" class="active">4</a>
   ```

3. **箭头分页**
   ```html
   <button class="next-page">→</button>
   <a href="?page=2">›</a>
   ```

4. **加载更多**
   ```html
   <button class="load-more">Load More</button>
   <div class="show-more">显示更多</div>
   ```

5. **无限滚动**
   - 通过滚动触发 AJAX 加载
   - 自动检测新内容

## 配置参数

### extraction_engine.py

```python
strategy = {
    "max_scrolls": 5,        # 最大翻页次数（默认 5）
    "scroll_strategy": "auto" # 滚动策略（auto/manual）
}
```

### executor.py

```python
# 滚动参数
need_overlap = True          # 是否需要 200px 重叠
threshold = 25               # 底部检测阈值（px）
```

## 调试技巧

### 1. 查看翻页日志

```python
import logging
logging.basicConfig(level=logging.INFO)
```

关键日志：
- `"VLM provided next_page_element_id: ..."`
- `"Found potential next page button: ..."`
- `"Scrolled from Xpx to Ypx (distance: Zpx)"`
- `"Reached page bottom, stopping scroll"`

### 2. 测试翻页功能

```python
# 测试滚动翻页
is_scrollable = await executor.is_page_scrollable()
scroll_y = await executor.scroll_to_next_page(need_overlap=True)
is_bottom = await executor.is_at_page_bottom()

# 测试按钮识别
result, _ = vlm_service.find_next_page_button(
    annotated_image_base64=image_base64,
    elements=dom_elements
)
```

## 常见问题

### Q: 为什么翻页失败？

**可能原因**：
1. 页面使用动态加载（AJAX），需要等待时间
2. 翻页按钮被遮挡或不可见
3. 页面使用非标准的翻页方式

**解决方法**：
- 增加 `wait_for_stable` 时间
- 检查 VLM 返回的 `next_page_element_id`
- 使用滚动翻页作为备选方案

### Q: 如何处理无限滚动？

无限滚动会自动处理：
1. 检测滚动距离
2. 如果滚动距离 < 25px，判定为到达底部
3. 等待新内容加载后继续提取

### Q: 翻页次数限制？

默认最大翻页次数为 5 次，可通过 `max_scrolls` 参数调整：

```python
strategy = {"max_scrolls": 10}  # 最多翻 10 页
```

## 性能优化

1. **重叠滚动** - 200px 重叠避免内容遗漏
2. **底部检测** - 25px 阈值快速判定
3. **三层识别** - 降级机制确保鲁棒性
4. **智能等待** - 根据页面加载情况调整等待时间

## 参考资料

- Skyvern domUtils.js: `skyvern/skyvern/webeye/scraper/domUtils.js`
- Skyvern page.py: `skyvern/skyvern/webeye/utils/page.py`
- 本地实现: `backend/dom_marker.js`, `backend/executor.py`

---

**最后更新**: 2026-02-09
**版本**: v3.1
**作者**: Claude Code
