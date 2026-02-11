# Backend

GUIAgent 后端，基于 FastAPI 提供网页自动化、步骤执行与数据提取能力。

## 功能概览

- 浏览器自动化执行（Playwright）
- 自然语言任务规划与步骤执行
- 流式任务反馈（SSE）
- 页面元素标注与可视化
- 列表/详情页数据提取与 Excel 导出

## 启动方式

在项目根目录执行：

```powershell
uvicorn backend.server:app --host 127.0.0.1 --port 8000 --reload
```

说明：当前后端会导入 `OmniParserService`，因此建议按根目录 `requirements.txt` 完整安装依赖。
主流程默认优先使用 DOM 标注（`USE_DOM_ANNOTATION=1`），OmniParser 主要用于兼容和可选路径。

接口文档：
- `http://127.0.0.1:8000/docs`

## 关键模块

- `server.py`：API 入口与任务编排
- `executor.py`：浏览器执行器
- `planner.py`：任务规划
- `reflection_engine.py`：步骤重试/反思执行
- `vlm_service.py`：VLM 调用封装
- `output_store.py`：提取结果存储与导出
- `actions/`：动作模型与处理器

## 接口说明（常用）

- `GET /run_task_stream`
  - 实时流式执行任务，返回步骤和提取进度

- `POST /run_task`
  - 非流式执行任务，返回最终结果

- `POST /mark_elements`
  - 标注当前页面可交互元素

- `GET /files`
  - 获取导出文件列表

- `GET /files/{filename}`
  - 下载导出文件

## 环境变量

请使用项目根目录的 `.env_temple` 作为模板创建 `.env`。
