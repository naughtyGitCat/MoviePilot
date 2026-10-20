# 同步 Windows 分支与上游 v3

`v3-static` 在上游 `v3` 之上保留 Windows NSSM 安装版适配；不替换上游的 Docker/CLI 实现。

## 当前同步基线

- 目标：`14b9ea6d3e43df14ab595d31b20821167428ccbd`（v3.1.8 发布后的 v3 提交）。
- 旧基线：`e40b9a044bbd67b3399ff2935d66418d906e1251`（v3.0.7）。
- 原 Windows 分支：`3965f8aab13eb9adab64065b1de9c2d29fd8f236`。
- 本地备份：`v3-static-backup-20261020-1900`。
- 本次 fetch 时上游已推进到 `43424e592`，比固定目标多 2 个提交；未混入本轮迁移。

## 必须保留的适配

| 入口 | 行为 |
|---|---|
| `app/runtime/config.py` | `PORT` 默认 3000、`AUTO_UPDATE_RESOURCE` 默认 False；保留上游 `Field` 元数据和显式配置覆盖。 |
| `app/runtime/state.py::_windows_restart` | 从 `Path(__file__).resolve().parents[3]` 定位安装根，依次查找 `RebotMP.exe`、`RebotMP.bat`、`MoviePilot-V3.exe`；仅最后者使用 `-c restart`。 |
| `app/adapters/system/resource.py::get_download_files` | 在要求下载 URL 之前排除 `.pyd`，由安装包提供 Windows 扩展；其余资源更新逻辑保持上游行为。 |
| `app/factory.py::mount_frontend` | 仅在 `MOVIEPILOT_SERVE_FRONTEND=true` 时托管前端，路径使用 `get_runtime_setting("FRONTEND_PATH")`；目录或 index 缺失时不挂载；在 factory 内部实现以避免新增模块改变宿主架构依赖基线。 |
| `app/factory.py::create_app` | 保留上游中间件、CORS、动态文档和生命周期，在返回应用前调用前端挂载。 |

静态前端的 404 handler 不得吞掉 API、CookieCloud、health、docs 等路径的错误。
非 SPA 响应委托原适用异常处理器，保留协议格式、状态码及 headers；不能反向导入 factory。
前端文件路径必须限制在配置目录内，目录外符号链接不能泄露文件。

以下历史修复已在上游，不重复移植：

- Windows fsproxy 匿名管道等待：上游 #6459 / `c0222ee7d`。
- 订阅旧 tmdbid 来源兼容：上游 #6460 / `a79e00f1e`。
- `FileURI.from_uri` Windows 盘符处理。
- 直接运行的模块导入路径、电视剧特典无集数降噪。

## 本地同步流程

先核对干净工作区及当前分支，不自动丢弃已有改动。下面的 SHA 和分支名称必须换成本轮记录值；备份名称必须唯一，不使用 `branch -f` 覆盖旧备份。

```bash
export DEVELOPER_DIR=/Library/Developer/CommandLineTools
cd ~/github/MoviePilot

git status --short --branch
git fetch --no-tags upstream +refs/heads/v3:refs/remotes/upstream/v3
# 固定并记录目标 SHA，避免后续验证期间跟随移动的分支。
git rev-parse upstream/v3

git branch v3-static-backup-<时间戳> v3-static
git switch -c sync/v3-static-<时间戳> v3-static
git rebase --onto <目标SHA> <旧上游基线SHA>
```

冲突处理只迁移必要差异，不整文件采用 ours/theirs：

- 配置已经使用 `Field(default=..., description=...)`，只改默认值，不能退回旧版裸赋值。
- 重启函数保留上游 Docker/CLI 分支，只调整 Windows 安装布局。
- 资源筛选保留上游 manifest 及下载地址验证。
- factory 保留上游所有新路由/中间件；静态挂载放在应用装配完成后。

遇到无法解决的冲突可 `git rebase --abort`；原分支和备份仍可用。验证不通过时保留同步分支，不移动 `v3-static`。

## 验证

遵循 `AGENTS.md`、`docs/testing.md`、目标版本 CI 和依赖规则，使用 Python 3.14+、uv 0.12.5+。
环境与上游锁文件一致，不为本机安装修改 `uv.lock`；测试必须隔离配置、数据库、网络和真实重启进程。

```bash
uv sync --locked
uv lock --check
uv sync --locked --offline --inexact --no-dev --check

# 先检查依赖边界，再运行行为测试。
uv run --locked --no-sync pytest \
  tests/test_architecture_dependencies.py \
  tests/test_architecture_adapter_imports.py -q
uv run --locked --no-sync pytest \
  tests/test_windows_adaptations.py \
  tests/test_resource_v3.py \
  tests/test_system_utils.py \
  tests/test_system_update_manager.py \
  tests/test_api_response.py \
  tests/test_health_probes.py \
  tests/test_api_docs_routes.py \
  tests/test_factory_cors.py -q

uv run --locked --no-sync python tests/run.py
uv run --locked --no-sync python scripts/startup/performance.py --check --repeat 3

git range-diff <旧基线>..<旧Windows分支SHA> <目标SHA>..HEAD
git diff --check
git merge-base --is-ancestor <目标SHA> HEAD
```

另按项目规则运行改动文件 pylint、适用架构快照和 ratchet；依赖变化还需要锁定依赖漏洞审计。
失败必须调查，不通过刷新 baseline、删测试或修改锁文件掩盖。

Windows 专项检查默认值/配置覆盖、RebotMP 顺序及安装路径、`.pyd` 筛选、前端开关与 SPA、
路径穿越、错误格式及 headers、动态文档开关和 CORS。语法检查或 macOS mock 测试不能代替
真实 Windows 服务重启与扩展 ABI 验证；没有执行的检查必须明确列为未验证。

全部适用检查通过后，确认 `v3-static` 仍停在原始 SHA，再更新本地引用并切回。
保留备份分支，交付报告记录准确提交及各检查结果。

## 推送、构建与部署是独立阶段

本地同步不自动授权推送、强推、构建或升级服务。获得对应授权后，推送前重新核对远端分支和变化；
如需重写历史，使用绑定已核对远端 SHA 的 lease，不覆盖别人的更新。
安装包构建仍由 `naughtyGitCat/Windows-MoviePilot` 的 `v3` 分支 `build.yml` 完成，
不因源码同步直接触发。

## Windows 安装布局约定

- AppId：`{{4f3f88b6-1f79-4d2b-9a4d-mp2static}}`。
- 安装目录：`C:\Program Files (x86)\MoviePilot`，源码在其下的 `MoviePilot` 目录。
- NSSM 服务名：`MoviePilot-V2`。
- 内嵌解释器目录仍名为 `Python3.11`，既有安装包实际使用 Python 3.14.7；名称用于兼容覆盖升级。
- 站点扩展和数据位于 `app/application/site/`，匹配解释器 ABI。
- 前后端同端口 3000；不改用 developer-wlj 的面板安装器。
