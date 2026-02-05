"""
启动脚本 - 从项目根目录启动后端服务

使用方法：
    python start_backend.py
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "backend.server:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info"
    )
