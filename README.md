# GUIAgent

---

## 1. 项目是什么

GUIAgent 是一个网页自动化系统，技术栈为：

- 后端：`FastAPI + Playwright + VLM/LLM`
- 前端：`React + Vite`
- 执行模式：SSE 流式任务执行、反思重试、DOM 标注、文件导出

当前支持两类任务：

1. **普通网页任务**（如百度、B 站、豆瓣）
2. **钢铁专用任务**（历史记录筛选、导出 Excel、下载图片包、嵌图）

---

## 2. 一图看架构

```text
UI(React) -> /run_task_stream (SSE)
             |
             v
        backend/server.py
          |- 普通任务分支: ReflectionEngine + VLMService
          |- 钢铁任务分支: _run_steel_download_pipeline
          |- 定时任务分支: task_scheduler -> _run_scheduled_task
             |
             v
         Executor(Playwright)
          |- 导航/点击/输入/滚动/截图
          |- DOM 标注(mark_page_elements)
             |
             v
  下载与后处理(download_skill/postprocess_skill/mapper)
```

---

## 3. 从零启动

### 3.1 环境准备

```powershell
conda create -n agent_env python=3.10 -y
conda activate agent_env
pip install -r requirements.txt
playwright install
```

### 3.2 配置文件

```powershell
copy .env_temple .env
```

关键项（最小可运行）：

- `VLM_PROVIDER`
- `VLM_BASE_URL`
- `VLM_MODEL`
- `VLM_API_KEY`
- `DISABLE_TEXT_LLM=1`（推荐）
- `USE_DOM_ANNOTATION=1`（推荐）

### 3.3 启动服务

```powershell
uvicorn backend.server:app --host 127.0.0.1 --port 8000 --reload
cd ui
npm install
npm run dev
```

---

## 4. 注意事项

## 4.1 cookies（auth_data）怎么拿、放哪里、怎么生效

### A. 你为什么需要 auth_data

钢铁网站通常需要登录态。项目通过 `auth_data_*.json` 自动注入：

- cookies
- localStorage
- sessionStorage
- url（目标站点）

### B. 最简单的获取方式（浏览器控制台）

在目标网站登录后，打开 F12 Console 执行：

```javascript
const authData = {
  url: location.href,
  timestamp: new Date().toISOString(),
  cookies: document.cookie || null,
  localStorage: { ...localStorage },
  sessionStorage: { ...sessionStorage }
};

const blob = new Blob([JSON.stringify(authData, null, 2)], { type: 'application/json' });
const url = URL.createObjectURL(blob);
const a = document.createElement('a');
a.href = url;
a.download = `auth_data_${location.hostname}_${Date.now()}.json`;
a.click();
URL.revokeObjectURL(url);
```

把下载的 json 放到项目目录 `cookies/` 下。

### C. 后端如何选 auth_data（必须理解）

见 `backend/server.py` 的 `_resolve_auth_data_file`：

1. 若请求显式传 `auth_data_file`，用它。
2. 否则看环境变量 `STEEL_AUTH_DATA_FILE`。
3. 否则自动在 `cookies/` 里找最近的 `auth_data_*.json`。

### D. 这会导致什么问题

如果 `cookies/` 里只有钢铁站点的 auth_data，那么**普通任务也可能被误判为钢铁流**。  
这就是“让它去百度却进了钢铁 history 页”的根因之一。

### E. 日常操作建议

- 跑普通任务前：临时移走钢铁 `auth_data_*.json`。
- 跑钢铁任务前：放回对应 auth_data。
- 长期方案：后续代码改为“仅钢铁任务才读取钢铁 auth_data”。

---

## 4.2 PLAYWRIGHT_USER_DATA_DIR 是什么、如何得到

### A. 作用

`PLAYWRIGHT_USER_DATA_DIR` 是 Playwright 持久化浏览器配置目录。启用后可保留：

- 登录状态
- 浏览器偏好（例如下载栏相关设置）
- 站点缓存

### B. 开关配置

在 `.env` 中设置：

```env
PLAYWRIGHT_PERSISTENT_CONTEXT=1
PLAYWRIGHT_USER_DATA_DIR=data/playwright_user_data
PLAYWRIGHT_CHANNEL=msedge
PLAYWRIGHT_EXECUTABLE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
```

### C. 第一次初始化流程（新手照做）

1. 启动后端。
2. 执行任意任务，系统会自动创建该目录并拉起浏览器。
3. 在弹出的浏览器里手动完成一次登录（如果需要）。
4. 手动设置你希望长期保留的浏览器选项（如下载提示行为）。
5. 关闭并重新执行任务，确认设置已被复用。

### D. 清理/重置

如果浏览器状态异常，删除 `PLAYWRIGHT_USER_DATA_DIR` 指向目录，再重新初始化。

---

## 5. DOM 机制（原理 + 调试）

## 5.1 DOM 在本项目里做什么

不是“写死坐标点击”，而是：

1. 先抓 DOM 元素列表（可交互元素、文本、bbox、中心点）
2. 再截图并标注
3. 决策层根据任务 + 元素语义做动作
4. 执行层按元素信息点击/输入/滚动

核心文件：

- `backend/dom_service.py`
- `backend/executor.py`（`mark_page_elements` 与交互）
- `backend/visualizer.py`（标注渲染）

## 5.2 一次普通任务的 DOM 数据流

1. `run_task_stream` 收到任务
2. `ReflectionEngine` 请求当前页面观测
3. `executor.mark_page_elements()` 返回元素
4. 截图 + elements 传给模型生成下一步动作
5. `actions/handler` 执行动作
6. 反思验证，继续下一步

## 5.3 你怎么验证 DOM 是否正常

- 调 `POST /mark_elements` 看是否返回元素列表
- 看 `data/screenshots/` 和标注图是否生成
- 看 SSE 里每步动作是否引用了合理元素

---

## 6. 后端逐文件职责（每个 Python 文件）

> 范围：根目录脚本 + `backend/`。  
> `skyvern/`、`browser-use-main/`、`Open-AutoGLM/` 是参考代码区，不是本系统主执行链。

### 6.1 根目录

- `_date_parse_smoke.py`：日期解析烟测脚本。

### 6.2 backend 顶层模块

- `backend/__init__.py`：包初始化。
- `backend/server.py`：主入口；路由、分流、钢铁流水线、调度执行、SSE。
- `backend/executor.py`：Playwright 执行器；浏览器生命周期、页面操作、DOM 标注。
- `backend/reflection_engine.py`：普通任务反思执行循环。
- `backend/vlm_service.py`：模型调用、计划、意图抽取。
- `backend/planner.py`：任务拆解与提取规格解析。
- `backend/extraction_engine.py`：结构化抽取执行。
- `backend/dom_service.py`：DOM 抓取与元素标准化。
- `backend/omniparser_service.py`：视觉解析兼容服务。
- `backend/visualizer.py`：截图标注可视化。
- `backend/scrolling_screenshot.py`：滚动截图。
- `backend/storage.py`：目录/文件名工具。
- `backend/output_store.py`：输出索引与管理。
- `backend/schemas.py`：API schema。
- `backend/task_scheduler.py`：定时任务管理器（daily/interval）。
- `backend/workflow_profiles.py`：站点 profile 匹配与关键词策略。
- `backend/steel_workflow.py`：钢铁流程配置构建。
- `backend/download_skill.py`：下载技能（点击/回退/命名）。
- `backend/postprocess_skill.py`：解压、图片目录定位、Excel 嵌图。
- `backend/steel_excel_image_mapper.py`：Excel 行与图片映射规则。
- `backend/web_agent.py`：历史兼容封装层。

### 6.3 backend/actions 子包

- `backend/actions/__init__.py`：导出入口。
- `backend/actions/action_types.py`：动作类型定义。
- `backend/actions/actions.py`：动作对象定义。
- `backend/actions/parser.py`：模型动作解析。
- `backend/actions/handler.py`：动作执行分发。

---

## 7. 钢铁任务当前流程（操作语义）

1. 注入登录态（auth_data）
2. 导航并守护到历史记录页
3. 设置时间范围
4. 设置状态筛选（异常/正常/全部）
5. 下载原始 Excel
6. 下载图片（原始/渲染/全部）
7. 解压并生成带图 Excel
8. 输出文件与状态回传

输出命名约定：

- 文件夹：`YYYY-MM-DD-HH-MM-SS_YYYY-MM-DD-HH-MM-SS`
- 原始 Excel：保持站点原文件名
- 处理后 Excel：
  - `_orgin.xlsx`
  - `_render.xlsx`
  - `_orgin_render.xlsx`

---

## 8. 扩展到“其他钢铁网站”怎么做

## 8.1 不要直接写死

优先级应为：

1. profile 配置化
2. DOM+模型决策
3. 通用 skill 复用
4. 最后才是站点专用分支

## 8.2 扩展步骤（建议顺序）

1. 在 `workflow_profiles.py` 增加新站点匹配规则。
2. 在 `steel_workflow.py` 增加新站点阶段配置（日期、筛选、下载、校验）。
3. 在 `vlm_service.py` 强化意图抽取 schema（时间窗、筛选、下载模式）。
4. 优先复用 `download_skill.py`，减少站点写死点击。
5. 如果输出结构不同，再改 `postprocess_skill.py` / mapper。
6. 增加 smoke 任务用例并回归。

---

## 9. 如何参考 Skyvern 与 browser-use（重点）

## 9.1 借鉴点

建议借鉴：

- 状态机与动作协议
- DOM 观测与动作执行分层
- 模型输出 schema 化（强约束 JSON）
- 失败重试与恢复策略

不建议直接照抄：

- 大而全的项目脚手架
- 与本项目运行模型冲突的基础设施代码

## 9.2 对照关系（本项目 <-> 参考项目）

- 我们的 `backend/actions/*` ≈ Skyvern 的 action 系统
- 我们的 `dom_service + executor` ≈ browser-use 的 dom/service + tools
- 我们的 `reflection_engine` ≈ 参考项目中的 agent loop/retry loop
- 我们的 `download_skill/postprocess_skill` ≈ 站点任务 skill 层

## 9.3 推荐阅读顺序

先读本项目：

1. `backend/server.py`
2. `backend/executor.py`
3. `backend/download_skill.py`
4. `backend/steel_workflow.py`
5. `backend/reflection_engine.py`

再对照参考：

- Skyvern：`skyvern/skyvern/webeye/actions/*`、`skyvern/skyvern/library/*`
- browser-use：`browser-use-main/browser_use/dom/*`、`browser-use-main/browser_use/tools/*`

---

## 10. 定时任务说明（当前边界）

- 自动 schedule 触发依赖任务文本有“每天/定时/每隔”等语义。
- 当前调度执行链路主要用于钢铁流。
- 时间窗支持示例：
  - “昨天一整天”
  - “昨天8:00-16:00”
  - “昨天0点到昨天下午四点”

---

## 11. 排障手册

### 11.1 普通任务总跳到钢铁 history

检查顺序：

1. 请求是否传了钢铁 `target_url`
2. `.env` 是否设置 `STEEL_TARGET_URL` / `STEEL_AUTH_DATA_FILE`
3. `cookies/` 下是否只有钢铁 auth_data（被自动回填）

### 11.2 下载后卡住或 about:blank 干扰

检查顺序：

1. 是否启用 persistent context
2. 下载栏/下载气泡是否遮挡页面交互
3. 空白页清理逻辑是否生效

### 11.3 筛选失效

检查顺序：

1. before/after observe 的状态值
2. DOM probe 是否拿到筛选结果
3. recover 阶段是否正确回放

---

## 12. 上线前最低验证

```powershell
conda run -n agent_env python -m py_compile backend/server.py backend/executor.py backend/download_skill.py backend/task_scheduler.py
cd ui
npm run build
```

---


## 15. 改进方向（从多流到全网站通用）

当前状态：系统仍有“普通流 / 钢铁流”分支。  
最终目标：把“下载、导出 Excel、筛选、提取、后处理”等能力做成通用 skill，由 LLM 按任务选择性调用，实现跨网站复用。

### 15.1 目标架构

- 单一 Agent Runtime（不按网站写大量分叉逻辑）
- LLM 负责：
  - 选择 skill
  - 填写 skill 参数
  - 给出预期验证条件
- 后端负责：
  - 执行 skill
  - 做结果校验
  - 失败恢复与重试

### 15.2 技能化拆分（建议第一批）

- `navigate_skill`：导航与页面就绪检测
- `date_range_skill`：设置时间范围
- `filter_skill`：筛选（状态、关键字、下拉选项）
- `export_excel_skill`：触发导出并确认文件
- `download_asset_skill`：图片/附件下载（含候选点击与回退）
- `extract_table_skill`：表格/卡片结构化提取
- `postprocess_skill`：解压、重命名、嵌图、映射

### 15.3 执行闭环（统一协议）

每一步固定 5 个阶段：

1. 观察（DOM + 页面状态）
2. 决策（LLM 输出 `skill + args + success_criteria`）
3. 执行（skill 运行）
4. 验证（是否满足 success_criteria）
5. 失败恢复（重试/换 skill/回退路径）

### 15.4 分阶段落地计划

- **阶段 A（收敛分叉）**  
  把钢铁流内部动作替换为 skill 调用，保留现有 API，不改前端。

- **阶段 B（统一调度）**  
  定时任务与即时任务都走同一 skill runtime，调度只负责触发，不负责业务分流。

- **阶段 C（配置化扩站）**  
  新网站仅新增 profile + 少量提示词/映射配置，不改核心执行器。

### 15.5 验收标准

- 新增网站时，不改 `executor` / `server` 主流程代码即可完成基础任务。
- 同类任务（下载、导出、筛选）在不同网站复用同一 skill。
- LLM 输出均可被 schema 校验，不依赖自由文本猜测。
- 失败路径可追踪：每次失败都能定位到 skill、参数、验证点。
