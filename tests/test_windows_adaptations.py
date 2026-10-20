"""
Windows 安装版配置、重启、资源更新与前端托管的回归测试。
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.adapters.system import resource as resource_module
from app.adapters.system.resource import ResourceHelper
from app.factory import mount_frontend
from app.runtime import state as state_module
from app.runtime.config import ConfigModel, settings
from app.runtime.state import SystemHelper


@pytest.fixture
def frontend_tree(tmp_path: Path) -> Path:
    """
    构造包含前端入口、资源和普通文件的临时站点。

    :param tmp_path: pytest 提供的临时目录
    :return: 可供静态挂载使用的前端根目录
    """
    frontend = tmp_path / "public"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>frontend</main>", encoding="utf-8")
    (assets / "app.js").write_text("window.ready = true", encoding="utf-8")
    (frontend / "version.txt").write_text("3.0.0", encoding="utf-8")
    return frontend


def _configure_frontend(monkeypatch: pytest.MonkeyPatch, frontend: Path) -> None:
    """
    将前端开关和资源目录限制在当前测试用例。

    :param monkeypatch: 用例结束后自动恢复环境与运行时配置的夹具
    :param frontend: 本用例的前端根目录
    """
    monkeypatch.setenv("MOVIEPILOT_SERVE_FRONTEND", "true")
    monkeypatch.setattr(settings, "FRONTEND_PATH", str(frontend))


def test_windows_config_defaults_overrides_and_field_metadata() -> None:
    """
    验证 Windows 部署配置的默认值、显式覆盖和字段说明。
    """
    defaults = ConfigModel()
    configured = ConfigModel(PORT=3210, AUTO_UPDATE_RESOURCE=True)

    assert defaults.PORT == 3000
    assert defaults.AUTO_UPDATE_RESOURCE is False
    assert configured.PORT == 3210
    assert configured.AUTO_UPDATE_RESOURCE is True
    assert ConfigModel.model_fields["PORT"].description
    assert ConfigModel.model_fields["AUTO_UPDATE_RESOURCE"].description


def test_windows_restart_prefers_exe_then_bat_then_panel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证 Windows 重启按安装器、批处理、主程序的顺序选择且不执行真实进程。

    :param tmp_path: pytest 提供的临时目录
    :param monkeypatch: 用例结束后自动还原模块路径和子进程边界
    """
    install_root = tmp_path / "Program Files (x86)" / "MoviePilot"
    runtime_file = install_root / "MoviePilot" / "app" / "runtime" / "state.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.touch()
    monkeypatch.setattr(state_module, "__file__", str(runtime_file))
    run = Mock()
    monkeypatch.setattr(state_module.subprocess, "run", run)

    exe = install_root / "RebotMP.exe"
    bat = install_root / "RebotMP.bat"
    panel = install_root / "MoviePilot-V3.exe"
    exe.touch()
    bat.touch()
    panel.touch()
    assert SystemHelper._windows_restart() == (True, "")
    run.assert_called_once_with(["start", "", str(exe)], shell=True)

    run.reset_mock()
    exe.unlink()
    assert SystemHelper._windows_restart() == (True, "")
    run.assert_called_once_with(["start", "", str(bat)], shell=True)

    run.reset_mock()
    bat.unlink()
    assert SystemHelper._windows_restart() == (True, "")
    run.assert_called_once_with(
        ["start", "", str(panel), "-c", "restart"], shell=True
    )


def test_windows_restart_reports_missing_and_failed_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证缺少重启程序或命令启动失败时返回可诊断结果。

    :param tmp_path: pytest 提供的临时目录
    :param monkeypatch: 用例结束后自动还原模块路径和子进程边界
    """
    install_root = tmp_path / "Program Files (x86)" / "MoviePilot"
    runtime_file = install_root / "MoviePilot" / "app" / "runtime" / "state.py"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.touch()
    monkeypatch.setattr(state_module, "__file__", str(runtime_file))

    success, message = SystemHelper._windows_restart()
    assert not success
    assert str(install_root / "RebotMP.exe") in message

    target = install_root / "RebotMP.exe"
    target.touch()
    run = Mock(side_effect=OSError("拒绝访问"))
    monkeypatch.setattr(state_module.subprocess, "run", run)
    success, message = SystemHelper._windows_restart()
    assert not success
    assert "拒绝访问" in message
    run.assert_called_once_with(["start", "", str(target)], shell=True)


def test_resource_update_skips_pyd_before_remote_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    验证 .pyd 不查询远端清单，其他资源扩展名仍正常保留。
    """
    payload = [
        {"name": "sites.cp314-win_amd64.pyd", "type": "auth"},
        {"name": "index.bin", "type": "indexer"},
        {"name": "native.so", "type": "indexer"},
    ]
    response = SimpleNamespace(
        status_code=200,
        json=lambda: [
            {"name": "index.bin", "download_url": "https://example.test/index", "size": 12},
            {"name": "native.so", "download_url": "https://example.test/native", "size": 34},
        ],
    )
    request = Mock()
    request.get_res.return_value = response
    monkeypatch.setattr(resource_module, "RequestUtils", lambda **_kwargs: request)

    result = ResourceHelper().get_download_files({"files": payload})

    assert [item["name"] for item in result] == ["index.bin", "native.so"]
    assert [item["size"] for item in result] == [12, 34]


@pytest.mark.parametrize("enabled", [False, True])
def test_frontend_mount_switch_and_static_routes(
    frontend_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """
    验证开关控制挂载，并保留静态文件、SPA 首页及深层前端路由。

    :param frontend_tree: 本用例使用的前端资源目录
    :param monkeypatch: 用例结束后自动恢复环境与运行时配置的夹具
    :param enabled: 本次参数化运行是否开启前端托管
    """
    _configure_frontend(monkeypatch, frontend_tree)
    if not enabled:
        monkeypatch.setenv("MOVIEPILOT_SERVE_FRONTEND", "false")
    app = FastAPI()
    mount_frontend(app)
    client = TestClient(app)

    if not enabled:
        assert client.get("/home").status_code == 404
        return

    assert client.get("/assets/app.js").text == "window.ready = true"
    assert client.get("/version.txt").text == "3.0.0"
    assert client.get("/").text == "<main>frontend</main>"
    assert client.get("/home").text == "<main>frontend</main>"
    assert client.get("/library/season/1").text == "<main>frontend</main>"
    assert client.head("/library/season/1").status_code == 200


def test_frontend_mount_is_noop_without_directory_or_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证前端目录不存在或缺少入口页时不安装 SPA 回退。

    :param tmp_path: pytest 提供的临时目录
    :param monkeypatch: 用例结束后自动恢复环境与运行时配置的夹具
    """
    monkeypatch.setenv("MOVIEPILOT_SERVE_FRONTEND", "true")
    missing = tmp_path / "missing"
    monkeypatch.setattr(
        "app.runtime.settings.get_runtime_setting", lambda _key: str(missing)
    )
    app = FastAPI()
    mount_frontend(app)
    assert TestClient(app).get("/home").status_code == 404

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(
        "app.runtime.settings.get_runtime_setting", lambda _key: str(empty)
    )
    app_without_index = FastAPI()
    mount_frontend(app_without_index)
    assert TestClient(app_without_index).get("/home").status_code == 404


def test_frontend_mount_rejects_traversal_and_external_symlinks(
    frontend_tree: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证文件系统路径不能通过目录穿越或外部符号链接泄露前端目录外内容。

    :param frontend_tree: 本用例使用的前端资源目录
    :param tmp_path: pytest 提供的临时目录
    :param monkeypatch: 用例结束后自动恢复环境与运行时配置的夹具
    """
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (frontend_tree / "external.txt").symlink_to(outside)
    _configure_frontend(monkeypatch, frontend_tree)
    app = FastAPI()
    mount_frontend(app)
    client = TestClient(app)

    external = client.get("/external.txt")
    traversal = client.get("/..%2Fsecret.txt")
    assert external.status_code == 404
    assert traversal.status_code == 404
    assert "secret" not in external.text
    assert "secret" not in traversal.text


def test_frontend_preserves_status_handler_priority_and_sync_support(
    frontend_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证已有 404 状态处理器优先于异常类处理器，且同步处理器仍可调用。

    :param frontend_tree: 本用例使用的前端资源目录
    :param monkeypatch: 用例结束后自动恢复环境与运行时配置的夹具
    """
    _configure_frontend(monkeypatch, frontend_tree)
    app = FastAPI()

    def status_handler(_request: Request, _exc: Exception) -> JSONResponse:
        """
        构造由状态码处理器优先返回的同步响应。

        :param _request: 当前 HTTP 请求
        :param _exc: 当前 404 异常
        :return: 标记状态处理器优先级的响应
        """
        return JSONResponse(
            {"handler": "status"},
            status_code=404,
            headers={"X-Handler": "status"},
        )

    async def exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
        """
        构造用于确认优先级的异常类响应。

        :param _request: 当前 HTTP 请求
        :param _exc: 当前 404 异常
        :return: 标记异常类处理器的响应
        """
        return JSONResponse({"handler": "exception"}, status_code=404)

    app.add_exception_handler(404, status_handler)
    app.add_exception_handler(HTTPException, exception_handler)

    @app.get("/api/status-priority")
    async def missing_api_route() -> None:
        """
        产生状态码处理器优先级测试所需的 API 404。

        :raises HTTPException: 每次调用均返回 404
        """
        raise HTTPException(status_code=404, detail="missing")

    mount_frontend(app)
    response = TestClient(app).get("/api/status-priority")

    assert response.status_code == 404
    assert response.json() == {"handler": "status"}
    assert response.headers["X-Handler"] == "status"


def test_frontend_preserves_upstream_api_error_and_cors_docs(
    frontend_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    验证后端 404 错误响应不被 SPA 覆盖，文档开关与 CORS 仍由应用提供。

    :param frontend_tree: 本用例使用的前端资源目录
    :param monkeypatch: 用例结束后自动恢复环境与运行时配置的夹具
    """
    _configure_frontend(monkeypatch, frontend_tree)
    app = FastAPI()

    async def upstream_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        """
        构造含状态、响应头和结构化错误体的上游 API 响应。

        :param _request: 当前 HTTP 请求
        :param exc: 上游路由抛出的 HTTP 异常
        :return: 保留上游状态码、错误体和响应头的响应
        """
        return JSONResponse(
            {"upstream": exc.detail},
            status_code=exc.status_code,
            headers={"X-Upstream": "preserved"},
        )

    app.add_exception_handler(HTTPException, upstream_handler)

    @app.get("/api/v1/missing")
    async def missing_api_route() -> None:
        """
        产生用于验证原异常处理器委派的 API 404。

        :return: 本路由只通过 HTTP 异常结束请求
        :raises HTTPException: 每次调用均返回上游 404
        """
        raise HTTPException(status_code=404, detail={"message": "upstream"})

    mount_frontend(app)
    response = TestClient(app).get("/api/v1/missing")
    assert response.status_code == 404
    assert response.json() == {"upstream": {"message": "upstream"}}
    assert response.headers["X-Upstream"] == "preserved"

    monkeypatch.setattr(settings, "API_DOCS_ENABLE", True)
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", ["https://client.example"])
    from app.factory import create_app

    integrated = TestClient(create_app())
    docs = integrated.get("/docs", headers={"Origin": "https://client.example"})
    assert docs.status_code == 200
    assert docs.headers["access-control-allow-origin"] == "https://client.example"
    assert integrated.get("/api/v1/openapi.json").status_code == 200
