# DOM Marker v3.0 快速上手指南

## 环境准备

```bash
# 1. 激活 conda 环境
conda activate agent_env

# 2. 确保已安装依赖
pip install -r requirements.txt

# 3. 配置环境变量（.env 文件）
PLAYWRIGHT_CHANNEL=msedge
PLAYWRIGHT_VIEWPORT_WIDTH=1280
PLAYWRIGHT_VIEWPORT_HEIGHT=720
```

## 快速测试

```bash
# 运行快速对比测试
python test/quick_test_v3.py
```

## 基本使用

```python
from backend.executor import Executor

async def main():
    executor = Executor()

    # 访问网页
    await executor.goto("https://github.com/trending")

    # 标记可交互元素
    result = await executor.mark_page_elements()

    # 获取元素列表
    elements = result['elements']
    print(f"找到 {len(elements)} 个可交互元素")

    # 查看元素信息
    for elem in elements[:5]:
        print(f"ID: {elem['id']}")
        print(f"标签: {elem['tagName']}")
        print(f"文本: {elem['text'][:50]}")
        print(f"位置: {elem['rect']}")
        print()

    # 清理
    await executor.stop()

# 运行
import asyncio
asyncio.run(main())
```

## 参数调优

### 元素数量限制

在 `backend/dom_marker.js` 中修改：

```javascript
// 限制最多 30 个元素（默认）
const finalElements = filteredElements.slice(0, 30);
```

### 面积阈值

```javascript
// 过滤太小的元素（默认 400px²）
if (area < 400) {
    continue;
}

// 过滤太大的元素（默认视口的 80%）
if (area > viewportArea * 0.8) {
    continue;
}
```

### 重叠阈值

```javascript
// 重叠阈值（默认 0.5，即 50%）
const filteredElements = filterOverlappingElementsWithQuadTree(candidateElements, 0.5);
```

## 常见问题

### Q: 浏览器启动失败？

A: 检查 `.env` 文件中的 `PLAYWRIGHT_CHANNEL` 配置：
- `msedge` - Microsoft Edge
- `chrome` - Google Chrome
- `firefox` - Firefox

### Q: 元素数量太少？

A: 降低面积阈值或增加元素数量限制。

### Q: 元素数量太多？

A: 提高面积阈值或减少元素数量限制。

### Q: 标注重叠？

A: 降低重叠阈值（如 0.3）以更严格地过滤重叠元素。

## 性能优化建议

1. **大型页面**：增加四叉树的 `maxDepth`（默认 4）
2. **复杂布局**：降低 `maxElements`（默认 10）以更细粒度分割
3. **快速标注**：减少元素数量限制（如 20 个）

## 下一步

- 查看 [V3_RELEASE_NOTES.md](V3_RELEASE_NOTES.md) 了解详细改进
- 运行 `test/test_dom_marker_v3.py` 进行完整测试
- 查看 `backend/dom_marker.js` 了解实现细节
