# Local-first Trigger Dashboard

`trigger/` 是本实例 Dashboard/runtime 的唯一实现归属。

## 默认行为

启动后后台只扫描**本地 Git clone**：

```bash
python3 trigger.py
```

它不会在轮询中自动 `git fetch`。Dashboard 的 **“从 GitHub 刷新”** 是显式网络入口；等价命令：

```bash
python3 trigger.py --config config.local.json --db state.sqlite3 --refresh-once
```

只做本地扫描：

```bash
python3 trigger.py --config config.local.json --db state.sqlite3 --once
```

## Dashboard

```text
http://127.0.0.1:8765/
```

配对：

```text
http://127.0.0.1:8765/pair
```

当前 renderer 只有 `dashboard.html` 一份。PR 事件节点显示三个独立状态：Trigger 审批、投递/执行、GitHub PR。

## 历史任务

点击事件节点后只查询：

```text
GET /api/task/snapshot?pr=<N>&sha=<event commit SHA>
```

后端严格执行 `git show SHA:coordination/PR-N/任务.md`。历史文件不存在就显示不可用，不做任何 fallback。

## 网络刷新失败

`POST /api/refresh-github` 的 fetch 失败时：

- 不调用后续本地 scan；
- 不改变已有 event approval/delivery 状态；
- 返回 `local_state_preserved=true`；
- Dashboard 显示实际 Git 错误。

如果本机安装 `gh`，显式刷新还会 best-effort 更新 GitHub PR 状态缓存；`gh` 不可用时 PR 状态保持未知/旧缓存，绝不从审批或投递状态推断。

## 审批边界

- Trigger 自动审批：只决定是否路由 GitHub 协作事件。
- Codex app-server：仍使用 `workspace-write + on-request`；命令/文件请求在 Dashboard 单独批准。

两套授权互不继承。

## Binding

`/pair` 调用 `/api/bindings/invite` 生成真实一次性 token，并把 token 放在复制链接的 URL fragment 中。token 不写 GitHub、事件或服务日志。Local Agent 认领后继续 claim → confirm。

## 可复现验证

```bash
python3 -m py_compile trigger.py test_trigger.py codex-agent-app-server.py
python3 -m unittest test_trigger.py -v
python3 - <<'PY'
from pathlib import Path
import re
s=Path('dashboard.html').read_text()
Path('/tmp/dashboard.js').write_text('\n'.join(re.findall(r'<script>(.*?)</script>',s,re.S)))
PY
node --check /tmp/dashboard.js
cd .. && git diff --check
```

单元测试覆盖：

- background local scan 不 fetch；
- 显式 GitHub 刷新失败保留本地 event state；
- exact-SHA 历史任务读取与缺失语义；
- 已从当前 HEAD 删除任务文件后历史 snapshot 仍可读；
- approval / delivery / GitHub PR 三状态独立；
- v1 legacy status 不被猜测；
- single renderer / no event-time fallback；
- binding token single-use + claim/confirm；
- pair link 使用 URL fragment。

CI 只证明代码/静态契约；真实 OBU、Chrome、Codex app-server 的本地运行证据仍由 Local Agent 提供。
