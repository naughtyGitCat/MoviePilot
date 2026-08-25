# Rebasing `v3-static` on upstream jxxghp/MoviePilot `v3`

此 fork 在 `v3-static` 分支上叠了 Windows 安装版所需的 commit（hash 每次 rebase 都变，按 subject 认，旧→新）：

```
windows: listen on 3000, disable auto-update resource
windows: restart service via bundled RebotMP
windows: pin sites.pyd updates to installer
windows: optional FastAPI StaticFiles frontend mount
docs: add REBASE.md with v3-static sync procedure
windows: wait on fsproxy pipes without select()
fix(subscribe): map v2 tmdbid source to media_id
```

后两条同时在往 `jxxghp/MoviePilot` `v3` 提 PR。合入后下次 rebase 会变成上游历史，从本列表拿掉即可。

v2-static 里已经不需要再移植的改动：

- `FileURI.from_uri` 盘符修复：上游 v3 已带 `WINDOWS_DRIVE_PATTERN`
- `sys.path.append(os.getcwd())`：v3 `app/main.py` 已有 `_prepare_direct_execution_import_path()`
- transhandler 特典文件降噪：已合入上游

每次 jxxghp 更新 `v3`，按下列步骤同步：

```bash
cd ~/github/MoviePilot
git fetch upstream v3
git checkout v3 && git reset --hard upstream/v3
git push origin v3      # 保持 fork 的 v3 与 upstream 一致
git checkout v3-static
git rebase upstream/v3
git push --force-with-lease origin v3-static
```

然后触发 Windows 安装包构建：

```bash
gh workflow run build.yml --repo naughtyGitCat/Windows-MoviePilot --ref v3
```

## 冲突概率（低到高）

| commit | 触碰文件 | 冲突概率 |
|---|---|---|
| `windows: optional FastAPI StaticFiles frontend mount` | `app/static_mount.py` (新) + `app/factory.py` (`mount_frontend` 在 `return _app` 前) | **高**（上游爱往 `create_app` 里加中间件） |
| `windows: pin sites.pyd updates` | `app/adapters/system/resource.py` | 中 |
| `windows: restart via RebotMP` | `app/adapters/system/host.py` + `app/runtime/state.py` | 中（如果 jxxghp 再改重启路径） |
| `windows: listen on 3000` | `app/runtime/config.py` (改 `PORT` / `AUTO_UPDATE_RESOURCE` 默认值) | 中 |

## 冲突处理原则

- **`PORT=3000` / `AUTO_UPDATE_RESOURCE=False`**：只要 jxxghp 没删字段，保留我们的默认值。`.13` 的 `app.env` 仍可覆盖资源热更。
- **`SystemUtils.restart`**（`host.py`）：Windows 安装根是 `Path(__file__).resolve().parents[3].parent`（`app/adapters/system/host.py` → 源码根 → 安装根）。先找 `RebotMP.exe` 再找 `RebotMP.bat`。
- **`SystemHelper.can_restart` / `restart`**（`state.py`）：Windows 非冻结进程优先走 `SystemUtils.restart()`，再落到上游 Docker / CLI 路径。
- **`resource.py` 的 .pyd 跳过**：如果循环结构变了，把 `if file_name.endswith(".pyd"): continue` 放在新的 `for` loop 里、真正下载之前。
- **`factory.py` 的 `mount_frontend`**：几乎必冲突。处理方式是**两边都留**，把 `mount_frontend(_app)` 放在上游新增内容之后、`return _app` 之前。
  - ⚠️ 异常处理器键空间：`mount_frontend` 按 status-code(`404`) 注册，上游 `localized_http_exception_handler` 按异常类(`HTTPException`) 注册。Starlette 对 404 优先命中 status handler。
  - v3 额外保留 `/health` 前缀，避免 SPA fallback 吞掉 `/health/live`、`/health/ready`。

## Windows 安装包约定（不要改，升级靠同 AppId 覆盖）

- AppId `{{4f3f88b6-1f79-4d2b-9a4d-mp2static}}`
- 安装目录 `C:\Program Files (x86)\MoviePilot`
- 服务名仍为 `MoviePilot-V2`
- 内嵌解释器目录名仍为 `Python3.11`（实际是 3.14.7 embed）
- 站点资源落到 `app/application/site/`：`sites.cp314-win_amd64.pyd` + `user.sites.v3.bin`
- 监听端口 3000（不要跟上游 Docker 内部 3001）

## rebase 后必做的验证

```bash
# 1. Windows 改动逐项还在?
grep -n "mount_frontend" app/factory.py
grep -nE "PORT: int = 3000|AUTO_UPDATE_RESOURCE: bool = False" app/runtime/config.py
grep -n '\.pyd' app/adapters/system/resource.py
grep -n "RebotMP" app/adapters/system/host.py
grep -n "SystemUtils.restart" app/runtime/state.py
grep -n "is_windows" app/runtime/state.py
grep -n "/health" app/static_mount.py

# 2. import 还在?
grep -n "from app.adapters.system.host import SystemUtils" app/runtime/state.py   # lazy import inside restart()
grep -n "from app.static_mount import mount_frontend" app/factory.py

# 3. 语法
git diff --name-only upstream/v3..v3-static | grep '\.py$' | xargs -n1 python3 -m py_compile
```

## 完全重做 (nuclear option)

```bash
git fetch upstream v3
git checkout -B v3-static upstream/v3
# 重新移植上述 5 个 commit（参考 git log origin/v3-static --oneline）
git push --force-with-lease origin v3-static
```
