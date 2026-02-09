# 反思机制 - 快速开始

## 🚀 立即测试

### 1. 重启后端

```bash
# 停止当前后端（Ctrl+C）
# 然后重新启动
python start_backend.py
```

### 2. 运行测试

```bash
python test/test_reflection.py
```

### 3. 预期结果

```
[OK] Browser started

============================================================
Testing Reflection Mechanism with Pagination
============================================================

Task: 访问https://books.toscrape.com/index.html，翻到第二页

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
    Verification: URL changed to page-2.html, indicating successful pagination
    Status: success

[SUCCESS] Successfully navigated to page 2!
```

## 📝 核心改进

### 问题：Run 和 Extract Data 都去不了第二页

**原因**：
1. 没有验证翻页是否成功
2. 没有重试机制
3. 不知道动作是否真的执行成功

### 解决：反思机制

**流程**：规划 → 执行 → **验证** → 决策（重试/继续/完成）

**关键**：
- ✅ 执行后验证（对比URL、截图、元素）
- ✅ 自动重试（失败最多重试3次）
- ✅ 详细历史（记录每步的验证结果）

## 🔧 使用方法

### 方法1：测试脚本

```bash
python test/test_reflection.py
```

### 方法2：API调用

```bash
curl -X POST http://127.0.0.1:8000/run_with_reflection \
  -H "Content-Type: application/json" \
  -d '{
    "task": "访问https://books.toscrape.com/index.html，翻到第二页",
    "max_steps": 10,
    "max_retries_per_step": 3
  }'
```

### 方法3：Python代码

```python
import requests

response = requests.post("http://127.0.0.1:8000/run_with_reflection", json={
    "task": "访问https://books.toscrape.com/index.html，翻到第二页",
    "max_steps": 10,
    "max_retries_per_step": 3
})

result = response.json()
print(f"Status: {result['status']}")
print(f"Final URL: {result['final_url']}")

for step in result['steps']:
    print(f"\nStep {step['step_index'] + 1}: {step['description']}")
    print(f"  Action: {step['action']}")
    print(f"  URL: {step['before_url']} → {step['after_url']}")
    print(f"  Verification: {step['verification']['reasoning']}")
    print(f"  Status: {step['status']}")
```

## 📊 验证标准

VLM会根据以下标准验证步骤是否成功：

### 1. URL变化
- **翻页**：URL应该从 `page-1` 变为 `page-2`
- **导航**：URL应该变为目标网站
- **输入**：URL可能不变

### 2. 页面内容变化
- 元素数量变化
- 新内容出现
- 预期元素可见

### 3. 视觉变化
- 对比前后截图
- 查找视觉成功指标

## 🎯 示例任务

### 任务1：简单翻页
```json
{
  "task": "访问https://books.toscrape.com/index.html，翻到第二页",
  "max_steps": 5,
  "max_retries_per_step": 3
}
```

### 任务2：搜索并点击
```json
{
  "task": "访问Google，搜索'Python'，点击第一个结果",
  "max_steps": 10,
  "max_retries_per_step": 3
}
```

### 任务3：多次翻页
```json
{
  "task": "访问https://books.toscrape.com，翻到第三页",
  "max_steps": 10,
  "max_retries_per_step": 3
}
```

## 📁 新增文件

1. **backend/reflection_engine.py** - 反思引擎
2. **backend/vlm_service.py** - 添加了验证方法（第599-707行）
3. **backend/server.py** - 添加了 `/run_with_reflection` 端点
4. **test/test_reflection.py** - 测试脚本
5. **docs/REFLECTION_MECHANISM.md** - 详细文档
6. **docs/REFLECTION_SUMMARY.md** - 总结文档

## 🐛 故障排除

### 问题1：测试失败

**检查**：
1. 后端是否重启？
2. 浏览器是否正常启动？
3. VLM API是否可用？

### 问题2：翻页仍然失败

**检查验证结果**：
```python
# 查看验证详情
for step in result['steps']:
    print(step['verification'])
```

**可能原因**：
- VLM判断错误（reasoning会说明原因）
- 翻页按钮未检测到（检查DOM标注）
- 页面加载太慢（增加等待时间）

### 问题3：重试次数不够

**增加重试次数**：
```json
{
  "task": "...",
  "max_retries_per_step": 5  // 增加到5次
}
```

## 📖 详细文档

- **REFLECTION_MECHANISM.md** - 完整的反思机制文档
- **REFLECTION_SUMMARY.md** - 总结文档
- **PAGINATION_IMPLEMENTATION.md** - 翻页功能实现文档

## 💡 下一步

### 1. 测试反思机制
```bash
python test/test_reflection.py
```

### 2. 如果成功，考虑集成到 Extract Data
将反思机制集成到数据提取流程中，使翻页更可靠。

### 3. 前端集成
在前端添加 "Run with Reflection" 按钮。

## 🎉 总结

反思机制通过 **执行后验证** 和 **自动重试**，解决了翻页不可靠的问题。

核心流程：
```
规划 → 执行 → 验证 → 决策（重试/继续/完成）
```

现在请运行测试，看看是否能成功翻到第二页！

```bash
python test/test_reflection.py
```
