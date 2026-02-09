# 反思机制实现文档

## 概述

实现了类似 Skyvern 的反思机制，核心流程是：**规划 → 执行 → 验证 → 决策（重试/继续/完成）**

## 核心组件

### 1. ReflectionEngine (backend/reflection_engine.py)

反思引擎负责整个执行流程的协调。

**主要方法**：

#### `run_task_with_reflection(task: str)`
执行任务的主入口

```python
流程：
1. 规划步骤 (plan_steps)
   - 调用 VLM 将任务分解为多个步骤
   - 例如："访问网站，翻到第二页" → ["goto https://...", "click next page button"]

2. 逐步执行
   - 对每个步骤调用 _execute_step_with_retry()
   - 带重试机制

3. 总结结果
   - 统计成功/失败步骤
   - 返回完整执行历史
```

#### `_execute_step_with_retry(step_description: str)`
执行单个步骤，带重试机制

```python
流程：
1. 捕获执行前状态
   - URL
   - 截图
   - 页面元素列表

2. 执行动作
   - 调用 _execute_single_action()
   - 使用 VLM 决策具体动作（click/type/goto等）

3. 等待页面稳定
   - sleep(2秒)
   - wait_for_stable(2000ms)

4. 捕获执行后状态
   - URL
   - 截图
   - 页面元素列表

5. 验证执行结果
   - 调用 VLM.verify_step_success()
   - 对比前后状态
   - 判断是否成功

6. 决策下一步
   - 成功 → 返回成功
   - 失败但可重试 → retry_index++，继续循环
   - 失败且不可重试 → 返回失败
```

### 2. VLMService.verify_step_success() (backend/vlm_service.py)

验证步骤是否成功执行

**输入**：
- task: 总任务描述
- step_description: 当前步骤描述
- action_taken: 执行的动作
- before_url / after_url: 前后URL
- before_image_base64 / after_image_base64: 前后截图
- elements_before / elements_after: 前后元素列表

**输出**：
```json
{
  "success": bool,
  "reasoning": str,
  "status": "success" | "failed" | "uncertain",
  "should_retry": bool,
  "next_action": "continue" | "retry" | "complete"
}
```

**验证标准**：
1. **URL变化**：
   - 导航/点击 → URL应该变化
   - 输入/表单 → URL可能不变
   - 翻页 → URL应该变化到下一页

2. **页面内容变化**：
   - 对比前后元素数量
   - 检查新内容是否出现
   - 检查预期元素是否可见

3. **视觉变化**：
   - 对比前后截图
   - 查找视觉成功指标

**示例**：
```
- 点击"next"按钮 → URL从page-1变为page-2 → success=true, next_action='continue'
- 点击"next"按钮 → URL未变化 → success=false, should_retry=true, next_action='retry'
- 输入搜索框 → 输入框有文本 → success=true, next_action='continue'
- 点击链接 → 404错误页 → success=false, should_retry=false, next_action='complete'
```

## API端点

### POST /run_with_reflection

使用反思机制执行任务

**请求**：
```json
{
  "task": "访问https://books.toscrape.com/index.html，翻到第二页",
  "max_steps": 10,
  "max_retries_per_step": 3
}
```

**响应**：
```json
{
  "status": "success" | "partial" | "failed",
  "steps": [
    {
      "step_index": 0,
      "retry_index": 0,
      "description": "goto https://books.toscrape.com/index.html",
      "action": "goto https://books.toscrape.com/index.html",
      "before_url": "about:blank",
      "after_url": "https://books.toscrape.com/index.html",
      "verification": {
        "success": true,
        "reasoning": "URL changed to target site",
        "status": "success",
        "should_retry": false,
        "next_action": "continue"
      },
      "status": "success"
    },
    {
      "step_index": 1,
      "retry_index": 0,
      "description": "click next page button",
      "action": "click element 37",
      "before_url": "https://books.toscrape.com/index.html",
      "after_url": "https://books.toscrape.com/catalogue/page-2.html",
      "verification": {
        "success": true,
        "reasoning": "URL changed from index.html to page-2.html, indicating successful pagination",
        "status": "success",
        "should_retry": false,
        "next_action": "continue"
      },
      "status": "success"
    }
  ],
  "final_url": "https://books.toscrape.com/catalogue/page-2.html",
  "reasoning": "Completed 2/2 steps successfully, 0 failed",
  "plan": [
    "goto https://books.toscrape.com/index.html",
    "click next page button"
  ]
}
```

## 测试方法

### 方法1：使用测试脚本

```bash
python test/test_reflection.py
```

**预期输出**：
```
[OK] Browser started

============================================================
Testing Reflection Mechanism with Pagination
============================================================

Task: 访问https://books.toscrape.com/index.html，翻到第二页

============================================================
Execution Result
============================================================
Status: success
Final URL: https://books.toscrape.com/catalogue/page-2.html
Reasoning: Completed 2/2 steps successfully, 0 failed

Planned Steps:
  1. goto https://books.toscrape.com/index.html
  2. click next page button

Execution History:

  ✓ Step 1 (Retry 0): goto https://books.toscrape.com/index.html
    Action: goto https://books.toscrape.com/index.html
    URL: about:blank → https://books.toscrape.com/index.html
    Verification: URL changed to target site
    Status: success

  ✓ Step 2 (Retry 0): click next page button
    Action: click element 37
    URL: https://books.toscrape.com/index.html → https://books.toscrape.com/catalogue/page-2.html
    Verification: URL changed from index.html to page-2.html, indicating successful pagination
    Status: success

[SUCCESS] Successfully navigated to page 2!
```

### 方法2：使用API

```bash
curl -X POST http://127.0.0.1:8000/run_with_reflection \
  -H "Content-Type: application/json" \
  -d '{
    "task": "访问https://books.toscrape.com/index.html，翻到第二页",
    "max_steps": 10,
    "max_retries_per_step": 3
  }'
```

### 方法3：前端集成（待实现）

在前端添加一个新按钮 "Run with Reflection"，调用 `/run_with_reflection` 端点。

## 与现有模式的对比

### Run 模式（现有）
```
流程：
1. 规划步骤
2. 逐步执行
3. 检查是否完成（should_finish）
4. 继续下一步

问题：
- 没有验证机制
- 不知道步骤是否真的成功
- 无法重试失败的步骤
```

### Extract Data 模式（现有）
```
流程：
1. 解析任务规格
2. 导航到目标网站
3. 循环提取数据
   - 标记元素
   - VLM提取
   - 根据next_action决定（scroll/next_page/stop）
4. 保存到Excel

问题：
- 没有验证翻页是否成功
- 如果翻页失败，会继续提取（可能重复提取第一页）
```

### Reflection 模式（新）
```
流程：
1. 规划步骤
2. 逐步执行
   - 捕获前状态
   - 执行动作
   - 捕获后状态
   - 验证是否成功
   - 决策：成功→继续，失败→重试或终止
3. 返回完整执行历史

优势：
✅ 有验证机制
✅ 知道每步是否成功
✅ 自动重试失败的步骤
✅ 详细的执行历史
✅ 更可靠的翻页
```

## 工作流程图

```
┌─────────────────────────────────────────────────────────────┐
│           run_task_with_reflection(task)                    │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  阶段1: 规划步骤                                            │
│  plan_steps(task) → ["step1", "step2", ...]                │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  阶段2: 逐步执行（for each step）                           │
└─────────────────────────────────────────────────────────────┘
                            ↓
        ┌───────────────────────────────────────┐
        │  _execute_step_with_retry(step)       │
        └───────────────────────────────────────┘
                            ↓
        ┌───────────────────────────────────────┐
        │  retry_index = 0                      │
        │  while retry_index < max_retries:     │
        └───────────────────────────────────────┘
                            ↓
        ┌───────────────────────────────────────┐
        │  1. 捕获前状态                        │
        │     - before_url                      │
        │     - before_screenshot               │
        │     - before_elements                 │
        └───────────────────────────────────────┘
                            ↓
        ┌───────────────────────────────────────┐
        │  2. 执行动作                          │
        │     _execute_single_action()          │
        │     - 标记元素                        │
        │     - VLM决策动作                     │
        │     - 执行（click/type/goto等）       │
        └───────────────────────────────────────┘
                            ↓
        ┌───────────────────────────────────────┐
        │  3. 等待页面稳定                      │
        │     sleep(2s) + wait_for_stable(2s)   │
        └───────────────────────────────────────┘
                            ↓
        ┌───────────────────────────────────────┐
        │  4. 捕获后状态                        │
        │     - after_url                       │
        │     - after_screenshot                │
        │     - after_elements                  │
        └───────────────────────────────────────┘
                            ↓
        ┌───────────────────────────────────────┐
        │  5. 验证执行结果                      │
        │     verify_step_success()             │
        │     - 对比URL变化                     │
        │     - 对比元素变化                    │
        │     - 对比视觉变化                    │
        │     → {success, should_retry, ...}    │
        └───────────────────────────────────────┘
                            ↓
                ┌───────────┴───────────┐
                ↓                       ↓
        ┌───────────────┐       ┌───────────────┐
        │ success=true  │       │ success=false │
        └───────────────┘       └───────────────┘
                ↓                       ↓
        ┌───────────────┐       ┌───────────────┐
        │ 返回成功      │       │ should_retry? │
        │ status=success│       └───────────────┘
        └───────────────┘               ↓
                                ┌───────┴───────┐
                                ↓               ↓
                        ┌───────────┐   ┌───────────┐
                        │ Yes       │   │ No        │
                        └───────────┘   └───────────┘
                                ↓               ↓
                        ┌───────────┐   ┌───────────┐
                        │retry_index│   │返回失败   │
                        │++         │   │status=    │
                        │继续循环   │   │failed     │
                        └───────────┘   └───────────┘
                                ↓
                        ┌───────────────┐
                        │达到max_retries│
                        │返回失败       │
                        └───────────────┘
```

## 关键改进点

1. ✅ **验证机制**：每步执行后都会验证是否成功
2. ✅ **重试机制**：失败的步骤会自动重试（最多3次）
3. ✅ **状态对比**：对比执行前后的URL、截图、元素列表
4. ✅ **智能决策**：VLM判断是否应该重试
5. ✅ **详细历史**：记录每步的执行详情和验证结果
6. ✅ **翻页可靠性**：通过URL变化验证翻页是否成功

## 使用场景

### 场景1：简单翻页
```
任务："访问https://books.toscrape.com，翻到第二页"

执行：
1. goto https://books.toscrape.com/index.html
2. click next page button
   - 验证：URL从index.html变为page-2.html ✓
```

### 场景2：翻页失败重试
```
任务："访问https://books.toscrape.com，翻到第二页"

执行：
1. goto https://books.toscrape.com/index.html
2. click next page button (第1次尝试)
   - 验证：URL未变化 ✗
   - 决策：should_retry=true → 重试
3. click next page button (第2次尝试)
   - 验证：URL从index.html变为page-2.html ✓
```

### 场景3：复杂任务
```
任务："在Google搜索'Python'，点击第一个结果"

执行：
1. goto https://www.google.com
   - 验证：URL变为google.com ✓
2. type 'Python' in search box
   - 验证：搜索框有文本 ✓
3. press Enter
   - 验证：URL变化，出现搜索结果 ✓
4. click first result
   - 验证：URL变化到目标网站 ✓
```

## 未来改进方向

1. **前端集成**
   - 添加"Run with Reflection"按钮
   - 实时显示执行进度和验证结果
   - 可视化执行历史

2. **更智能的验证**
   - 使用OCR检测文本变化
   - 使用图像相似度检测视觉变化
   - 检测网络请求完成

3. **更灵活的重试策略**
   - 不同类型的错误使用不同的重试策略
   - 指数退避（exponential backoff）
   - 自适应重试次数

4. **与Extract Data模式结合**
   - 在Extract Data中使用反思机制
   - 验证每次翻页是否成功
   - 避免重复提取数据

## 总结

反思机制通过 **执行后验证** 和 **自动重试**，大大提高了任务执行的可靠性。特别是对于翻页这种容易失败的操作，反思机制能够：

1. 检测翻页是否真的成功（URL变化）
2. 如果失败自动重试
3. 记录详细的执行历史
4. 提供清晰的失败原因

这使得系统更加健壮和可靠。
