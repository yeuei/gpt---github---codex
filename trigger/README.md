# Local Trigger Dashboard

本目录包含目标交接项目的本机 Trigger/runtime。Dashboard 只服务启动配置中确认的
单一交接仓库，不提供运行中仓库切换。

## 配对链接

打开 `http://127.0.0.1:8765/pair`，填写当前 Web 对话的稳定
`web_conversation_id`、repository、branch 和 PR，提交后由本机
`POST /api/bindings/invite` 生成真实的一次性 token。页面将其封装为短期 URL
fragment 并提供复制；token 不写入 GitHub、事件记录或日志。

GPT 无法访问本机 localhost 时不得声称已生成连接，应指导用户从此入口生成。Local
Agent 粘贴链接后读取字段调用 `/api/bindings/claim`，再用返回的一次性
`confirm_token` 调用 `/api/bindings/confirm`。过期、重复、目标不匹配或冲突均须停止
并报告。

启动参数、状态和安全边界以 `docs/协作协议.md`、`docs/技术规范.md` 为准；本机配置和
SQLite 状态保持在忽略文件中。
