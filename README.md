# wecom-msg-audit-mcp

> 企业微信会话内容存档 MCP 服务：拉取、解密并查询聊天记录，让 AI Agent 拥有合规可审计的会话访问能力。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![FastMCP](https://img.shields.io/badge/MCP-FastMCP-purple)](https://gofastmcp.com)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 特性

- 🔌 **MCP 协议**：基于 [FastMCP](https://gofastmcp.com) 暴露工具，兼容 Cursor、Claude Desktop、Cline 等 MCP 客户端
- 🔐 **完整解密链路**：封装企业微信官方 `libWeWorkFinanceSdk_C.so`，自动处理 RSA 私钥解密 + SDK 对称解密
- 📨 **多种消息类型**：支持文本、图片、视频、语音、文件、链接、位置、名片、表情、视频号、混排等
- 🗂️ **本地检索**：解密结果持久化为 JSONL，支持按群、发送者、消息类型、关键词查询与分页
- 👥 **群资料查询**：自动识别内部群 / 外部客户群，补全群名称、成员列表
- ☁️ **媒体上传**：图片自动上传到 Cloudflare R2 并返回预签名链接（其他文件保存到本地）
- 🛡️ **Bearer 鉴权**：内置 Token 校验，支持 `stdio` / `http` 两种传输

## 架构

```
┌────────────────────┐
│  MCP 客户端         │
│  (Cursor / Claude) │
└─────────┬──────────┘
          │ MCP (stdio / http + Bearer)
          ▼
┌────────────────────┐
│  mcp_server.py     │  FastMCP 工具定义
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│  wecom_core.py     │  配置 / 业务逻辑
└─────────┬──────────┘
          │ ctypes
          ▼
┌────────────────────┐    ┌──────────────┐
│  libWeWorkFinance  │◄──►│  企业微信 API │
│  SDK_C.so (官方)    │    └──────────────┘
└────────────────────┘
```

## 快速开始

### 1. 前置条件

> 拉取会话内容需要先在企业微信管理后台（[work.weixin.qq.com](https://work.weixin.qq.com/wework_admin/loginpage_wx)）开启会话存档功能。开启方式和 API 参数说明见[企业微信开发者文档](https://developer.work.weixin.qq.com/document/path/91774)。

- **Python 3.11+**
- **企业微信会话内容存档权限**：登录 [管理后台](https://work.weixin.qq.com/wework_admin/loginpage_wx) → 安全与管理 → 管理工具 → 会话内容存档
- **企业微信官方 SDK**：[libWeWorkFinanceSdk_C.so](https://developer.work.weixin.qq.com/document/path/91774)（需自行从开放平台下载）
- **RSA 私钥**：在管理后台「会话内容存档」页面上传公钥，私钥用 `openssl` 在本地生成（**不要使用任何在线生成工具**，避免私钥外泄）：

  ```bash
  # 生成 2048 位 RSA 私钥（兼容企业微信会话存档）
  openssl genrsa -out private_key.pem 2048

  # 提取对应公钥，上传到管理后台
  openssl rsa -in private_key.pem -pubout -out public_key.pem
  ```

### 2. 安装

```bash
git clone https://github.com/deadmau5v/wecom-msg-audit-mcp.git
cd wecom-msg-audit-mcp

# 推荐使用 uv
uv sync

# 或使用 pip
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

将官方 SDK 文件 `libWeWorkFinanceSdk_C.so` 放置到项目根目录（或在 `.env` 中自定义 `SDK_LIB_PATH`）。

### 3. 配置

```bash
cp .env.example .env
# 编辑 .env，填入企业 ID、Secret、私钥路径等
```

### 4. 启动

#### 作为 MCP 服务（推荐）

```bash
# stdio 模式（默认）
uv run python mcp_server.py

# http 模式（带 Bearer 鉴权）
MCP_TRANSPORT=http MCP_PORT=8331 uv run python mcp_server.py
```

然后在 MCP 客户端配置中接入：

```json
{
  "mcpServers": {
    "wecom": {
      "command": "uv",
      "args": ["run", "python", "mcp_server.py"],
      "cwd": "/path/to/wecom-msg-audit-mcp"
    }
  }
}
```

#### 作为 CLI 拉取

```bash
uv run python main.py
```

输出解密后的消息到 stdout，并增量追加到 `wecom_messages.jsonl`。

## 配置项

所有配置通过环境变量（或 `.env` 文件）注入，**禁止硬编码到代码中**。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `CORP_ID` | ✅ | 企业 ID（我的企业 → 企业信息） |
| `MSGAUDIT_SECRET` | ✅ | 会话内容存档 Secret（**不是**普通应用 Secret） |
| `EXTERNAL_CONTACT_SECRET` | ⬜ | 客户联系可调用应用 Secret（用于外部客户群） |
| `SDK_LIB_PATH` | ⬜ | SDK 动态库路径，默认 `./libWeWorkFinanceSdk_C.so` |
| `PRIVATE_KEY_PATH` | ⬜ | RSA 私钥路径，默认 `./private_key.pem` |
| `SEQ_FILE` | ⬜ | seq 进度文件，默认 `./wecom_msg_seq.txt` |
| `OUTPUT_JSONL` | ⬜ | 解密消息输出文件，默认 `./wecom_messages.jsonl` |
| `FETCH_LIMIT` | ⬜ | 单次拉取条数，1-1000，默认 `100` |
| `SDK_TIMEOUT` | ⬜ | SDK 请求超时（秒），默认 `30` |
| `PROXY` / `PROXY_PASSWORD` | ⬜ | 代理与代理密码（按需） |
| `R2_ENDPOINT` | ⬜ | Cloudflare R2 端点，例：`https://xxx.r2.cloudflarestorage.com/<bucket>` |
| `R2_BUCKET` | ⬜ | R2 存储桶（若已包含在 endpoint 路径中可省略） |
| `R2_CUSTOM_DOMAIN` | ⬜ | 自定义访问域名（用于预签名 URL 替换） |
| `R2_TOKEN` | ⬜ | 兼容字段（当前未使用，可保留） |
| `R2_S3_ID` / `R2_S3_KEY` | ⬜ | R2 S3 兼容 AccessKey |
| `MCP_BEARER_TOKEN` | ⬜ | HTTP 模式下的 Bearer 鉴权 Token，留空则自动生成 |
| `MCP_TRANSPORT` | ⬜ | `stdio`（默认）或 `http` |
| `MCP_HOST` / `MCP_PORT` | ⬜ | HTTP 模式监听地址，默认 `127.0.0.1:8331` |

> 完整示例见 [.env.example](.env.example)。

## MCP 工具一览

| 工具 | 用途 |
| --- | --- |
| `wecom_get_config_status` | 检查服务就绪状态（各能力是否可用） |
| `wecom_pull_messages` | 从当前 seq 拉取并解密新消息 |
| `wecom_get_seq` / `wecom_set_seq` | 查看 / 重置拉取进度 |
| `wecom_query_messages` | 按群、发送者、类型、关键词检索本地消息 |
| `wecom_extract_roomids` | 从本地消息中提取所有群聊 roomid |
| `wecom_get_group_info` | 查询群聊详情（内部 / 外部自动判定） |
| `wecom_list_external_groups` | 列出外部客户群 |
| `wecom_download_media` | 下载 / 上传消息中的媒体文件 |

### 示例：让 Agent 拉取并搜索

```
User: 帮我拉取最近的聊天记录，搜索包含"发票"的消息

Agent: 调用 wecom_pull_messages → 解密
       调用 wecom_query_messages(keyword="发票", limit=20) → 返回结果
```

## 安全提醒

> ⚠️ **本项目会处理企业敏感数据，部署前请仔细阅读本节。**

1. **`.env` 绝不能提交到仓库**——本项目已通过 `.gitignore` 默认忽略；首次 `git add` 前请确认。
2. **`private_key.pem` 绝不能提交**——同理已忽略。若不慎泄露，请立即在企业微信管理后台 **重置公钥**。
3. **消息数据可能包含个人隐私**——`wecom_messages.jsonl`、群资料导出文件等已加入 `.gitignore`，请勿外发。
4. **Bearer Token 默认会写入 `.env`**——HTTP 模式启动时若未设置 `MCP_BEARER_TOKEN`，会自动生成并保存到 `.env`；若希望每次启动都使用临时 Token，请改为外部注入或自行改造启动脚本。
5. **部署时建议**：
   - 使用反向代理（HTTPS + IP 白名单）暴露 MCP HTTP 端点
   - 定期轮换 `MSGAUDIT_SECRET` 和 RSA 密钥对
   - 限制 `OUTPUT_JSONL` 所在目录的访问权限

## 项目结构

```
.
├── mcp_server.py        # FastMCP 入口，工具定义
├── wecom_core.py        # 核心业务：配置、SDK 封装、解密、查询
├── main.py              # CLI 入口：拉取并打印消息
├── group_chat.py        # 群资料识别与导出脚本
├── test_mcp_all.py      # MCP 工具冒烟测试
├── restart.sh           # tmux 一键重启脚本
├── fastmcp.json         # FastMCP 客户端配置
├── pyproject.toml       # 项目元数据
├── requirements.txt     # pip 依赖
├── .env.example         # 配置示例
├── .gitignore
├── LICENSE
└── README.md
```

## 开发

```bash
# 启动 HTTP 模式做本地调试
MCP_TRANSPORT=http MCP_PORT=8331 uv run python mcp_server.py

# 跑冒烟测试（需要先启动服务）
uv run python test_mcp_all.py
```

### 常见问题

**Q: 启动报 `SDK 动态库不存在`？**
A: 检查 `.env` 中 `SDK_LIB_PATH` 是否指向正确的 `.so` 文件，且有可执行权限。

**Q: 报 `RSA 解密 encrypt_random_key 失败`？**
A: 私钥与后台公钥版本不匹配，需在管理后台重新下载配套私钥。

**Q: 报 `errcode=60011`？**
A: 表示企业没有开通「会话内容存档」权限，需管理员在后台申请。

## License

[MIT](LICENSE)
