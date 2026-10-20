"""
构建 ASGI 应用并装配接口与启动生命周期。
"""

import inspect
import json
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from app.adapters.observability.otel import build_observation_port
from app.adapters.web.correlation import CorrelationIdMiddleware
from app.adapters.web.docs import install_api_docs_routes
from app.adapters.web.health import install_health_routes
from app.adapters.web.metrics import HttpMetricsMiddleware
from app.adapters.web.plugin.routes import FastAPIDynamicRouteRegistry
from app.adapters.web.priority import ExecutionPriorityMiddleware
from app.adapters.web.security.access import (
    configure_token_codec,
    verify_apikey,
    verify_token,
)
from app.api.response import ResponseAPIRoute
from app.application.outbox import PostCommitEffectError
from app.application.plugin.routes import configure_plugin_routes
from app.application.plugin.runtime import get_plugin_manager
from app.application.security.token import create_access_token, decode_access_token
from app.runtime.correlation import get_correlation_id
from app.runtime.localization import LocaleHelper
from app.runtime.log import configure_correlation_id_provider, logger
from app.runtime.loop import main_loop_registry
from app.runtime.observability import configure_observation
from app.runtime.settings import get_runtime_setting
from app.runtime.version import get_app_version
from app.schemas.exception import (
    PersistenceUnavailableError,
)
from app.schemas.mcp import McpJsonRpcError, McpJsonRpcErrorDetail
from app.schemas.openai import (
    AnthropicErrorDetail,
    AnthropicErrorResponse,
    OpenAIErrorDetail,
    OpenAIErrorResponse,
)
from app.schemas.response import Response as ApiResponse
from app.schemas.response import ValidationIssue
from app.startup.lifecycle import lifespan


def _get_http_exception_message(detail: Any) -> str:
    """
    将 HTTPException 的 detail 转换为统一消息文本。
    """
    if isinstance(detail, str) and detail:
        return detail
    if detail is None:
        return "请求失败"
    try:
        return json.dumps(detail, ensure_ascii=False)
    except TypeError:
        return str(detail)


def _localize_exception_message(request: Request, message: str) -> str:
    """
    直接按异常所属请求的语言翻译消息，避免中间件上下文已被恢复。
    """
    return LocaleHelper.translate_text(
        message,
        locale=LocaleHelper.get_locale_from_request(request),
    )


def _is_mcp_jsonrpc_request(request: Request) -> bool:
    """
    判断请求是否指向保持原生响应的 MCP JSON-RPC 根端点。
    """
    request_path = getattr(getattr(request, "url", None), "path", "")
    return request_path.rstrip("/") == f"{get_runtime_setting('API_V1_STR')}/mcp"


def _get_native_ai_protocol(request: Request) -> str | None:
    """
    识别需要保持原生错误体的 OpenAI 或 Anthropic 兼容请求。
    """
    request_path = getattr(getattr(request, "url", None), "path", "")
    if request_path.startswith(f"{get_runtime_setting('API_V1_STR')}/openai/v1/"):
        return "openai"
    if request_path.startswith(f"{get_runtime_setting('API_V1_STR')}/anthropic/v1/"):
        return "anthropic"
    return None


def _native_ai_error_response(
        protocol: str,
        status_code: int,
        message: str,
        headers: dict[str, str] | None = None,
) -> JSONResponse:
    """
    按 OpenAI 或 Anthropic 兼容协议构造原生错误响应。
    """
    if protocol == "openai":
        error_type = (
            "authentication_error"
            if status_code in {401, 403}
            else "server_error"
            if status_code >= 500
            else "invalid_request_error"
        )
        return JSONResponse(
            status_code=status_code,
            content=OpenAIErrorResponse(
                error=OpenAIErrorDetail(
                    message=message,
                    type=error_type,
                    code=error_type,
                )
            ).model_dump(mode="json"),
            headers=headers,
        )

    error_type = (
        "authentication_error"
        if status_code in {401, 403}
        else "api_error"
        if status_code >= 500
        else "invalid_request_error"
    )
    return JSONResponse(
        status_code=status_code,
        content=AnthropicErrorResponse(
            error=AnthropicErrorDetail(type=error_type, message=message)
        ).model_dump(mode="json"),
        headers=headers,
    )


def _mcp_jsonrpc_error_response(
        status_code: int,
        code: int,
        message: str,
        headers: dict[str, str] | None = None,
) -> JSONResponse:
    """
    构造带 HTTP 状态码的 MCP JSON-RPC 原生错误响应。
    """
    return JSONResponse(
        status_code=status_code,
        content=McpJsonRpcError(
            jsonrpc="2.0",
            id=None,
            error=McpJsonRpcErrorDetail(code=code, message=message),
        ).model_dump(mode="json"),
        headers=headers,
    )


def _protocol_validation_error_response(
        request: Request,
        exc: RequestValidationError,
) -> JSONResponse | None:
    """
    为 OpenAI 与 Anthropic 兼容端点生成协议原生的参数错误响应。
    """
    errors = exc.errors()
    first_error = errors[0] if errors else {}
    location = ".".join(
        str(item)
        for item in first_error.get("loc", ())
        if item not in {"body"}
    )
    message = str(first_error.get("msg") or "Invalid request parameters.")
    native_ai_protocol = _get_native_ai_protocol(request)

    if native_ai_protocol == "openai":
        return JSONResponse(
            status_code=422,
            content=OpenAIErrorResponse(
                error=OpenAIErrorDetail(
                    message=message,
                    type="invalid_request_error",
                    param=location or None,
                    code="invalid_request_error",
                )
            ).model_dump(mode="json"),
        )
    if native_ai_protocol == "anthropic":
        if location:
            message = f"{location}: {message}"
        return JSONResponse(
            status_code=422,
            content=AnthropicErrorResponse(
                error=AnthropicErrorDetail(
                    type="invalid_request_error",
                    message=message,
                )
            ).model_dump(mode="json"),
        )
    if _is_mcp_jsonrpc_request(request):
        if location:
            message = f"{location}: {message}"
        return _mcp_jsonrpc_error_response(
            status_code=422,
            code=-32602,
            message=message,
        )
    return None


async def localized_http_exception_handler(
        request: Request,
        exc: HTTPException,
) -> JSONResponse:
    """
    将 HTTPException 响应统一封装为 Response 结构并隐藏内部实现细节。

    :param request: 当前 HTTP 请求
    :param exc: FastAPI HTTP 异常
    :return: 统一 JSON 错误响应
    """
    from app.runtime.errors import public_error_message

    message = _localize_exception_message(
        request,
        public_error_message(_get_http_exception_message(exc.detail)),
    )
    native_ai_protocol = _get_native_ai_protocol(request)
    if native_ai_protocol:
        return _native_ai_error_response(
            protocol=native_ai_protocol,
            status_code=exc.status_code,
            message=message,
            headers=exc.headers,
        )
    if _is_mcp_jsonrpc_request(request):
        error_codes = {
            400: -32600,
            401: -32001,
            403: -32001,
            404: -32601,
            409: -32009,
        }
        return _mcp_jsonrpc_error_response(
            status_code=exc.status_code,
            code=error_codes.get(exc.status_code, -32000),
            message=message,
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse[None](success=False, message=message).model_dump(mode="json"),
        headers=exc.headers,
    )


async def persistence_unavailable_handler(
        request: Request,
        _exc: PersistenceUnavailableError,
) -> JSONResponse:
    """
    将持久化能力暂不可用映射为可重试的 503 响应。
    """
    return await localized_http_exception_handler(
        request,
        HTTPException(
            status_code=503,
            detail="服务当前繁忙，请稍后重试",
            headers={"Retry-After": "1"},
        ),
    )


async def localized_validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
) -> JSONResponse:
    """
    将请求参数校验错误转换为统一响应并保留结构化错误数据。

    :param request: 当前 HTTP 请求
    :param exc: FastAPI 请求参数校验异常
    :return: 统一 JSON 错误响应
    """
    protocol_response = _protocol_validation_error_response(request, exc)
    if protocol_response is not None:
        return protocol_response

    errors = [
        ValidationIssue(
            location=list(error.get("loc", ())),
            message=str(error.get("msg") or "请求参数错误"),
            error_type=str(error.get("type") or "validation_error"),
        )
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=ApiResponse[list[ValidationIssue]](
            success=False,
            message=_localize_exception_message(request, "请求参数不正确"),
            data=errors,
        ).model_dump(mode="json"),
    )


async def localized_unhandled_exception_handler(
        request: Request,
        exc: Exception,
) -> JSONResponse:
    """
    将未捕获异常隐藏为统一的服务器错误响应，避免泄露内部细节。

    :param request: 当前 HTTP 请求
    :param exc: 未捕获异常
    :return: 统一 JSON 错误响应
    """
    logger.error(
        f"API 请求发生未捕获异常: {exc}",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    native_ai_protocol = _get_native_ai_protocol(request)
    if native_ai_protocol:
        return _native_ai_error_response(
            protocol=native_ai_protocol,
            status_code=500,
            message="Internal server error.",
        )
    if _is_mcp_jsonrpc_request(request):
        return _mcp_jsonrpc_error_response(
            status_code=500,
            code=-32603,
            message="Internal error",
        )
    message = (
        str(exc)
        if isinstance(exc, PostCommitEffectError)
        else "未知错误"
    )
    return JSONResponse(
        status_code=500,
        content=ApiResponse[None](
            success=False,
            message=_localize_exception_message(request, message),
        ).model_dump(mode="json"),
    )


def resolve_cors_policy(allowed_hosts: Any) -> tuple[list[str], bool]:
    """
    把 ALLOWED_HOSTS 解析为 CORS 的允许来源与凭据开关。

    通配来源不得与 allow_credentials 并用：Starlette 在两者同时成立时会把任意
    请求 Origin 原样回显并声明允许携带凭据，等于对所有站点开放带 Cookie 的跨域
    访问。只有显式列出来源时才允许携带凭据。
    """
    origins = [str(item).strip() for item in (allowed_hosts or ["*"]) if str(item).strip()]
    if not origins or "*" in origins:
        return ["*"], False
    return origins, True


_ENV_SERVE_FRONTEND: str = "MOVIEPILOT_SERVE_FRONTEND"
_RESERVED_FRONTEND_FALLBACK_ROOTS: tuple[str, ...] = (
    "/api",
    "/cookiecloud",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health",
)


def _is_frontend_serve_enabled() -> bool:
    """
    判断是否通过环境变量启用前端静态托管。

    :return: 环境变量设置为常见真值时返回 True，否则返回 False。
    """
    return os.getenv(_ENV_SERVE_FRONTEND, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_reserved_backend_path(path: str) -> bool:
    """
    判断请求路径是否属于必须保留给后端处理的服务根。

    :param path: 请求 URL 的绝对路径。
    :return: 若请求路径匹配保留后端路径前缀则返回 True，否则返回 False。
    """
    return any(
        path == root or path.startswith(f"{root}/")
        for root in _RESERVED_FRONTEND_FALLBACK_ROOTS
    )


def _find_matching_exception_handler(
    handlers: dict[Any, Callable[..., Any]],
    exception: Exception,
) -> Optional[Callable[..., Any]]:
    """
    按异常类型的继承层次匹配最具体的已注册异常处理器。

    :param handlers: 已注册的异常类型到处理器的映射字典。
    :param exception: 触发委派的异常实例。
    :return: 最具体的匹配处理器，未匹配到时返回 None。
    """
    for exception_type in type(exception).__mro__:
        handler = handlers.get(exception_type)
        if handler is not None:
            return handler
    return None


async def _invoke_exception_handler(
    handler: Callable[..., Any],
    request: Request,
    exc: Exception,
) -> Response:
    """
    执行同步或异步异常处理器并返回响应。

    :param handler: 异常处理回调。
    :param request: 触发异常的请求对象。
    :param exc: 异常实例。
    :return: 处理器生成的 HTTP 响应。
    """
    response = handler(request, exc)
    if inspect.isawaitable(response):
        response = await response
    return response


def _resolve_frontend_root(
    frontend_path: Optional[str],
) -> Optional[tuple[Path, Path]]:
    """
    校验并解析前端根目录与 SPA 首页路径。

    :param frontend_path: 配置的前端根目录路径字符串。
    :return: 目录与首页合法且存在时返回 (frontend, index_file)，否则返回 None。
    """
    if not frontend_path:
        return None
    frontend = Path(frontend_path).resolve()
    if not frontend.is_dir():
        return None
    index_file = frontend / "index.html"
    try:
        index_file.resolve().relative_to(frontend)
    except (ValueError, OSError):
        return None
    if not index_file.is_file():
        return None
    return frontend, index_file


def _mount_assets_if_present(app: FastAPI, frontend: Path) -> None:
    """
    当 assets 目录存在且位于前端根目录下时挂载静态路由。

    :param app: 接收静态资源路由的 FastAPI 实例。
    :param frontend: 前端根目录路径。
    """
    assets = frontend / "assets"
    if not assets.is_dir():
        return
    try:
        assets.resolve().relative_to(frontend)
    except (ValueError, OSError):
        return
    app.mount("/assets", StaticFiles(directory=assets), name="assets")


def _resolve_spa_target_file(
    frontend: Path,
    path: str,
) -> tuple[Optional[Path], bool]:
    """
    安全解析未命中路由对应的静态文件，拒绝路径穿越与目录外符号链接。

    :param frontend: 前端根目录路径。
    :param path: 请求 URL 路径。
    :return: (目标文件路径或 None, 是否存在越界访问风险)。
    """
    relative = path.lstrip("/")
    if not relative:
        return None, False
    target = (frontend / relative).resolve()
    try:
        target.relative_to(frontend)
    except (ValueError, OSError):
        return None, True
    if target.is_file():
        return target, False
    return None, False


def _install_spa_fallback_handler(
    app: FastAPI,
    frontend: Path,
    index_file: Path,
) -> None:
    """
    向应用注册 SPA 404 回退处理器，保留后端错误委派。

    :param app: 接收 404 回退处理器的 FastAPI 实例。
    :param frontend: 前端根目录路径。
    :param index_file: SPA 首页文件路径。
    """
    previous_status_handler = app.exception_handlers.get(404)
    exception_handlers = {
        key: handler
        for key, handler in app.exception_handlers.items()
        if isinstance(key, type) and issubclass(key, Exception)
    }

    async def _delegate_404(request: Request, exc: Exception) -> Response:
        """
        委派调用原始 404 处理器或通用异常处理器。

        :param request: 当前请求对象。
        :param exc: 触发 404 的异常对象。
        :return: 委派处理后的 HTTP 响应。
        """
        handler = previous_status_handler or _find_matching_exception_handler(
            exception_handlers, exc
        )
        if handler is None:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await _invoke_exception_handler(handler, request, exc)

    @app.exception_handler(404)
    async def _spa_fallback(request: Request, exc: Exception) -> Response:
        """
        处理未命中路由的静态文件或 SPA 首页回退。

        :param request: 当前请求对象。
        :param exc: 未命中路由产生的 404 异常。
        :return: 静态文件响应、SPA 首页响应或委派错误响应。
        """
        path = request.url.path
        if _is_reserved_backend_path(path) or request.method not in ("GET", "HEAD"):
            return await _delegate_404(request, exc)

        target, is_traversal = _resolve_spa_target_file(frontend, path)
        if is_traversal:
            return await _delegate_404(request, exc)
        if target is not None:
            return FileResponse(target)

        return FileResponse(index_file)


def mount_frontend(app: FastAPI) -> None:
    """
    在 Windows 安装版中由 FastAPI 托管前端静态资源与 SPA 路由。

    仅在环境变量启用且前端目录与首页存在时挂载；后端保留路径、
    非 GET/HEAD 请求以及目录外路径委派给原有异常处理器。

    :param app: 需要挂载静态前端与 404 回退处理器的 FastAPI 实例。
    """
    if not _is_frontend_serve_enabled():
        return

    resolved = _resolve_frontend_root(get_runtime_setting("FRONTEND_PATH"))
    if resolved is None:
        return

    frontend, index_file = resolved
    _mount_assets_if_present(app, frontend)
    _install_spa_fallback_handler(app, frontend, index_file)


def create_app() -> FastAPI:
    """
    创建并配置 FastAPI 应用实例。
    """
    configure_correlation_id_provider(get_correlation_id)
    configure_observation(build_observation_port())
    openapi_path = f"{get_runtime_setting('API_V1_STR')}/openapi.json"
    # 自带文档路由只能在创建时开关；改由 install_api_docs_routes 按 API_DOCS_ENABLE 逐请求判断
    _app = FastAPI(
        title=get_runtime_setting('PROJECT_NAME'),
        version=get_app_version(),
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan
    )

    _app.add_exception_handler(HTTPException, localized_http_exception_handler)
    _app.add_exception_handler(
        PersistenceUnavailableError,
        persistence_unavailable_handler,
    )
    _app.add_exception_handler(
        RequestValidationError,
        localized_validation_exception_handler,
    )
    _app.add_exception_handler(Exception, localized_unhandled_exception_handler)
    # 主程序静态路由统一使用 ResponseAPIRoute；动态插件注册时会显式覆盖为原生 APIRoute。
    _app.router.route_class = ResponseAPIRoute
    # 编排器探针使用原生 APIRoute 和最小响应，不进入业务响应包络或版本前缀。
    install_health_routes(_app)
    install_api_docs_routes(
        _app,
        openapi_path=openapi_path,
        enabled=lambda: bool(get_runtime_setting("API_DOCS_ENABLE")),
    )

    # 配置 CORS 中间件；通配来源时不允许携带凭据
    allow_origins, allow_credentials = resolve_cors_policy(get_runtime_setting('ALLOWED_HOSTS'))
    _app.add_middleware(
        CORSMiddleware,  # noqa
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _app.add_middleware(CorrelationIdMiddleware)
    _app.add_middleware(ExecutionPriorityMiddleware)
    _app.add_middleware(HttpMetricsMiddleware)

    @_app.middleware("http")
    async def locale_context_middleware(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """
        为每个请求设置后端多语言上下文。
        """
        token = LocaleHelper.set_current_locale(
            LocaleHelper.get_locale_from_request(request)
        )
        try:
            return await call_next(request)
        finally:
            LocaleHelper.reset_current_locale(token)

    # HTTP 适配器只持有令牌编解码端口，具体实现由组合根在创建应用时连接。
    configure_token_codec(create_access_token, decode_access_token)

    # 向 application 层插件路由服务注入应用实例，插件 API 的动态注册/移除
    # 统一经服务完成，避免 api.endpoints 反向依赖本模块。
    configure_plugin_routes(FastAPIDynamicRouteRegistry(
        app=_app,
        plugin_ids=lambda: get_plugin_manager().get_running_plugin_ids(),
        plugin_apis=lambda plugin_id: get_plugin_manager().get_plugin_apis(plugin_id),
        verify_token=verify_token,
        verify_apikey=verify_apikey,
        prefix=f"{get_runtime_setting('API_V1_STR')}/plugin",
        log=logger,
        event_loop=lambda: main_loop_registry.current,
    ))

    # mount_frontend 按 status-code(404) 注册 handler，与上游按异常类注册的
    # localized_http_exception_handler 并存；Starlette 对 404 优先命中 status handler。
    mount_frontend(_app)

    return _app


# 创建 FastAPI 应用实例；所有组合根装配副作用都在 create_app() 内部完成
app = create_app()
