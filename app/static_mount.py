"""
直接用 FastAPI StaticFiles 托管前端静态资源，允许 Windows 安装版在不启动 Nginx
的情况下通过后端端口同时访问前端。

通过环境变量 MOVIEPILOT_SERVE_FRONTEND=true 启用；默认 no-op，以保持与
Docker / Nginx 部署方式的完全兼容。
"""
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


_ENV_FLAG = "MOVIEPILOT_SERVE_FRONTEND"
_API_PREFIXES = ("api/", "cookiecloud")


def _enabled() -> bool:
    return os.getenv(_ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def mount_frontend(app: FastAPI) -> None:
    """
    在 app 上挂载 /assets 静态目录和 SPA fallback。
    必须在所有 API 路由注册之后调用，避免 catch-all 吞掉 API 请求。
    """
    if not _enabled():
        return

    from app.core.config import settings

    frontend = Path(settings.FRONTEND_PATH).resolve()
    if not frontend.is_dir():
        return

    app.add_middleware(GZipMiddleware, minimum_size=1000)

    assets = frontend / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str):
        # 已注册的 API 前缀留给 FastAPI 路由匹配；到这里说明真的没命中
        if full_path.startswith(_API_PREFIXES):
            raise HTTPException(status_code=404)
        candidate = frontend / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend / "index.html")
