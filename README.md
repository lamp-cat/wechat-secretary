# 消息工作台（wechat-secretary）

运行在你自己 Mac 上的**微信本地消息助手**：只读本机微信 4.x 的本地数据库，把收到的消息近实时同步到一个贴着微信窗口的**伴侣浮窗**里；当你在微信切到某个人/群时，它能**总结你和这个人的历史聊天**并给出**回复建议（仅生成草稿，绝不自动发送）**。

> **只收，我自己回复。** 全程只读微信，**没有任何发送通道**。

更新记录见 [CHANGELOG.md](CHANGELOG.md)。

## 设计原则（硬约束）
- **只读、只收**：绝不提供任何向微信发送消息的通道；AI 只产出草稿供参考。
- **数据不出本机**：消息都来自本机微信本地库，通过 `127.0.0.1` 读取。
- **不改微信、不关 SIP**：不修改官方微信 App、不改签名，SIP 保持开启。
- **机密只存本地**：解密密钥、网关令牌只写入权限 `0600` 的本地文件，界面/接口/日志一律打码，也不会写进任何 agent 配置。

## 组成
- `app/` — 原生 Mac 外壳（Swift + AppKit + WebKit），菜单栏图标 + 伴侣浮窗；从命令行用 `swiftc` 构建，产物在 `app/dist/`。`.app` 内嵌读取器、网关源码与首次启动脚本，下载后开箱即用。
- `wechat-frontend/` — 后端网关与页面（Python FastAPI + Uvicorn），本地回环提供接口、令牌鉴权；含可选消息归档、AI 草稿、**Agent 工具（MCP）自动接入**。
- `wechat-realtime-reader/` — 只读近实时读取器（Go），解密本机微信本地 SQLite 库；提供 HTTP API、页面，以及 `wechat-reader mcp`（stdio）/ `/mcp`（HTTP）。
- `setup/` — 首次启动全自动配置与应用内抓密钥脚本（由 App 调用）。
- `config/` — 配置模板（不含任何真实值/机密）。
- `install.sh` — 从源码安装（开发者用）。普通用户请直接下载 Release 里的 `.dmg`。

## 环境要求
- macOS（Apple Silicon），微信 4.x。
- 首次启动时若本机没有 Python / 命令行工具，App 会引导你一键安装。

## 下载即用（推荐）
1. 从 [Releases](https://github.com/lamp-cat/wechat-secretary/releases) 下载 `WeChatDesk-vX.Y.Z.dmg`，拖到「应用程序」。
2. 双击打开「消息工作台」。
3. **首次启动会自动完成**：
   - 查找本机微信 4.x 账号数据目录并写入配置；
   - 复用已有密钥，或在窗口里引导你抓取一次（临时签名副本 + Frida，不改正式版微信、不关 SIP）；
   - 生成网关令牌（0600）、创建 Python 环境、用 launchd 保活读取器（`127.0.0.1:5678`）与网关（`127.0.0.1:8088`）；
   - 把只读 MCP 读取器自动登记到本机检测到的 agent 工具（Codex / Claude Desktop / Claude Code / Cursor / VS Code）。配置里只写命令路径，**不含任何令牌**。

需要你亲自做的只有这几类（App 会弹清晰提示）：登录微信、退出微信（抓密钥前）、在系统弹窗里允许「完全磁盘访问权限」、确认安装命令行工具。

**外部 AI 接口**（Base URL / API Key / 模型）和**消息归档**都是可选项，默认关闭；需要时在设置（⌘,）里打开。

## 从源码安装（开发者）
```bash
git clone https://github.com/lamp-cat/wechat-secretary.git
cd wechat-secretary
./install.sh
open app/dist/消息工作台.app
```

## 使用
- 伴侣浮窗（⌘2）、置顶小窗（⌘3）、设置（⌘,）、重新加载（⌘R），以及重启后台 / 退出。
- 设置页可查看微信数据源状态、验证/重新抓取密钥、管理访问令牌、配置 AI、开关消息归档、查看/撤销 Agent 工具接入。
- 菜单「重新运行首次配置…」可随时再跑一遍自动配置（幂等，不会覆盖你的密钥）。

## Agent 工具接入（MCP，只读）
首次启动会自动把 `wechat-reader mcp` 登记到本机已安装的工具里。也可在设置 →「Agent 工具接入」里手动接入 / 撤销（撤销后不会再自动加回去；每次改动前都会备份原配置）。

手动接入示例（stdio，不含令牌）：

```json
{
  "mcpServers": {
    "wechat-reader": {
      "command": "/path/to/wechat-reader",
      "args": ["mcp"]
    }
  }
}
```

等价的只读 HTTP 入口：`http://127.0.0.1:5678/mcp`，鉴权 `Authorization: Bearer <读取器令牌>`（令牌在设置页按需显示，或运行 `wechat-reader token`；令牌 = 完整读权限，请勿外传）。

可用工具：`list_sessions`、`search_contacts`、`get_messages`、`search_messages`、`list_chat_members`、`get_new_messages`、`get_image`、`get_video`、`get_voice`、`sync_status`。没有任何发送类工具。

## 消息归档（可选，默认关闭）
在设置页打开「消息归档」后，会把消息持续复制到本机一个独立目录（默认 `~/MessageArchive/`），方便交给不能直接读取微信的 AI agent 使用。默认归档全部对话，也可只勾选指定对话。关闭开关立即停止同步。

限制：文件消息只记录文件名和大小；原图和视频本体需要先在微信里下载到本机，归档会定期自动补上。

## 读取器常用命令
```bash
wechat-realtime-reader/bin/wechat-reader detect [-write]  # 查找本机微信账号目录；-write 写入 source-config.json
wechat-realtime-reader/bin/wechat-reader doctor           # 自检密钥/库（不打印密钥）
wechat-realtime-reader/bin/wechat-reader sync             # 增量解密、同步快照
wechat-realtime-reader/bin/wechat-reader serve            # 只读 API + 页面 + HTTP MCP（127.0.0.1:5678）
wechat-realtime-reader/bin/wechat-reader mcp              # 只读 MCP（stdio）
wechat-realtime-reader/bin/wechat-reader token            # 查看访问令牌（请当机密）
```

## 安全
- 数据只在本机、只读、只收；机密只存 0600 本地文件并全程打码；不改微信、不关 SIP；遇到任何微信安全提示应立即停止。
- 网关/读取器令牌等同全量读权限，请当机密对待，勿分享，勿写进世界可读的配置文件。
- 服务只绑定回环地址；HTTP 客户端请设 `trust_env=False`，避免走到系统代理。

## 限制
- 单账号；只读、只收，**没有发送路径**。
- `wxgf` 原图需 `ffmpeg`（`brew install ffmpeg`），否则回退缩略图。
- 远程查看不在本项目范围（可自行用跳板/中转实现）。
