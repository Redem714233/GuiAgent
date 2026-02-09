# 反爬虫解决方案

## 问题描述

在访问豆瓣等网站时，可能会遇到以下错误：

```
https://sec.douban.com/c?r=...
error code: 01004 Please login: https://accounts.douban.com/passport/login
```

这是网站的**反爬虫机制**触发了，要求登录验证。

---

## 已实现的反爬虫措施

### 1. 真实浏览器特征模拟

在 `executor.py` 中已添加：

✅ **真实 User-Agent**
```python
"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
```

✅ **浏览器 Headers**
```python
"extra_http_headers": {
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}
```

✅ **地理位置和时区**
```python
"timezone_id": "Asia/Shanghai",
"locale": "zh-CN",
```

✅ **隐藏 WebDriver 特征**
```javascript
// 覆盖 navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined
});
```

### 2. Cookie 管理功能

新增三个方法：

#### `save_cookies(file_path)` - 保存 Cookie
```python
await executor.save_cookies("douban_cookies.json")
```

#### `load_cookies(file_path)` - 加载 Cookie
```python
await executor.load_cookies("douban_cookies.json")
```

#### `wait_for_manual_login(timeout_seconds)` - 等待手动登录
```python
success = await executor.wait_for_manual_login(timeout_seconds=300)
```

---

## 使用方法

### 方法 1：手动登录 + 保存 Cookie（推荐）

**第一次运行**：

```python
import asyncio
from backend.executor import Executor

async def main():
    executor = Executor()
    await executor.start()

    # 访问豆瓣
    await executor.goto("https://movie.douban.com/top250")
    await executor.wait_for_stable(3000)

    # 检查是否需要登录
    current_url = await executor.get_url()
    if "sec.douban.com" in current_url or "login" in current_url.lower():
        print("请在浏览器中手动登录...")

        # 等待用户登录
        success = await executor.wait_for_manual_login(timeout_seconds=300)

        if success:
            # 保存 Cookie
            await executor.save_cookies("douban_cookies.json")
            print("Cookie 已保存！")

    # 继续操作...
    await executor.stop()

asyncio.run(main())
```

**后续运行**：

```python
async def main():
    executor = Executor()
    await executor.start()

    # 加载已保存的 Cookie
    await executor.load_cookies("douban_cookies.json")

    # 直接访问，无需登录
    await executor.goto("https://movie.douban.com/top250")

    # 继续操作...
    await executor.stop()

asyncio.run(main())
```

### 方法 2：使用示例脚本

我们提供了完整的示例脚本：

```bash
python examples/douban_with_login.py
```

**工作流程**：
1. 检查是否有保存的 Cookie
2. 如果有，自动加载
3. 如果没有或 Cookie 失效，等待手动登录
4. 登录成功后自动保存 Cookie
5. 下次运行自动使用保存的 Cookie

---

## 完整示例

### 豆瓣电影 Top250 提取

```python
import asyncio
import os
from backend.executor import Executor
from backend.extraction_engine import ExtractionEngine
from backend.planner import Planner
from backend.output_store import OutputStore

async def main():
    # 初始化
    executor = Executor()
    planner = Planner()
    output_store = OutputStore()
    engine = ExtractionEngine(
        executor=executor,
        parser_service=None,
        planner=planner,
        output_store=output_store,
        data_dir="./data"
    )

    await executor.start()

    # Cookie 文件路径
    cookie_file = "douban_cookies.json"

    # 加载 Cookie（如果存在）
    if os.path.exists(cookie_file):
        await executor.load_cookies(cookie_file)
        print("✅ Cookie 已加载")

    # 访问豆瓣
    await executor.goto("https://movie.douban.com/top250")
    await executor.wait_for_stable(3000)

    # 检查是否需要登录
    current_url = await executor.get_url()
    if "sec.douban.com" in current_url or "login" in current_url.lower():
        print("⚠️  需要登录，请在浏览器中完成登录...")
        success = await executor.wait_for_manual_login(timeout_seconds=300)

        if success:
            await executor.save_cookies(cookie_file)
            print("✅ 登录成功，Cookie 已保存")
        else:
            print("❌ 登录失败")
            return

    # 开始提取数据
    print("\n开始提取豆瓣电影 Top250...")
    result = await engine.run_extraction(
        task="提取豆瓣电影Top250的前50部电影",
        max_items=50,
        strategy={
            "max_scrolls": 10,
            "list_only": True
        }
    )

    print(f"\n✅ 提取完成！")
    print(f"提取了 {result['items_extracted']} 部电影")
    print(f"保存到: {result['file_path']}")

    await executor.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 常见问题

### Q1: Cookie 保存在哪里？

默认保存在项目根目录的 `douban_cookies.json` 文件中。

### Q2: Cookie 会过期吗？

会的。豆瓣的 Cookie 通常有效期为几天到几周。过期后需要重新登录。

### Q3: 如何判断 Cookie 是否有效？

访问目标网站后检查 URL：
- 如果包含 `sec.douban.com` 或 `login` → Cookie 失效
- 如果正常显示内容 → Cookie 有效

### Q4: 可以用于其他网站吗？

可以！这套方案适用于所有需要登录的网站：
- 微博
- 知乎
- B站
- GitHub（私有仓库）
- 等等

### Q5: 为什么还是被检测为爬虫？

可能的原因：
1. **访问频率过快** - 添加延迟：`await executor.wait_for_stable(2000)`
2. **Cookie 失效** - 重新登录并保存
3. **IP 被封** - 更换网络或使用代理
4. **行为模式异常** - 模拟真实用户行为（随机滚动、停留等）

---

## 高级技巧

### 1. 添加随机延迟

```python
import random

# 随机等待 1-3 秒
await executor.wait_for_stable(random.randint(1000, 3000))
```

### 2. 模拟真实滚动

```python
# 随机滚动距离
scroll_distance = random.randint(300, 800)
await executor.scroll_by(scroll_distance)
await executor.wait_for_stable(random.randint(500, 1500))
```

### 3. 检测并处理验证码

```python
current_url = await executor.get_url()
if "captcha" in current_url.lower():
    print("⚠️  检测到验证码，请手动完成...")
    await executor.wait_for_stable(30000)  # 等待 30 秒
```

### 4. 使用代理（可选）

在 `executor.py` 的 `new_context()` 中添加：

```python
context_options = {
    # ... 其他配置
    "proxy": {
        "server": "http://proxy.example.com:8080",
        "username": "user",
        "password": "pass"
    }
}
```

---

## 文件清单

1. ✅ `backend/executor.py` - 添加了反爬虫措施和 Cookie 管理
2. ✅ `examples/douban_with_login.py` - 豆瓣登录示例脚本
3. ✅ `docs/ANTI_CRAWLER.md` - 本文档

---

## 总结

通过以下措施，可以有效应对大多数网站的反爬虫机制：

1. ✅ **真实浏览器特征** - User-Agent、Headers、时区等
2. ✅ **隐藏自动化特征** - 覆盖 `navigator.webdriver`
3. ✅ **Cookie 管理** - 保存和加载登录状态
4. ✅ **手动登录支持** - 等待用户完成登录
5. ✅ **智能延迟** - 模拟真实用户行为

**最佳实践**：
- 第一次运行时手动登录并保存 Cookie
- 后续运行自动加载 Cookie
- 添加随机延迟和滚动
- 控制访问频率

---

**最后更新**: 2026-02-09
**版本**: v3.1
**作者**: Claude Code
