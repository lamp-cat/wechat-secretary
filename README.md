# 消息助手(wechat-secretary)

运行在你自己 Mac 上的**微信本地消息助手**:只读本机微信 4.x 的本地数据库,把收到的消息近实时同步到一个贴着微信窗口的**伴侣浮窗**里;当你在微信切到某个人/群时,它能**总结你和这个人的历史聊天**并给出**回复建议(仅生成草稿,绝不自动发送)**。

> **只收,我自己回复。** 全程只读微信,**没有任何发送通道**。

## 设计原则(硬约束)
- **只读、只收**:绝不提供任何向微信发送消息的通道;AI 只产出草稿供参考。
- **数据不出本机**:消息都来自本机微信本地库,通过 `127.0.0.1` 读取。
- **不改微信、不关 SIP**:不修改官方微信 App、不改签名,SIP 保持开启。
- **机密只存本地**:解密密钥、网关令牌只写入权限 `0600` 的本地文件,界面/接口/日志一律打码。

## 组成
- `app/` — 原生 Mac 外壳(Swift + AppKit + WebKit),菜单栏图标 + 伴侣浮窗;用 `swiftc` 从命令行构建,产物在 `app/dist/`。
- `wechat-frontend/` — 后端网关与页面(Python FastAPI + Uvicorn),本地回环提供接口、令牌鉴权。
- `wechat-realtime-reader/` — 只读近实时读取器(Go),解密本机微信本地 SQLite 库。
- `config/` — 配置模板(不含任何真实值/机密)。
- `install.sh` — 一键安装可自动化的部分。

## 环境要求
- macOS(Apple Silicon),微信 4.x 已登录并保持在线。
- Xcode Command Line Tools(提供 `swiftc`):`xcode-select --install`。
- Python 3。

## 快速安装
```bash
git clone https://github.com/lamp-cat/wechat-secretary.git
cd wechat-secretary
./install.sh
```
`install.sh` 会:建前端 venv 并装依赖、生成本机配置与网关令牌(0600)、编译出 `消息工作台.app`、把 App 指向本仓库目录、安装前端后台服务(launchd)。

## 一次性手动步骤(安装脚本不会替你做)
1. **抓微信数据库密钥**:登录并保持微信在线,运行 `wechat-frontend/scripts/recapture-keys.sh`。它会对微信做签名临时副本,用 Frida 抓出各本地库的解密密钥,写入 `~/.config/wechat-realtime-reader/`(权限 0600)。详见 `wechat-realtime-reader/README.md`。
2. **填数据源**:把 `~/.config/wechat-realtime-reader/source-config.json` 里的 `wxid` 与 `db_base_path` 改成你的真实值。
3. **开权限**:系统设置 → 隐私与安全性 → **完全磁盘访问权限**,加入读取器进程,否则读不到本地库。
4. **用 AI**:双击打开 App → 设置(⌘,)→ 填 Base URL、API Key,选模型并启用(任意 OpenAI 兼容接口)。

## 使用
- 双击 `app/dist/消息工作台.app`,或从 `app/dist/消息工作台.dmg` 安装。
- 启动后自动连接本机微信目录(复用已抓密钥),后台拉起读取器(5678)与网关(8088),打开伴侣浮窗。
- 菜单/快捷键:伴侣浮窗(⌘2)、置顶小窗(⌘3)、设置(⌘,)、重新加载(⌘R),以及重启后台 / 退出。

## 读取器常用命令
```bash
wechat-realtime-reader/bin/wechat-reader doctor   # 自检密钥/库(不打印密钥)
wechat-realtime-reader/bin/wechat-reader sync     # 增量解密、同步快照
wechat-realtime-reader/bin/wechat-reader serve    # 只读 API + 页面 + MCP(127.0.0.1:5678,令牌鉴权)
```

## 安全
- 数据只在本机、只读、只收;机密只存 0600 本地文件并全程打码;不改微信、不关 SIP;遇到任何微信安全提示应立即停止。
- 网关/读取器令牌等同全量读权限,请当机密对待,勿分享。

## 限制
- 单账号;只读、只收,**没有发送路径**。
- `wxgf` 原图需 `ffmpeg`(`brew install ffmpeg`),否则回退缩略图。
- 远程查看不在本项目范围(可自行用跳板/中转实现)。
