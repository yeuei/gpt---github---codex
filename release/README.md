# 发布与安装入口

本仓库 `trigger/` 是 Dashboard/runtime 唯一实现归属；Local Agent skill 负责安装、调度和健康观测，基础协议仓库不作为 runtime submodule。

```text
python /path/to/local-agent-github-project-executor-v2/scripts/dashboard_runtime.py \
  start --project-root /path/to/gpt---github---codex
python /path/to/local-agent-github-project-executor-v2/scripts/dashboard_runtime.py \
  status --project-root /path/to/gpt---github---codex
```

最低兼容面：

- Python 3；
- `/api/status`；
- `/api/bindings` + binding.v1；
- `/api/task/snapshot?pr=N&sha=<commit>` exact-SHA 历史任务；
- `/api/refresh-github` 显式网络刷新；
- 后台默认 local Git scan，无隐式 GitHub API/fetch 依赖。

本机 `config.local.json`、`state.sqlite3`、浏览器 profile、conversation URL、Agent command 与 pairing token 不进入 Git。
