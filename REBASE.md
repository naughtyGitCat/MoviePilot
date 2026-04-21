# Rebasing `v2-static` on upstream jxxghp/MoviePilot

此 fork 在 `v2-static` 分支上叠了 6 个 Windows-specific commit：

```
2df239b windows: RebotMP restart also accepts .bat fallback
ee7f4fa windows: optional FastAPI StaticFiles frontend mount
da5743f windows: pin sites.pyd updates to installer and skip auto-restart
ab4fdd4 windows: restart service via bundled RebotMP.exe
9c6f777 windows: inject CWD into sys.path for launcher flexibility
79649fd windows: relative FRONTEND_PATH, port 3111, disable auto-update resource
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

## 冲突处理原则

- **`FRONTEND_PATH`/`PORT` 默认值**：只要 jxxghp 没删字段，保留我们的值即可
- **`SystemUtils.restart`**：如果 jxxghp 新加了 restart 逻辑，把他们的逻辑放在 Windows 分支之前，Windows 分支作为 fallback
- **`resource.py` 的 .pyd 跳过**：如果循环结构变了，把 `if file_name.endswith(".pyd"): continue` 放在新的 `for` loop 开头

## 完全重做 (nuclear option)

如果某次 upstream 变动太大导致 rebase 不值得修，可以 squash 重来：

```bash
git checkout -B v2-static upstream/v2
# 对照本文件的 commit 列表，重新用 Edit/Write 写一遍
git push --force-with-lease
```

每个 commit 的意图和文件清单都在上面表格里，重做成本 ~30 分钟。
