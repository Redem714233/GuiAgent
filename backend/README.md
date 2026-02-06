# GUIAgent Backend

基于 Skyvern 架构模式的浏览器自动化系统

## 🎉 v2.2 最新更新 (2026-02-06)

**DOM 驱动的视觉标注系统（完全替代 OmniParser）**

核心改进：废弃 YOLO/Florence2 视觉检测，全面转向 DOM 驱动的视觉增强架构。

**关键特性：**
- ✅ **DOM 视觉标注**：使用 Pillow 在截图上绘制 DOM 坐标的红框 + ID 标签
- ✅ **统一数据源**：视觉标注和 href 提取都来自 DOM，数据一致性更好
- ✅ **更轻量**：无需深度学习模型（YOLO/Florence2），启动更快
- ✅ **更准确**：DOM 坐标精确，避免 YOLO 检测框不准确的问题
- ✅ **可配置**：通过 `USE_DOM_ANNOTATION=1` 启用新方案

**新增文件：**
- `backend/visualizer.py` - DOM 视觉标注模块

---

## 🎉 v2.1 更新 (2026-02-05)

**DOM-based 元素定位系统（参考Skyvern架构）**

核心改进：解决了页面重新加载后element_id失效导致点击失败的问题。

**关键特性：**
- ✅ **智能href提取**：从DOM元素自动提取href，避免element_id失效
- ✅ **优先URL导航**：详情页提取使用goto(url)而不是点击
- ✅ **智能元素匹配**：评分算法（完全匹配100分、包含匹配80分、词语重叠50+分）
- ✅ **多层点击回退**：JavaScript click → 坐标点击 → Playwright locator
- ✅ **延迟初始化**：OmniParser按需加载，避免启动时下载模型

---

## 架构概述

本项目是一个使用视觉语言模型（VLM）和计算机视觉技术的智能浏览器自动化系统，能够自动完成网页操作、数据提取等任务。

### 核心组件

```
backend/
├── actions/              # 动作系统（基于 Skyvern 模式）
│   ├── action_types.py   # 动作类型枚举
│   ├── actions.py        # 动作类层次结构
│   ├── handler.py        # 动作执行器
│   └── parser.py         # 动作解析器
├── dom_marker.js         # DOM元素标记（JavaScript）⭐ v2.1
├── dom_service.py        # DOM服务封装 ⭐ v2.1
├── visualizer.py         # DOM视觉标注模块 ⭐ v2.2新增
├── extraction_engine.py  # 数据提取引擎
├── executor.py           # 浏览器控制器（Playwright）
├── planner.py            # 决策规划器（LLM/VLM）
├── vlm_service.py        # 视觉语言模型服务
├── omniparser_service.py # UI 元素检测服务（已废弃）
├── web_agent.py          # 高级代理编排
├── server.py             # FastAPI REST API
└── schemas.py            # Pydantic 数据模型
```

---

## 核心改进（基于 Skyvern）

### 1. DOM-based 元素定位系统 ⭐ v2.1

**问题背景：**
页面重新加载后，DOM元素的`unique_id`会重新生成，导致之前保存的element_id失效，点击失败。

**解决方案（参考Skyvern架构）：**

#### 1.1 JavaScript DOM标记
**文件：** `backend/dom_marker.js`

```javascript
// 为所有可交互元素分配unique_id
function markAndExtractElements() {
    for (const element of allElements) {
        if (isVisible(element) && isInteractable(element)) {
            assignUniqueId(element);  // 分配 skyvern-48 这样的ID
            extractElementInfo(element);  // 提取 text, href, rect 等
            calculatePriority(element);  // 计算优先级分数
        }
    }
    // 按优先级排序，主要内容排在前面
    return sortedElements;
}
```

**特性：**
- 自动标记所有可交互元素（a, button, input等）
- 提取元素信息：标签名、文本、href、位置、大小
- 优先级排序：主要内容链接排在前面，避免VLM选择错误元素
- 可见性检测：只标记可见且可交互的元素

#### 1.2 智能href提取
**文件：** `backend/extraction_engine.py:172-194`

```python
# 列表提取阶段：从element_id提取href
if "element_id" in item:
    for elem in dom_elements:
        if elem['id'] == element_id:
            href = elem['attributes']['href']
            if href and not item.get("url"):
                item["url"] = href  # 保存为URL，避免后续依赖element_id
                logger.info(f"Extracted href from element {element_id}: {href}")
```

**工作原理：**
1. VLM返回element_id（如 `skyvern-48`）
2. 从DOM元素列表中查找对应元素
3. 提取该元素的href属性
4. 保存为item的URL字段
5. 详情提取时优先使用URL导航

#### 1.3 优先URL导航
**文件：** `backend/extraction_engine.py:282-291`

```python
# 详情提取阶段：优先使用URL
if has_url:
    await executor.goto(detail_url)  # 直接导航，稳定可靠
elif has_element_id:
    # 备用方案：重新匹配并点击
    element_id = smart_match_by_title(saved_title, dom_elements)
    await executor.click_element_by_id(element_id)
```

**优势：**
- ✅ 避免element_id失效问题
- ✅ 导航更可靠（直接goto URL vs 点击元素）
- ✅ 符合Skyvern的设计理念

#### 1.4 智能元素匹配
**文件：** `backend/extraction_engine.py:303-350`

如果必须使用element_id点击（没有URL的情况），使用评分算法重新定位元素：

```python
# 计算匹配分数
score = 0
if saved_title_lower == elem_text_lower:
    score = 100  # 完全匹配
elif saved_title_lower in elem_text_lower:
    score = 80   # 包含匹配
elif elem_text_lower in saved_title_lower:
    score = 70   # 被包含匹配
else:
    # 词语重叠匹配
    common_words = saved_title_words & elem_words
    if len(common_words) >= min(3, len(saved_title_words)):
        score = 50 + len(common_words) * 5

# <a>标签加成
if elem.tag == 'a':
    score += 10

# 只使用分数 >= 50 的匹配
if score >= 50:
    element_id = elem['id']
```

#### 1.5 多层点击回退
**文件：** `backend/dom_service.py`

```python
# 方法1: JavaScript click
success = await page.evaluate(f"clickElementById('{element_id}')")

# 方法2: 坐标点击
if not success:
    center = await page.evaluate(f"getElementCenter('{element_id}')")
    await page.mouse.click(center['x'], center['y'])

# 方法3: Playwright locator
if not success:
    await page.locator(f"[unique_id='{element_id}']").click()
```

**与Skyvern的对比：**

| 特性 | Skyvern | 我们的方案 |
|------|---------|-----------|
| 元素追踪 | SHA256哈希（基于元素结构） | href提取 + title匹配 |
| 导航方式 | 优先使用href | 优先使用href ✅ |
| 复杂度 | 高（哈希计算、缓存机制） | 低（直接提取href） |
| 效果 | 非常稳定 | 同样稳定 ✅ |
| 实现难度 | 复杂 | 简单 ✅ |

**我们的优势：** 更简单但同样有效，避免了复杂的哈希计算和缓存管理。

---

### 2. 完整的 Action 类型系统

**新增文件：**
- `backend/actions/action_types.py` - 动作类型枚举
- `backend/actions/actions.py` - 动作类层次结构

**特性：**
- ✅ 类型安全的动作定义（使用 Pydantic）
- ✅ 清晰的继承层次：`Action` → `WebAction` / `DecisiveAction`
- ✅ 支持 20+ 种动作类型
- ✅ 动作状态跟踪（pending, running, completed, failed）
- ✅ 丰富的元数据（reasoning, confidence, timestamps）

**支持的动作类型：**

| 类别 | 动作类型 | 说明 |
|------|---------|------|
| **基础交互** | ClickAction | 点击元素（支持单击/双击/三击） |
| | InputTextAction | 输入文本 |
| | UploadFileAction | 上传文件 |
| | DownloadFileAction | 下载文件 |
| **表单操作** | SelectOptionAction | 下拉选择 |
| | CheckboxAction | 复选框切换 |
| **导航控制** | GotoUrlAction | 导航到 URL |
| | ScrollAction | 滚动页面 |
| | WaitAction | 等待 |
| | ReloadPageAction | 刷新页面 |
| | ClosePageAction | 关闭页面 |
| **高级交互** | HoverAction | 悬停 |
| | KeypressAction | 按键 |
| | MoveAction | 鼠标移动 |
| | DragAction | 拖拽 |
| **数据提取** | ExtractAction | 提取数据 |
| **任务控制** | CompleteAction | 任务完成 |
| | TerminateAction | 任务终止 |
| | NullAction | 空操作 |
| **特殊功能** | SolveCaptchaAction | 验证码求解 |
| | VerificationCodeAction | 验证码输入 |

### 2. 增强的 ActionHandler

**改进：**
- ✅ 支持新的 Action 对象（类型安全）
- ✅ 向后兼容旧的字典格式
- ✅ 每个动作类型有专门的处理方法
- ✅ 统一的错误处理和状态管理
- ✅ 动作执行后自动延迟

**使用示例：**

```python
from backend.actions.actions import ClickAction, InputTextAction
from backend.actions.handler import ActionHandler
from backend.executor import Executor

# 创建执行器和处理器
executor = Executor()
handler = ActionHandler(executor, action_delay=2.0)

# 方式 1: 使用新的 Action 对象（推荐）
click_action = ClickAction(
    element_id="button_123",
    x=500,
    y=300,
    reasoning="Click the submit button"
)
result = await handler.execute(click_action)

# 方式 2: 使用旧的字典格式（兼容）
legacy_action = {
    "_metadata": "do",
    "action": "Tap",
    "element": [500, 300]
}
result = await handler.execute(legacy_action)
```

### 3. 动作解析器改进

**文件：** `backend/actions/parser.py`

**特性：**
- ✅ 安全解析 VLM 输出（使用 AST）
- ✅ 支持 `do()` 和 `finish()` 格式
- ✅ 防止代码注入攻击
- ✅ 优雅的错误处理

### 4. 架构模式对比

| 特性 | 改进前 | 改进后（基于 Skyvern） |
|------|--------|----------------------|
| 动作定义 | 字典 | Pydantic 模型（类型安全） |
| 动作类型 | 6 种 | 20+ 种 |
| 状态跟踪 | 无 | 完整的状态机 |
| 错误处理 | 基础 | 统一的错误处理 |
| 元数据 | 最小 | 丰富（reasoning, confidence, timestamps） |
| 扩展性 | 低 | 高（继承层次清晰） |
| 测试性 | 低 | 高（类型安全，易于 mock） |

## 使用指南

### 创建自定义动作

```python
from backend.actions.actions import Action, ActionType, ActionStatus

class CustomAction(Action):
    action_type: ActionType = ActionType.CUSTOM
    custom_param: str

    def __repr__(self) -> str:
        return f"CustomAction(custom_param={self.custom_param})"
```

### 扩展 ActionHandler

```python
class CustomActionHandler(ActionHandler):
    async def _handle_custom(self, action: CustomAction) -> ActionResult:
        # 实现自定义逻辑
        logger.info(f"Handling custom action: {action.custom_param}")

        # 执行操作
        result = await self.executor.custom_operation(action.custom_param)

        return ActionResult(
            success=True,
            should_finish=False,
            message="Custom action completed"
        )
```

### API 使用

```python
# POST /step - 执行单步操作
{
    "task": "点击登录按钮",
    "screenshot_base64": "...",
    "elements": [...]
}

# POST /run - 执行完整任务
{
    "task": "登录网站并提取用户信息",
    "url": "https://example.com",
    "max_steps": 10
}
```

## 下一步改进计划

### 高优先级
- [ ] 实现 BrowserState 协议（更好的浏览器抽象）
- [ ] 添加重试逻辑和错误恢复
- [ ] 完善 WebAgent 实现
- [ ] 添加配置管理系统

### 中优先级
- [ ] 支持多 LLM 提供商（OpenAI, Anthropic, 本地模型）
- [ ] 实现文件上传/下载功能
- [ ] 添加表单操作（select, checkbox）
- [ ] 改进数据提取引擎

### 低优先级
- [ ] 添加工作流系统（类似 Skyvern 的 Block 系统）
- [ ] 实现验证码求解集成
- [ ] 添加代理和会话管理
- [ ] 性能优化（缓存、并行执行）

## 技术栈

- **浏览器自动化**: Playwright (async)
- **视觉模型**: Alibaba DashScope (Qwen3-VL)
- **UI 检测**: OmniParser (YOLO + Florence2)
- **OCR**: PaddleOCR / EasyOCR
- **Web 框架**: FastAPI
- **数据验证**: Pydantic
- **异步**: asyncio

## 参考资料

- [Skyvern](https://github.com/Skyvern-AI/skyvern) - 本项目的架构参考
- [Playwright](https://playwright.dev/python/) - 浏览器自动化
- [Pydantic](https://docs.pydantic.dev/) - 数据验证

## 贡献

欢迎提交 Issue 和 Pull Request！

## 许可证

MIT License
