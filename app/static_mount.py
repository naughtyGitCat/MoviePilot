"""
直接用 FastAPI StaticFiles 托管前端静态资源，允许 Windows 安装版在不启动 Nginx
的情况下通过后端端口同时访问前端。

通过环境变量 MOVIEPILOT_SERVE_FRONTEND=true 启用；默认 no-op，以保持与
Docker / Nginx 部署方式的完全兼容。

实现策略:
- /assets/ 目录通过 StaticFiles mount 提供
- 其他静态文件 (favicon, version.txt, manifest 等) 通过 404 exception handler 解析:
  * 优先走 FastAPI 路由, API 没命中时才进入 handler
  * handler 检查是否是 API 路径前缀 (api/, cookiecloud, docs, redoc, openapi.json, health)
    → 是则保留原 404; 否则尝试找 frontend 下的实际文件或返回 index.html
- 这样不会抢占 FastAPI 的 /docs、/redoc、/api/v*/openapi.json、/health/* 等内置路由
"""
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


_ENV_FLAG = "MOVIEPILOT_SERVE_FRONTEND"
# 下列前缀的 404 不做 SPA fallback, 直接返回 JSON 404
_RESERVED_PREFIXES = (
    "/api/",
    "/cookiecloud/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health",
)


def _enabled() -> bool:
    return os.getenv(_ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def mount_frontend(app: FastAPI) -> None:
    """
    mount /assets StaticFiles + 注册 404 exception handler 实现 SPA fallback。
    """
    if not _enabled():
        return

    from app.runtime.settings import RuntimeSettingsCompat

    settings = RuntimeSettingsCompat()
    frontend = Path(settings.FRONTEND_PATH).resolve()
    if not frontend.is_dir():
        return

    index_file = frontend / "index.html"
    if not index_file.is_file():
        return

    # /assets/ 长缓存静态资源
    assets = frontend / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.exception_handler(404)
    async def _spa_fallback(request: Request, exc):
        """
        只有在 FastAPI 的所有路由都没匹配时才会进到这里。
        """
        path = request.url.path

        # 不对 API 路径做 SPA fallback, 让客户端真实看到 404
        for prefix in _RESERVED_PREFIXES:
            if path.startswith(prefix):
                return JSONResponse({"detail": "Not Found"}, status_code=404)

        # 尝试作为前端目录下的文件提供 (例如 /favicon.ico, /version.txt, /manifest.webmanifest)
        candidate = frontend / path.lstrip("/")
        try:
            # 防止路径穿越到 frontend 之外
            candidate.resolve().relative_to(frontend)
        except (ValueError, OSError):
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        if candidate.is_file():
            return FileResponse(candidate)

        # 其他情况返回 SPA 主页, 让前端路由接管
        return FileResponse(index_file)
