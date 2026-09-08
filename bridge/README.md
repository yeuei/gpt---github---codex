# Gitee ChatGPT Bridge

这是一个无第三方依赖的本地 Bridge：ChatGPT 网页通过 OAuth 2.1 + PKCE 连接
`/mcp`，Bridge 将请求转发到 Gitee 官方远程 MCP `https://api.gitee.com/mcp`。
Gitee PAT 只从 `GITEE_ACCESS_TOKEN` 环境变量读取，不写入仓库或网页。

## 启动

```bash
cd bridge
export GITEE_ACCESS_TOKEN='你的 Gitee PAT'
export BRIDGE_ALLOWED_REPOSITORIES='yeuei/gpt---github---codex'
./start_bridge.sh
```

可选变量：

- `BRIDGE_PORT`：默认 `48765`；
- `BRIDGE_PUBLIC_URL`：Cloudflare Tunnel 的 HTTPS URL，不带结尾 `/`；
- `BRIDGE_PAIRING_CODE`：自定义 8 位配对码；不设置则启动时随机生成；
- `BRIDGE_ALLOWED_TOOLS`：逗号分隔白名单；不设置时只放行 `get/list/search/...` 等只读命名；
- `GITEE_MCP_URL`：默认 `https://api.gitee.com/mcp`。

本地检查：

```bash
curl http://127.0.0.1:48765/health
curl http://127.0.0.1:48765/.well-known/oauth-authorization-server
```

## 连接 ChatGPT 网页

ChatGPT 的 MCP 连接器需要一个公网 HTTPS 地址。先安装 Cloudflare 的
`cloudflared`（它不需要把 PAT 写入 Cloudflare），然后可以直接运行：

```bash
./start_quick_tunnel.sh
```

脚本会先启动 Quick Tunnel，自动取得随机的 `trycloudflare.com` 地址，再启动
Bridge，因此 OAuth 元数据里会使用正确的公网地址。它会打印 ChatGPT 连接器应填的：

```text
https://<随机名>.trycloudflare.com/mcp
```

连接器首次授权会打开 Bridge 的授权页面；把 Bridge 启动时显示的 8 位配对码填入。
配对码 5 分钟有效且只能使用一次。

快速隧道只适合开发验证，稳定使用时应改为 Cloudflare named tunnel，并固定
`BRIDGE_PUBLIC_URL`。不要把 Gitee PAT 填入 ChatGPT 的连接器表单。

如果不使用脚本，也可以手动启动：先取得 Tunnel URL，再以
`BRIDGE_PUBLIC_URL=https://...` 启动 Bridge，最后在 ChatGPT 中填写
`https://.../mcp`。Bridge 必须在 OAuth 回调之前已经使用同一个公网 URL 启动。

## 安全边界

- MCP `/mcp` 需要 Bridge 自己签发的 OAuth access token；Gitee PAT 不会返回给客户端。
- 动态客户端注册只保存内存状态；Bridge 重启后需重新连接。
- 默认仅转发只读工具；`BRIDGE_ALLOWED_TOOLS` 和 `BRIDGE_ALLOWED_REPOSITORIES`
  可进一步收窄权限。
- 日志不打印 URL 查询参数、Authorization 或请求体。

运行测试：

```bash
python3 -m unittest discover -s bridge -p 'test_*.py' -v
```
