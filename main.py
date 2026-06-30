#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""企业微信会话内容存档 CLI：拉取并解密聊天记录。"""

from __future__ import annotations

import argparse
import json
import sys
import time

from wecom_core import WeComConfig, pull_messages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="企业微信会话内容存档 CLI：拉取并解密聊天记录",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="单次拉取条数（覆盖 FETCH_LIMIT），最大 1000",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="启用循环拉取模式",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="循环拉取间隔秒数（默认 10）",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="不将解密消息追加到本地 JSONL",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    config = WeComConfig.from_env()
    missing = config.validate_for_pull()
    if missing:
        print(
            "缺少必要配置：\n"
            + "\n".join(f"  - {item}" for item in missing)
            + "\n\n请在项目根目录创建 .env 文件（可参考 .env.example）。",
            file=sys.stderr,
        )
        return 1

    while True:
        result = pull_messages(
            config,
            limit=args.limit,
            save_to_file=not args.no_save,
        )
        print(
            f"本次完成：成功 {result.success_count} 条，失败 {result.fail_count} 条，"
            f"seq {result.start_seq} -> {result.end_seq}"
        )
        for msg in result.messages:
            print(json.dumps(msg, ensure_ascii=False, indent=2))

        if not args.loop:
            break
        time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
