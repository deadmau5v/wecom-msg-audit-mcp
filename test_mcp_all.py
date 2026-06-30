# -*- coding: utf-8 -*-
"""测试 MCP 服务全部工具（使用本地 .env 配置）。"""

from __future__ import annotations

import asyncio
import json
import traceback
from typing import Any

from fastmcp import Client

MCP_URL = "http://127.0.0.1:8331/mcp"


def short(data: Any, max_len: int = 600) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > max_len:
        return text[:max_len] + "\n... (truncated)"
    return text


async def run_tool(client: Client, name: str, args: dict | None = None) -> dict:
    args = args or {}
    print(f"\n{'=' * 60}")
    print(f"TOOL: {name}")
    print(f"ARGS: {json.dumps(args, ensure_ascii=False)}")
    print("-" * 60)
    try:
        result = await client.call_tool(name, args)
        data = result.structured_content
        print("STATUS: OK")
        print(short(data))
        return {"tool": name, "status": "ok", "result": data}
    except Exception as e:
        print(f"STATUS: FAIL - {e}")
        traceback.print_exc()
        return {"tool": name, "status": "fail", "error": str(e)}


async def main():
    results: list[dict] = []

    async with Client(MCP_URL) as client:
        tools = await client.list_tools()
        print("已注册工具:", [t.name for t in tools])

        results.append(await run_tool(client, "wecom_get_config_status", {}))

        results.append(await run_tool(client, "wecom_get_seq", {}))

        results.append(
            await run_tool(
                client,
                "wecom_pull_messages",
                {"limit": 10, "save_to_file": True},
            )
        )

        results.append(
            await run_tool(
                client,
                "wecom_query_messages",
                {"limit": 3, "msgtype": "text"},
            )
        )

        results.append(
            await run_tool(
                client,
                "wecom_query_messages",
                {"keyword": "你好", "limit": 5},
            )
        )

        results.append(await run_tool(client, "wecom_extract_roomids", {}))

        results.append(
            await run_tool(
                client,
                "wecom_get_group_info",
                {"roomid": "test_roomid_placeholder", "group_type": "auto"},
            )
        )

        results.append(
            await run_tool(client, "wecom_list_external_groups", {"limit": 5})
        )

        seq_result = await run_tool(client, "wecom_get_seq", {})
        current_seq = 0
        if seq_result.get("status") == "ok":
            result_text = seq_result["result"]
            if isinstance(result_text, str):
                for line in result_text.splitlines():
                    if line.startswith("当前消息拉取进度 seq:"):
                        current_seq = int(line.split(":", 1)[1].strip())
                        break
            elif isinstance(result_text, dict):
                current_seq = result_text.get("current_seq", 0)

        results.append(
            await run_tool(
                client,
                "wecom_set_seq",
                {"seq": current_seq},
            )
        )

        results.append(await run_tool(client, "wecom_get_seq", {}))

    print(f"\n{'=' * 60}")
    print("测试汇总")
    print("=" * 60)
    ok = sum(1 for r in results if r["status"] == "ok")
    fail = sum(1 for r in results if r["status"] == "fail")
    for r in results:
        mark = "✓" if r["status"] == "ok" else "✗"
        print(f"  {mark} {r['tool']}")
    print(f"\n总计: {len(results)} 项, 成功 {ok}, 失败 {fail}")


if __name__ == "__main__":
    asyncio.run(main())