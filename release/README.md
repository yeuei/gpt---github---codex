# 发布与安装入口

本仓库的 `trigger/` 是 Dashboard/runtime 唯一实现归属。Local Agent skill
`local-agent-github-project-executor-v2` 是安装、调度和健康观测入口；协议模板仅
提供通用文档与模板快照，不是运行时依赖，也不默认使用 git submodule。

```text
python /path/to/local-agent-github-project-executor-v2/scripts/dashboard_runtime.py \
  start --project-root /path/to/gpt---github---codex
python /path/to/local-agent-github-project-executor-v2/scripts/dashboard_runtime.py \
  status --project-root /path/to/gpt---github---codex
```

默认使用 `trigger/config.local.json`、`trigger/state.sqlite3` 和
`127.0.0.1:8765`；可用 `--config`、`--db`、`--port` 覆盖。调度器应依据
`status` 的 `reachable` 与 `/api/status` 内容判断健康。发布时记录协议模板的
tag/commit 快照；项目专属 URL、profile、命令和 token 只能留在本机忽略配置。

最低兼容面：Python 3、`/api/status`、`/api/bindings` 与 binding.v1 状态机。
