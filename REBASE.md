# Rebasing `v2-static` on upstream jxxghp/MoviePilot

此 fork 在 `v2-static` 分支上叠了 12 个 Windows-specific commit（hash 每次 rebase 都变，按 subject 认，旧→新）：

```
windows: relative FRONTEND_PATH, port 3111, disable auto-update resource
windows: inject CWD into sys.path for launcher flexibility
windows: restart service via bundled RebotMP.exe
windows: pin sites.pyd updates to installer and skip auto-restart
windows: optional FastAPI StaticFiles frontend mount
windows: RebotMP restart also accepts .bat fallback
docs: add REBASE.md with sync-upstream procedure and conflict notes
fix(static_mount): drop GZipMiddleware, cannot add after app started
fix(static_mount): switch from catchall route to 404 exception handler
fix(static_mount): register mount_frontend in factory.create_app (pre-lifespan)
windows: align listen port with upstream Docker (3111 -> 3000)
fix(windows): FileURI.from_uri 不给 Windows 盘符路径补前导 /
```

每次 jxxghp 更新 `v2`，按下列步骤同步：

```bash
cd ~/github/MoviePilot
git fetch upstream v2
git checkout v2 && git reset --hard upstream/v2
git push origin v2      # 保持 fork 的 v2 与 upstream 一致
git checkout v2-static
git rebase upstream/v2
git push --force-with-lease origin v2-static
```

## 冲突概率（低到高）

| commit | 触碰文件 | 冲突概率 |
|---|---|---|
| `windows: optional FastAPI StaticFiles frontend mount` | `app/static_mount.py` (新) + `app/startup/routers_initializer.py` (+3 行) | 极低 |
| `windows: inject CWD into sys.path` | `app/main.py` (+2 行，在 import 区) | 低 |
| `windows: RebotMP.bat fallback` | `app/utils/system.py` (改 `SystemUtils.restart`) | 中 (如果 jxxghp 重构 SystemUtils) |
| `windows: pin sites.pyd updates` | `app/helper/resource.py` (+5 行) | 中 |
| `windows: relative FRONTEND_PATH, port 3111` | `app/core/config.py` (改默认值 3 处) | 中 |
| `windows: restart via RebotMP.exe` | `app/utils/system.py` + `app/chain/system.py` | 中 |
| `fix(static_mount): register mount_frontend in factory.create_app` | `app/factory.py` (+5 行，紧邻 CORS 中间件) | **高**（上游爱往 `create_app` 里加中间件）|
| `fix(windows): FileURI.from_uri` | `app/schemas/file.py` (+2 行) | 极低（上游几乎不动此文件）|

## 冲突处理原则

- **`FRONTEND_PATH`/`PORT` 默认值**：只要 jxxghp 没删字段，保留我们的值即可
- **`SystemUtils.restart`**：如果 jxxghp 新加了 restart 逻辑，把他们的逻辑放在 Windows 分支之前，Windows 分支作为 fallback
- **`resource.py` 的 .pyd 跳过**：如果循环结构变了，把 `if file_name.endswith(".pyd"): continue` 放在新的 `for` loop 开头
- **`factory.py` 的 `mount_frontend`**：几乎必冲突（上游持续往 `create_app` 里加东西）。处理方式是**两边都留**，把 `mount_frontend(_app)` 放在上游新增内容之后、`return _app` 之前。
  - ⚠️ 注意异常处理器的**键空间**：`mount_frontend` 按 status-code(`404`) 注册，上游的 `localized_http_exception_handler` 按异常类(`HTTPException`) 注册。Starlette 对 404 优先命中 status handler（我们的 SPA fallback 生效），其余 HTTPException 走上游的 i18n handler。二者**不会互相覆盖**，无需取舍。

## 历史 rebase 记录

| 日期 | 上游跨度 | 冲突 |
|---|---|---|
| 2026-06 | 582 commits | 0（改动碰的行上游正好没动）|
| 2026-07 | 166 commits（`1c60d8c` → `685f044`，v2.13.10 → v2.14.2）| 1 处：`app/factory.py`（上游新增 `locale_context_middleware`）|

**rebase 后必做的验证**（`py_compile` 只查语法，查不出被自动合并吞掉的改动 / 未定义名）：

```bash
# 1. 12 个改动逐项还在?
grep -n "mount_frontend" app/factory.py
grep -n 'A-Za-z\]:' app/schemas/file.py          # FileURI 盘符修复
grep -nE "FRONTEND_PATH|PORT.*3000|AUTO_UPDATE_RESOURCE" app/core/config.py
grep -n '\.pyd' app/helper/resource.py
grep -n "RebotMP" app/utils/system.py
grep -n "SystemUtils.restart" app/chain/system.py
grep -n "sys.path" app/main.py
grep -c "mount_frontend" app/startup/routers_initializer.py   # 应为 0

# 2. import 还在?（自动合并可能改了 import 区，py_compile 抓不到 NameError）
grep -n "from app.utils.system import SystemUtils" app/chain/system.py

# 3. 语法
git diff --name-only upstream/v2..v2-static | grep '\.py$' | xargs -n1 python3 -m py_compile
```

## 完全重做 (nuclear option)

如果某次 upstream 变动太大导致 rebase 不值得修，可以 squash 重来：

```bash
git checkout -B v2-static upstream/v2
# 对照本文件的 commit 列表，重新用 Edit/Write 写一遍
git push --force-with-lease
```

每个 commit 的意图和文件清单都在上面表格里，重做成本 ~30 分钟。
