# -*- coding: utf-8 -*-
"""企业微信会话内容存档 MCP 服务（FastMCP）。"""

from __future__ import annotations

import os
import secrets
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.lifespan import lifespan
from starlette.responses import JSONResponse

from wecom_core import (
    WeComConfig,
    download_media,
    get_external_group_info,
    get_internal_group_info,
    list_external_groups,
    load_roomids_from_messages,
    load_seq,
    pull_messages,
    query_messages,
    save_seq,
)

# ----------------------------------------------------------------------------
# 常量（可通过环境变量覆盖）
# ----------------------------------------------------------------------------
SERVICE_NAME = "wecom-msg-audit-mcp"
SERVICE_INSTRUCTIONS = (
    "提供企业微信会话内容存档能力：拉取并解密聊天记录、查询本地消息、"
    "获取群聊信息。服务启动时已预配置，直接调用工具即可。"
)

BEARER_TOKEN_ENV = "MCP_BEARER_TOKEN"
ENV_FILE = Path(__file__).resolve().parent / ".env"
HEALTH_ROUTE = "/health"

DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8331


# ----------------------------------------------------------------------------
# 鉴权
# ----------------------------------------------------------------------------
class BearerTokenVerifier(TokenVerifier):
    """校验 HTTP 模式下的 Bearer Token。"""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self._token or token != self._token:
            return None
        return AccessToken(
            token=token,
            client_id="mcp-client",
            scopes=[],
            claims={},
        )


def _read_env_value(env_file: Path, key: str) -> str | None:
    if not env_file.exists():
        return None
    prefix = f"{key}="
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        val = line[len(prefix):].strip().strip('"').strip("'")
        return val or None
    return None


def resolve_bearer_token() -> tuple[str, str]:
    """解析 Bearer Token。

    优先级：进程环境变量 > .env 文件 > 自动生成（仅本次进程有效）。
    返回 (token, source)，其中 source 用于启动日志。
    """
    token = os.getenv(BEARER_TOKEN_ENV)
    if token:
        return token, "env"

    token = _read_env_value(ENV_FILE, BEARER_TOKEN_ENV)
    if token:
        return token, "env-file"

    token = secrets.token_urlsafe(32)
    return token, "generated"


# ----------------------------------------------------------------------------
# 消息格式化
# ----------------------------------------------------------------------------
_SHORT_LABELS: dict[str, str] = {
    "image": "[图片]", "video": "[视频]", "voice": "[语音]",
    "emoticon": "[表情]", "card": "[名片]", "location": "[位置]",
    "redpacket": "[红包]", "meeting": "[会议]", "docmsg": "[在线文档]",
    "calendar": "[日历]", "todo": "[待办]", "vote": "[投票]",
    "collect": "[填表]", "qydisk": "[企业云盘]", "mixed": "[图文混排]",
    "agree": "[同意会话存档]", "disagree": "[不同意会话存档]",
    "revoke": "[撤回消息]",
}


def _human_size(size: int) -> str:
    if size >= 1024:
        return f"{size / 1024:.0f}KB"
    return f"{size}B"


def _extract_content(msg: dict) -> str:
    msgtype = msg.get("msgtype", "")
    if msgtype == "text":
        return msg.get("text", {}).get("content", "")

    if msgtype == "image":
        size = msg.get("image", {}).get("filesize", 0)
        return f"[图片] {_human_size(size)}" if size else "[图片]"

    if msgtype == "video":
        size = msg.get("video", {}).get("filesize", 0)
        return f"[视频] {_human_size(size)}" if size else "[视频]"

    if msgtype == "voice":
        size = msg.get("voice", {}).get("filesize", 0)
        return f"[语音] {_human_size(size)}" if size else "[语音]"

    if msgtype == "emoticon":
        emo = msg.get("emoticon", {})
        size = emo.get("imagesize", 0) or emo.get("filesize", 0)
        desc = (emo.get("md5sum", "") or "")[:8]
        parts = ["[表情]"]
        if size:
            parts.append(_human_size(size))
        if desc:
            parts.append(f"#{desc}")
        return " ".join(parts)

    if msgtype == "file":
        file = msg.get("file", {})
        filename = file.get("filename", "")
        size = file.get("filesize", 0)
        size_str = f" {_human_size(size)}" if size else ""
        return f"[文件] {filename}{size_str}" if filename else f"[文件]{size_str}"

    if msgtype == "link":
        link = msg.get("link", {})
        parts = [p for p in (link.get("title", ""), link.get("description", "")) if p]
        return f"[链接] {' - '.join(parts)}" if parts else "[链接]"

    if msgtype == "sphfeed":
        sph = msg.get("sphfeed", {})
        parts = [p for p in (sph.get("sph_name", ""), sph.get("feed_desc", "")) if p]
        return f"[视频号] {' - '.join(parts)}" if parts else "[视频号]"

    if msgtype == "location":
        addr = msg.get("location", {}).get("address", "")
        return f"[位置] {addr}" if addr else "[位置]"

    if msgtype == "mixed":
        parts: list[str] = []
        for it in msg.get("mixed", {}).get("item", []):
            t = it.get("type", "")
            c = it.get("content", "")
            if t == "text" and c:
                parts.append(c)
            elif t:
                parts.append(_SHORT_LABELS.get(t, f"[{t}]"))
        return f"[混排] {' '.join(parts)}" if parts else "[混排]"

    return _SHORT_LABELS.get(msgtype, f"[{msgtype}]")


def _fmt_msg(msg: dict) -> str:
    m = msg.get("message", msg)
    if m.get("action") == "switch":
        ts = datetime.fromtimestamp(m.get("time", 0) / 1000).strftime("%m-%d %H:%M:%S")
        return f"[{ts}] {m.get('user', '?')} [切换企业]"

    ts = datetime.fromtimestamp(m.get("msgtime", 0) / 1000).strftime("%m-%d %H:%M:%S")
    sender = m.get("from", "?")
    roomid = m.get("roomid", "")
    content = _extract_content(m)

    if roomid:
        return f"[{ts}] {sender} @群 {roomid}: {content}"
    return f"[{ts}] {sender}: {content}"


def _format_messages_md(messages: list[dict]) -> str:
    return "\n".join(
        _fmt_msg(m) for m in messages if m.get("message", m).get("action") != "switch"
    )


def _api_error_message(data: dict[str, Any]) -> str:
    errmsg = data.get("errmsg", "未知错误")
    if isinstance(errmsg, str) and errmsg.startswith("ok"):
        return "未知错误"
    return str(errmsg)


def _format_external_group(group_chat: dict[str, Any]) -> str:
    name = group_chat.get("name") or "(无群名)"
    owner = group_chat.get("owner", "?")
    members = group_chat.get("member_list", [])
    lines = [
        f"群名: {name}",
        "类型: 外部客户群",
        f"群主: {owner}",
        f"成员数: {len(members)}",
    ]
    if members:
        lines.append("成员:")
        for m in members[:20]:
            uid = m.get("userid", "?")
            mtype = m.get("type", "")
            lines.append(f"  - {uid}" + (f" ({mtype})" if mtype else ""))
        if len(members) > 20:
            lines.append(f"  ... 共 {len(members)} 人")
    return "\n".join(lines)


def _format_internal_group(data: dict[str, Any]) -> str:
    name = data.get("roomname") or "(无群名)"
    creator = data.get("creator", "?")
    members = data.get("members", [])
    lines = [
        f"群名: {name}",
        "类型: 内部群",
        f"创建者: {creator}",
        f"成员数: {len(members)}",
    ]
    if members:
        lines.append("成员:")
        for m in members[:20]:
            uid = m.get("memberid", m.get("userid", "?"))
            lines.append(f"  - {uid}")
        if len(members) > 20:
            lines.append(f"  ... 共 {len(members)} 人")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# MCP 应用
# ----------------------------------------------------------------------------
@lifespan
async def app_lifespan(server):
    config = WeComConfig.from_env()
    try:
        yield {"config": config}
    finally:
        pass


_bearer_token, _bearer_source = resolve_bearer_token()

mcp = FastMCP(
    SERVICE_NAME,
    instructions=SERVICE_INSTRUCTIONS,
    lifespan=app_lifespan,
    auth=BearerTokenVerifier(_bearer_token),
)


# ----------------------------------------------------------------------------
# 工具定义
# ----------------------------------------------------------------------------
@mcp.tool
def wecom_get_config_status(ctx: Context) -> dict[str, Any]:
    """检查服务是否就绪（消息拉取、群聊查询等能力是否可用）。"""
    config = ctx.lifespan_context["config"]
    return config.client_status()


@mcp.tool
def wecom_pull_messages(
    ctx: Context,
    limit: int = 100,
    save_to_file: bool = True,
) -> dict[str, Any]:
    """从企业微信会话存档 SDK 拉取并解密聊天记录。

    从上次保存的 seq 位置继续拉取，解密后返回消息列表，并自动更新 seq。

    Args:
        limit: 单次拉取条数，最大 1000
        save_to_file: 是否将解密消息追加保存到本地
    """
    config = ctx.lifespan_context["config"]
    result = pull_messages(config, limit=limit, save_to_file=save_to_file)

    lines = [
        f"拉取完成: seq {result.start_seq}→{result.end_seq}, "
        f"成功{result.success_count}条, 失败{result.fail_count}条",
    ]
    if result.messages:
        lines.append("")
        lines.append(_format_messages_md(result.messages))
    if result.errors:
        lines.append("")
        lines.append("错误:")
        for e in result.errors:
            lines.append(f"  - seq={e.get('seq')}: {e.get('error')}")
    return {"result": "\n".join(lines)}


@mcp.tool
def wecom_get_seq(ctx: Context) -> str:
    """获取当前消息拉取进度 seq。"""
    config = ctx.lifespan_context["config"]
    current = load_seq(config.seq_file)
    return (
        f"当前消息拉取进度 seq: {current}\n\n"
        "用法说明:\n"
        "- 调用 wecom_pull_messages 从当前 seq 继续拉取新消息\n"
        "- 调用 wecom_set_seq 可重置或跳过历史消息的起始位置\n"
        "- seq 会在每次成功拉取后自动更新为最新值"
    )


@mcp.tool
def wecom_set_seq(ctx: Context, seq: int) -> str:
    """设置消息拉取起始 seq（用于重置或跳过历史消息）。

    Args:
        seq: 新的 seq 值
    """
    config = ctx.lifespan_context["config"]
    save_seq(config.seq_file, seq)
    return (
        f"已将 seq 设置为 {seq}\n\n"
        "用法说明:\n"
        f"- 下次调用 wecom_pull_messages 将从 seq {seq} 开始拉取\n"
        "- 如需查看最新进度，调用 wecom_get_seq"
    )


@mcp.tool
def wecom_query_messages(
    ctx: Context,
    roomid: str | None = None,
    from_user: str | None = None,
    msgtype: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """查询本地已保存的解密聊天记录。

    Args:
        roomid: 按群聊 roomid 过滤
        from_user: 按发送者 userid 过滤
        msgtype: 按消息类型过滤（text、image、file 等）
        keyword: 按关键词全文搜索
        limit: 返回条数上限
        offset: 分页偏移
    """
    config = ctx.lifespan_context["config"]
    data = query_messages(
        config.output_jsonl,
        roomid=roomid,
        from_user=from_user,
        msgtype=msgtype,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    lines = [
        f"查询结果: 共{data['total']}条 (offset={data['offset']}, limit={data['limit']})"
    ]
    if data["messages"]:
        lines.append("")
        lines.append(_format_messages_md(data["messages"]))
    return {"result": "\n".join(lines)}


@mcp.tool
def wecom_extract_roomids(ctx: Context) -> dict[str, Any]:
    """从本地消息记录中提取所有群聊 roomid。"""
    config = ctx.lifespan_context["config"]
    roomids = sorted(load_roomids_from_messages(config.output_jsonl))
    return {
        "count": len(roomids),
        "roomids": roomids,
        "result": (
            "\n".join([f"共发现 {len(roomids)} 个群聊 roomid:", *(f"  - {r}" for r in roomids)])
            if roomids
            else "当前消息记录中暂无群聊。"
        ),
    }


@mcp.tool
def wecom_get_group_info(
    ctx: Context,
    roomid: str,
    group_type: str = "auto",
) -> dict[str, Any]:
    """获取群聊详情（外部客户群或内部群）。

    Args:
        roomid: 群聊 roomid
        group_type: 群类型，可选 auto（自动尝试）、external（外部客户群）、internal（内部群）
    """
    config = ctx.lifespan_context["config"]
    result: dict[str, Any] = {"roomid": roomid, "found": False}

    if group_type in ("auto", "external"):
        try:
            ext = get_external_group_info(config, roomid)
            if ext.get("errcode") == 0:
                group_chat = ext.get("group_chat", {})
                result.update(
                    found=True,
                    group_type="external",
                    name=group_chat.get("name") or "",
                    member_count=len(group_chat.get("member_list", [])),
                    result=_format_external_group(group_chat),
                )
                return result
            result.setdefault("errors", []).append(
                f"外部客户群: {_api_error_message(ext)}"
            )
        except RuntimeError as e:
            result.setdefault("errors", []).append(str(e))

    if group_type in ("auto", "internal"):
        try:
            internal = get_internal_group_info(config, roomid)
            if internal.get("errcode") == 0:
                result.update(
                    found=True,
                    group_type="internal",
                    name=internal.get("roomname") or "",
                    member_count=len(internal.get("members", [])),
                    result=_format_internal_group(internal),
                )
                return result
            result.setdefault("errors", []).append(
                f"内部群: {_api_error_message(internal)}"
            )
        except RuntimeError as e:
            result.setdefault("errors", []).append(str(e))

    errors = result.get("errors", [])
    if errors:
        result["result"] = f"未找到群聊 {roomid}\n" + "\n".join(f"- {e}" for e in errors)
    else:
        result["result"] = f"未找到群聊 {roomid}"
    return result


@mcp.tool
def wecom_list_external_groups(
    ctx: Context,
    limit: int = 100,
) -> dict[str, Any]:
    """列出外部客户群。

    Args:
        limit: 返回群数量上限
    """
    config = ctx.lifespan_context["config"]
    groups = list_external_groups(config, limit=limit)
    lines = [f"外部客户群共 {len(groups)} 个:"]
    for g in groups:
        chat_id = g.get("chat_id", "?")
        status = g.get("status", "")
        lines.append(f"  - {chat_id}" + (f" (status={status})" if status != "" else ""))
    return {
        "count": len(groups),
        "groups": [{"chat_id": g.get("chat_id"), "status": g.get("status")} for g in groups],
        "result": "\n".join(lines),
    }


@mcp.tool
def wecom_download_media(
    ctx: Context,
    sdkfileid: str,
    save_to: str | None = None,
) -> str:
    """下载消息中的媒体文件（图片、视频、语音、文件等）。

    通过 sdkfileid 下载媒体内容。图片上传到 R2 并返回预签名访问链接，其他文件保存到磁盘。
    也可指定 save_to 路径将文件保存到指定位置。

    Args:
        sdkfileid: 媒体文件 ID（从消息的 sdkfileids 字段获取）
        save_to: 保存路径，不传则图片返回 R2 预签名链接、其他文件自动保存
    """
    config = ctx.lifespan_context["config"]
    return download_media(config, sdkfileid=sdkfileid, save_to=save_to)


@mcp.custom_route(HEALTH_ROUTE, methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": SERVICE_NAME})


# ----------------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------------
def run_server() -> None:
    """MCP 服务入口（供 console_scripts 调用）。"""
    transport = os.getenv("MCP_TRANSPORT", DEFAULT_TRANSPORT).lower()
    host = os.getenv("MCP_HOST", DEFAULT_HOST)
    port = int(os.getenv("MCP_PORT", str(DEFAULT_PORT)))

    if transport == "http":
        if _bearer_source == "generated":
            print(
                f"[{SERVICE_NAME}] 未检测到 {BEARER_TOKEN_ENV}，已自动生成 Bearer Token "
                f"（仅本次进程有效）:\n  {_bearer_token}\n"
                f"[{SERVICE_NAME}] 建议通过 {BEARER_TOKEN_ENV} 环境变量显式注入。",
                file=sys.stderr,
            )
        else:
            print(
                f"[{SERVICE_NAME}] HTTP 模式启动于 http://{host}:{port}/mcp "
                f"（鉴权来源: {_bearer_source}）",
                file=sys.stderr,
            )
        mcp.run(transport="http", host=host, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    run_server()
