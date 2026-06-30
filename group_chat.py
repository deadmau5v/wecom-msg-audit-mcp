# -*- coding: utf-8 -*-
"""从会话存档消息中识别群聊 roomid，并补充群资料、导出群聊记录。"""

import json
from pathlib import Path

from wecom_core import (
    WeComConfig,
    append_jsonl,
    get_external_group_info,
    get_internal_group_info,
    list_external_groups,
    load_roomids_from_messages,
)

EXTERNAL_GROUPS_JSONL = "./external_groups.jsonl"
INTERNAL_GROUPS_JSONL = "./internal_groups.jsonl"
EXTERNAL_GROUP_MESSAGES_JSONL = "./external_group_messages.jsonl"
INTERNAL_GROUP_MESSAGES_JSONL = "./internal_group_messages.jsonl"


def export_group_messages(
    messages_jsonl: str,
    group_ids: set[str],
    output_jsonl: str,
) -> int:
    count = 0
    with open(messages_jsonl, "r", encoding="utf-8") as src, open(
        output_jsonl, "w", encoding="utf-8"
    ) as dst:
        for line in src:
            if not line.strip():
                continue
            row = json.loads(line)
            roomid = row.get("message", {}).get("roomid")
            if roomid in group_ids:
                dst.write(json.dumps(row, ensure_ascii=False))
                dst.write("\n")
                count += 1
    return count


def fetch_external_groups(config: WeComConfig, roomids: set[str]) -> set[str]:
    external_ids = set()
    Path(EXTERNAL_GROUPS_JSONL).write_text("", encoding="utf-8")

    for roomid in sorted(roomids):
        data = get_external_group_info(config, roomid)
        if data.get("errcode") == 0:
            group_chat = data.get("group_chat", {})
            external_ids.add(roomid)
            print(f"[外部客户群] {roomid} - {group_chat.get('name', '(无群名)')}")
            append_jsonl(
                EXTERNAL_GROUPS_JSONL,
                {
                    "roomid": roomid,
                    "type": "external_customer_group",
                    "group_chat": group_chat,
                },
            )
        else:
            print(
                f"[非客户群或无权限] {roomid} - errcode={data.get('errcode')} "
                f"{data.get('errmsg')}"
            )

    return external_ids


def fetch_internal_groups(config: WeComConfig, roomids: set[str]) -> set[str]:
    internal_ids = set()
    Path(INTERNAL_GROUPS_JSONL).write_text("", encoding="utf-8")

    for roomid in sorted(roomids):
        data = get_internal_group_info(config, roomid)
        if data.get("errcode") == 0:
            internal_ids.add(roomid)
            print(f"[内部群] {roomid} - {data.get('roomname', '(无群名)')}")
            append_jsonl(
                INTERNAL_GROUPS_JSONL,
                {
                    "roomid": roomid,
                    "type": "internal_group",
                    "group_info": data,
                },
            )
        else:
            print(
                f"[非内部群或无权限] {roomid} - errcode={data.get('errcode')} "
                f"{data.get('errmsg')}"
            )

    return internal_ids


def main():
    config = WeComConfig.from_env()
    if not config.corp_id:
        raise RuntimeError("缺少 CORP_ID，请在 .env 中配置")

    roomids = load_roomids_from_messages(config.output_jsonl)
    print(f"从 {config.output_jsonl} 中发现群聊 roomid 数量: {len(roomids)}")

    if not roomids:
        print("当前解密消息中没有群聊记录（roomid 均为空）。")

    external_ids: set[str] = set()
    internal_ids: set[str] = set()

    if roomids and config.external_contact_secret:
        print("\n--- 查询外部客户群详情 ---")
        external_ids = fetch_external_groups(config, roomids)
        try:
            listed = list_external_groups(config)
            print(f"\n客户群列表接口返回群数量: {len(listed)}")
            for g in listed[:10]:
                print(f"  chat_id={g.get('chat_id')} status={g.get('status')}")
        except RuntimeError as e:
            print(f"客户群列表查询失败: {e}")
    elif roomids:
        print("\n未配置 EXTERNAL_CONTACT_SECRET，跳过外部客户群详情查询。")

    if roomids and config.msgaudit_secret:
        print("\n--- 查询内部群详情 ---")
        internal_ids = fetch_internal_groups(config, roomids)

    if external_ids:
        count = export_group_messages(
            config.output_jsonl, external_ids, EXTERNAL_GROUP_MESSAGES_JSONL
        )
        print(f"\n已导出外部群消息 {count} 条 -> {EXTERNAL_GROUP_MESSAGES_JSONL}")

    if internal_ids:
        count = export_group_messages(
            config.output_jsonl, internal_ids, INTERNAL_GROUP_MESSAGES_JSONL
        )
        print(f"已导出内部群消息 {count} 条 -> {INTERNAL_GROUP_MESSAGES_JSONL}")

    print(
        f"\n完成。roomid 总数={len(roomids)}, "
        f"外部客户群={len(external_ids)}, 内部群={len(internal_ids)}"
    )


if __name__ == "__main__":
    main()