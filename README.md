# GUIAgent

GUIAgent 是一个基于 `FastAPI + Playwright + VLM` 的网页自动化与数据提取项目。

本项目适合做：
- 自然语言驱动的网页操作
- 列表页与详情页信息提取
- 实时执行过程可视化（SSE）
- Excel 数据导出

---

## 1. 项目结构

```text
GUIagent/
├─ backend/                 # 后端服务与执行引擎
├─ ui/                      # 前端（React + Vite）
├─ docs/                    # 设计与实现文档
├─ data/                    # 运行产物（截图、输出）
├─ requirements.txt         # Python 依赖
└─ .env_temple              # 环境变量模板
```

---

## 2. 快速开始（新人接手）

## 2.1 克隆项目

```powershell
git clone <your-repo-url>
cd GUIagent
```

## 2.2 准备 Python 环境

建议 Python 3.10+：

```powershell
conda create -n agent_env python=3.10 -y
conda activate agent_env
pip install -r requirements.txt
```

## 2.3 安装 Playwright 浏览器驱动

```powershell
playwright install
```

## 2.4 OmniParser 说明（重要）

当前代码虽然默认走 DOM 标注（`USE_DOM_ANNOTATION=1`），但后端仍会导入 OmniParser 相关模块，
因此建议完整安装 `requirements.txt` 中的依赖。

如果你使用了独立的 OmniParser 环境或 DLL 路径，按需设置：

```env
OMNIPARSER_EXTRA_PATHS=
```

Windows 下可填多个路径，用分号分隔（例如 CUDA / cuDNN 路径）。

## 2.5 配置环境变量

```powershell
copy .env_temple .env
```

然后编辑 `.env`（下节有逐项说明）。

## 2.6 启动后端

```powershell
uvicorn backend.server:app --host 127.0.0.1 --port 8000 --reload
```

后端文档地址：
- `http://127.0.0.1:8000/docs`

## 2.7 启动前端

```powershell
cd ui
npm install
npm run dev
```

前端默认地址：
- `http://127.0.0.1:5173`

---

## 3. 环境变量填写说明（含 1/0 约定）

以下为推荐配置（VLM-only）：

### 3.1 必填

- `VLM_PROVIDER`：VLM 提供商标识（如 `dashscope`）
- `VLM_BASE_URL`：VLM OpenAI-compatible 接口地址
- `VLM_MODEL`：VLM 模型名
- `VLM_API_KEY`：VLM API Key

### 3.2 关键开关（1/0）

- `DISABLE_TEXT_LLM`
  - `1`：禁用文本 LLM（推荐）
  - `0`：启用文本 LLM（需配置 `OPENAI_*` 或 `DEEPSEEK_*`）

- `USE_DOM_ANNOTATION`
  - `1`：使用 DOM 标注（推荐）
  - `0`：走 OmniParser 标注路径

- `VLM_DISABLE_ELEMENTS`
  - `0`：把元素文本列表也给 VLM（推荐）
  - `1`：仅看截图，不使用 elements 文本

### 3.3 建议填写

- `PLAYWRIGHT_CHANNEL`：如 `msedge` / `chrome`
- `PLAYWRIGHT_VIEWPORT_WIDTH`：推荐 `1280`
- `PLAYWRIGHT_VIEWPORT_HEIGHT`：推荐 `720`
- `PLAYWRIGHT_DEVICE_SCALE_FACTOR`：推荐 `1`
- `GUIAGENT_MAX_STEPS`：默认最大步骤数，推荐 `5`

### 3.4 可选（仅当 `DISABLE_TEXT_LLM=0`）

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `DEEPSEEK_BASE_URL`
- `OPENAI_MODEL`

### 3.5 可选路径

- `PLAYWRIGHT_EXECUTABLE`：浏览器可执行文件路径（通常可留空）
- `PLAYWRIGHT_START_URL`：启动默认页面（可留空）
- `OMNIPARSER_EXTRA_PATHS`：OmniParser 运行补充路径（可留空）

---

## 4. 使用方式

### 4.1 前端方式（推荐）

在 UI 输入自然语言任务，点击执行。
前端默认调用流式接口：

- `GET /run_task_stream`

### 4.2 API 方式

- `GET /run_task_stream`：SSE 实时执行
- `POST /run_task`：非流式执行（一次性返回）
- `POST /mark_elements`：标注页面元素
- `GET /files`：获取输出文件列表
- `GET /files/{filename}`：下载输出文件

---

## 5. 文档导航（实现细节）

以下文档可帮助新人快速理解核心逻辑：

- [QUICK_START](docs/QUICK_START.md)：快速上手
- [QUICKSTART_REFLECTION](docs/QUICKSTART_REFLECTION.md)：反思机制快速上手
- [REFLECTION_MECHANISM](docs/REFLECTION_MECHANISM.md)：反思机制设计
- [REFLECTION_SUMMARY](docs/REFLECTION_SUMMARY.md)：反思机制总结
- [EXTRACT_DATA_WITH_REFLECTION](docs/EXTRACT_DATA_WITH_REFLECTION.md)：提取 + 反思流程
- [PAGINATION_FEATURE](docs/PAGINATION_FEATURE.md)：分页能力说明
- [PAGINATION_IMPLEMENTATION](docs/PAGINATION_IMPLEMENTATION.md)：分页实现细节
- [PAGINATION_DEBUG](docs/PAGINATION_DEBUG.md)：分页调试记录
- [ANTI_CRAWLER](docs/ANTI_CRAWLER.md)：反爬策略
- [V3_RELEASE_NOTES](docs/V3_RELEASE_NOTES.md)：版本改动说明


---

## 6. 提交前检查

```powershell
conda run -n agent_env python -m py_compile backend/server.py backend/executor.py backend/omniparser_service.py
cd ui
npm run build
```

并请确保不要提交：
- `.env`
- cookie / auth 文件
- 私有业务数据与敏感站点信息
