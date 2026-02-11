# Steel 任务自动化交接（2026-02-10）

## 0) 你在做什么（业务目标说明）
- 目标是把你生产环境的“钢铁生产打包带系统检查”流程做成 **前端自然语言一键自动化**。
- 目标流程（对应 `goal/step1.png` ~ `goal/step6.png`）：
  1) 进入站点（复用登录态）。
  2) 选择日期范围（某天 00:00:00 ~ 23:59:59）。
  3) 将“打包状态”筛选为“异常”。
  4) 点击“数据导出”，下载当日异常数据 Excel。
  5) 点击“图片下载”，选择“原始图片”，下载 zip。
  6) 自动解压 zip，按“图片反序对应 Excel 序号”规则，把图片嵌入 Excel 新列“原始图片”，生成最终带图 Excel。

---

## 1) 关键业务事实（已修正）
- 四个按钮（数据导出 / 图片下载 / 视频提取 / 视频下载）在初始状态不可点击，这 **是正常行为**。
- 正确启用条件是：
  - 先选择时间范围；
  - 页面出现“已选择筛选结果中 N 条数据”与对应时间区间提示；
  - 此时四个按钮可用。
- 下载前仍需要把“打包状态”切换为“异常”。
- **不应把“先勾选表格首行”当作业务前提**。

> 备注：当前代码里仍保留了“勾选首行再导出”的实现分支，这是历史调试策略，不是你的真实业务规则。

---

## 2) 当前代码进展（已完成）
- 已接入统一入口：`/run_task` 与 `/run_task_stream` 都可走 steel pipeline。
- 已接入登录态复用：支持 `auth_data`（cookie + localStorage/sessionStorage 注入）。
- 已支持 SSE 阶段事件：前端可看到 `steel_stage` 实时进度。
- 已有下载后处理脚本：`backend/steel_excel_image_mapper.py`，支持把“原始图片”真正嵌入 Excel（不是只写文件名）。
- Excel 下载链路已可跑通；zip 下载链路仍需继续稳固。

---

## 3) 当前主要阻塞
- `backend/server.py` 的 steel pipeline 仍有“先勾选首行”的硬依赖：
  - `_run_steel_download_pipeline(...)` 内调用 `_select_first_table_row_for_export()`。
- zip 下载不稳定的核心在“图片下载 -> 原始图片”菜单项命中与触发：
  - 顶部按钮 / 行内按钮可能混淆；
  - 下拉菜单可能是懒渲染（Element Plus `el-popper` / `el-dropdown-menu`）。

---

## 4) 下一窗口优先事项（按顺序）
1. 去掉“勾选首行”作为前置条件（与业务事实对齐）。
2. 增强“图片下载 + 原始图片”DOM 策略：
   - 强制优先命中顶部操作区按钮；
   - 等待菜单出现后点“原始图片”；
   - 如菜单项仅切换模式，则二次触发“图片下载”并 `expect_download`。
3. 在 `agent_env` 做端到端验证：
   - 输出 `raw_export.xlsx`、`raw_images.zip`、最终 `steel_with_images_*.xlsx`。
4. 再走前端自然语言验证（`/run_task_stream` + `steel_stage`）。

---

## 5) 已确认的运行约束
- 验证命令必须在 `conda` 环境 `agent_env` 下执行。
- 登录态文件使用：
  - `cookies/auth_data_vision.lg.china-yongfeng.com_1770702561198.json`

---

## 6) 快速定位（下个窗口直接看）
- pipeline 主流程：`backend/server.py`
- 嵌图脚本：`backend/steel_excel_image_mapper.py`
- 前端流式状态显示：`ui/src/App.tsx`
- 调试产物：`data/outputs/steel_debug_*.json`

