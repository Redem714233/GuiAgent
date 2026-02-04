# GUIAgent - 智能浏览器自动化系统

## 项目简介
一个基于视觉理解的GUI自动化系统，能够自动操作浏览器完成复杂任务，如数据提取、网页导航等。

**核心技术栈**:
- **OmniParser**: YOLO + OCR + Florence2 实现屏幕元素识别
- **VLM (Qwen3-VL)**: 视觉语言模型进行决策和数据提取
- **Playwright**: 浏览器自动化执行
- **FastAPI**: 后端服务
- **React**: 前端交互界面

## Setup
Create/activate your Python env, then install deps:

```powershell
pip install -r requirements.txt
python -m playwright install
```

If you need extra DLL paths (CUDA/cuDNN), set:

```powershell
$env:OMNIPARSER_EXTRA_PATHS="C:\path\to\env\bin;C:\path\to\env\Library\bin"
```

Ensure your OpenAI key is set:

```powershell
$env:OPENAI_API_KEY="your_key_here"
```

Optional: run Playwright on Edge

```powershell
$env:PLAYWRIGHT_CHANNEL="msedge"
# or use explicit path
$env:PLAYWRIGHT_EXECUTABLE="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

## Run

```powershell
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000
```

## API

- `POST /parse` with JSON body:
  - `image_path` or `image_base64` (PNG/JPG)
  - returns `elements[]` and `annotated_image_base64`
- `POST /plan` with `{ task, elements[] }`
- `POST /step` with `{ task }` or `{ task, override_point: [x,y] }`
- `POST /task_spec` with `{ task }` returns a task specification JSON
- `POST /extract` with `{ task, spec, mode }` returns extracted list/detail data (image-based)
- `POST /append_row` with `{ row }` appends a row to the in-memory output table
- `POST /save_output` with `{ file_name? }` writes Excel to `data/outputs/` (no overwrite)
- `GET /files` lists output files
- `GET /files/{name}` downloads a file

## Output files
Excel outputs are saved under `data/outputs/` with timestamp+UUID names by default. Existing files are never overwritten.

---

## 架构设计

### 核心流程
```
用户输入任务
  ↓
VLM规划步骤 (plan_steps)
  ↓
逐步执行循环:
  1. 截图 (Playwright)
  2. 解析元素 (OmniParser: YOLO + OCR + Florence2)
  3. VLM决策动作 (decide: 点击/输入/滚动等)
  4. 执行动作 (Playwright)
  5. 检查是否完成 (should_finish)
  ↓
任务完成
```

### 关键设计决策

#### 1. 视觉优先策略
**问题**: Florence2生成的元素文本描述存在偏差，容易误导VLM点击错误元素。

**解决方案**:
- VLM主要依赖**标注后的截图**进行决策
- 元素文本列表仅作为辅助参考
- 可通过环境变量 `VLM_DISABLE_ELEMENTS=1` 完全禁用文本元素

**代码位置**:
- [backend/vlm_service.py](backend/vlm_service.py) - VLM决策提示词强调"image as primary source"
- [backend/planner.py](backend/planner.py:85) - 支持禁用元素列表

#### 2. 工具化动作系统
支持多种动作类型：`click`, `type`, `press`, `wait`, `copy`, `goto`, `scroll`

每个动作可携带参数：
- `point`: 归一化坐标 [0,1] 或像素坐标
- `id`: 元素ID（从解析结果中选择）
- `text`: 输入文本
- `key`: 按键（如 "Enter"）
- `url`: 跳转URL
- `scroll`: 滚动像素数

### 目录结构
```
GUIAgent/
├── backend/              # FastAPI后端
│   ├── server.py        # API端点
│   ├── omniparser_service.py  # OmniParser封装
│   ├── vlm_service.py   # VLM服务（Qwen3-VL）
│   ├── planner.py       # 任务规划器
│   ├── executor.py      # Playwright执行器
│   ├── output_store.py  # Excel输出管理
│   └── schemas.py       # 数据模型
├── ui/                  # React前端
│   └── src/App.tsx      # 主界面
├── models/
│   └── OmniParser/      # OmniParser模型
└── data/
    ├── screenshots/     # 截图存储
    └── outputs/         # Excel输出
```

---

## 开发路线图

### ✅ 已完成功能

#### 后端
- [x] OmniParser集成（YOLO + OCR + Florence2）
- [x] VLM决策服务（Qwen3-VL）
- [x] Playwright浏览器控制
- [x] 步骤规划 (`/plan_steps`)
- [x] 单步执行 (`/step`)
- [x] 任务规格解析 (`/task_spec`)
- [x] 页面数据提取 (`/extract`)
- [x] Excel输出 (`/append_row`, `/save_output`)
- [x] **自动化数据提取引擎** (`/run_extraction`)
  - 端到端提取流程（导航→列表提取→详情提取→保存）
  - 支持滚动/翻页检测
  - URL去重和标准化
  - **OmniParser开关**：可选择使用标注图或原始截图
  - **仅列表模式**：跳过详情页提取（快速模式）
  - 详情页超时保护（30秒/页）
  - 详细错误日志

#### 前端
- [x] 任务输入界面
- [x] 步骤可视化
- [x] 调试面板（显示VLM请求/响应）
- [x] 手动点击模式
- [x] 文件下载列表
- [x] **数据提取UI**
  - 提取数量设置（1-100）
  - "使用标注图"开关
  - "仅列表(快速)"开关
  - 提取结果面板（状态、进度、文件下载）
  - 数据预览表格

### 🚧 开发中功能

#### 阶段1: 自动化数据提取工作流 ✅ (已完成)
**状态**: 核心功能已实现

**已实现**:
- ✅ ExtractionEngine类 ([backend/extraction_engine.py](backend/extraction_engine.py))
- ✅ `/run_extraction` 端点
- ✅ 状态机管理（导航→列表提取→详情提取→保存）
- ✅ 滚动/翻页检测
- ✅ URL去重和标准化
- ✅ OmniParser开关（标注图 vs 原始截图）
- ✅ 仅列表模式（快速提取）
- ✅ 详情页超时保护（30秒/页）
- ✅ 错误恢复和详细日志

**性能对比**:
- 仅列表 + 原始截图: ~15秒/10条 (最快)
- 仅列表 + 标注图: ~25秒/10条
- 详情页 + 原始截图: ~80秒/10条
- 详情页 + 标注图: ~150秒/10条

#### 阶段2: 增强VLM提取能力 ✅ (已完成)
**改进点**:
1. ✅ **列表提取提示词优化** ([backend/vlm_service.py](backend/vlm_service.py))
   - 精确的字段识别（标题、URL、时间、作者等）
   - 结构化输出格式
   - 支持多种内容类型（新闻、视频、评论等）
   - **禁止编造数据**：强调"仅提取可见字段"

2. ✅ **详情提取提示词优化**
   - 根据任务规格动态生成提取字段
   - 支持多种内容类型
   - 明确要求省略不可见字段

3. ✅ **添加提取示例**
   ```python
   # 新闻列表示例
   {
     "items": [
       {"title": "新闻标题", "url": "https://...", "time": "2024-01-01", "source": "新浪新闻"},
       ...
     ],
     "next": "scroll" | "next_page" | "stop"
   }

   # 详情页示例
   {
     "data": {
       "title": "完整标题",
       "content": "正文内容...",
       "author": "作者",
       "time": "发布时间",
       "comments_count": 123
     }
   }
   ```

#### 阶段3: 前端提取UI ✅ (已完成)
**已实现** ([ui/src/App.tsx](ui/src/App.tsx)):
1. ✅ **提取任务面板**
   - 任务输入框
   - 目标数量设置（1-100）
   - "使用标注图"复选框
   - "仅列表(快速)"复选框

2. ✅ **提取结果显示**
   - 状态显示（成功/失败）
   - 已提取数量
   - 文件下载链接
   - 错误信息列表

3. ✅ **数据预览表格**
   - 实时显示已提取的数据
   - 支持查看所有字段

#### 阶段4: 鲁棒性增强 🚧 (进行中)
- [x] 详情页加载超时处理（30秒）
- [x] VLM返回格式验证
- [x] 详细错误日志
- [ ] 断点续传（保存中间状态）
- [ ] 反爬虫对策（随机延迟、User-Agent轮换）
- [ ] 元素未找到重试机制

#### 阶段5: 测试用例
**目标场景**:
1. ✅ **新浪新闻提取**
   - 任务: "进入新浪新闻，复制今天的前10条新闻标题和内容"
   - 模式: 列表提取
   - 字段: 标题、URL、摘要、时间

2. ✅ **B站评论提取**
   - 任务: "打开哔哩哔哩，随机进入一个视频，复制最上方的一条评论"
   - 模式: 导航 + 详情提取
   - 字段: 评论内容、用户名、点赞数、时间

---

## 目标用例

### 用例1: 新闻数据提取
```
任务: "进入新浪新闻，复制今天的前10条新闻标题和内容，输出到Excel"

执行流程:
1. 导航到 news.sina.com.cn
2. 识别新闻列表
3. 提取每条新闻的标题、链接、摘要
4. 可选: 点击进入详情页提取完整内容
5. 保存到 Excel (标题 | URL | 内容 | 时间)
```

### 用例2: 视频评论提取
```
任务: "打开哔哩哔哩，随机进入一个视频，复制最上方的一条评论"

执行流程:
1. 导航到 bilibili.com
2. 点击首页推荐视频
3. 滚动到评论区
4. 提取第一条评论（内容、用户名、点赞数）
5. 保存到 Excel
```

---

## 技术细节

### VLM配置
```powershell
# 使用Qwen3-VL (推荐)
$env:VLM_PROVIDER="qwen3-vl"
$env:VLM_MODEL="qwen3-vl-flash"
$env:VLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:VLM_API_KEY="your_dashscope_key"

# 禁用元素文本列表（仅使用图像）
$env:VLM_DISABLE_ELEMENTS="1"

# 禁用文本LLM（仅使用VLM）
$env:DISABLE_TEXT_LLM="1"
```

### Playwright配置
```powershell
# 浏览器设置
$env:PLAYWRIGHT_CHANNEL="msedge"  # 或 "chrome"
$env:PLAYWRIGHT_START_URL="https://www.bing.com"

# 视口大小
$env:PLAYWRIGHT_VIEWPORT_WIDTH="1280"
$env:PLAYWRIGHT_VIEWPORT_HEIGHT="720"
$env:PLAYWRIGHT_DEVICE_SCALE_FACTOR="1"
```

### OmniParser配置
```powershell
# CUDA/cuDNN路径（Windows）
$env:OMNIPARSER_EXTRA_PATHS="C:\path\to\env\bin;C:\path\to\env\Library\bin"

# 避免库冲突
$env:KMP_DUPLICATE_LIB_OK="TRUE"
$env:FLAGS_use_onednn="0"
```

---

## 性能优化建议

1. **OmniParser加速**
   - 使用GPU加速（CUDA）
   - 调整 `imgsz` 参数（默认640，可提高到1280）
   - 使用PaddleOCR（比EasyOCR快）

2. **VLM调用优化**
   - 使用 `qwen3-vl-flash` 模型（速度快）
   - 降低图片分辨率（保持可读性前提下）
   - 批量处理时添加延迟避免限流

3. **浏览器性能**
   - 禁用不必要的资源加载（图片、视频）
   - 使用无头模式（headless）
   - 复用浏览器实例

---

## 常见问题

### Q: Florence2文本描述不准确怎么办？
A: 这是已知问题。设置 `VLM_DISABLE_ELEMENTS=1` 让VLM仅依赖图像进行决策。

### Q: VLM点击错误元素？
A: 检查标注图片质量，确保元素ID清晰可见。可以调整OmniParser的 `box_threshold` 和 `iou_threshold`。

### Q: 提取的数据格式不对？
A: 改进VLM提示词，添加更多示例。或者在后端添加数据验证和格式化逻辑。

### Q: 如何处理动态加载的内容？
A: 使用 `scroll` 动作触发加载，配合 `wait` 动作等待内容加载完成。

### Q: 数据提取很慢怎么办？
A:
1. **取消"使用标注图"**：可节省50%时间（OmniParser处理需5-10秒/页）
2. **勾选"仅列表(快速)"**：跳过详情页提取，速度提升5-10倍
3. **减少提取数量**：先测试少量数据（如5条）验证效果
4. **降低视口分辨率**：在环境变量中设置更小的viewport（如960x540）

### Q: 详情页提取卡住怎么办？
A:
- 系统已添加30秒超时保护，超时会自动跳过并记录错误
- 检查错误日志了解具体失败原因
- 对于复杂页面，建议使用"仅列表"模式

### Q: VLM提取的数据不完整或有编造的字段？
A:
- 最新版本已优化提示词，强调"仅提取可见字段"
- VLM只能识别图片中清晰可见的文字
- 如果字段不可见，VLM会省略该字段（不会编造）
- 建议使用"仅列表"模式获取基本信息，避免详情页提取失败

### Q: 如何提取URL不可见的网站（如B站视频）？
A:
- **当前限制**：`/run_extraction` 仅支持URL可见的场景（如新闻列表）
- **解决方案1**：使用交互式流程（"Run"按钮 + "下一步"），让VLM通过点击进入详情页
- **解决方案2**（未来）：增强提取引擎支持点击操作（需要开发）

---

## 贡献指南

欢迎提交Issue和Pull Request！

**开发优先级**:
1. 🔥 提取工作流引擎（最高优先级）
2. 🔥 VLM提取提示词优化
3. 前端提取UI
4. 鲁棒性增强
5. 性能优化

---
