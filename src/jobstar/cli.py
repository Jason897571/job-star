"""命令行入口。`jobstar <子命令>`。"""

from __future__ import annotations

import argparse
import sys

from jobstar.config import get_settings
from jobstar.db import get_conn, init_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobstar")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库")

    p_collect = sub.add_parser("collect", help="采集岗位（列表页 → 预门禁 → 详情页）")
    p_collect.add_argument("--keyword", required=True)
    p_collect.add_argument("--city", default="101210100", help="Boss 城市码，默认杭州")
    p_collect.add_argument("--pages", type=int, default=1)

    p_score = sub.add_parser("score", help="归一化 + 门禁 + 打分")
    p_score.add_argument("--limit", type=int, default=None)

    sub.add_parser("send", help="执行队列中已批准的动作")

    p_serve = sub.add_parser("serve", help="启动本地面板")
    p_serve.add_argument("--port", type=int, default=8777)

    sub.add_parser("cards", help="校验能力卡片库")

    args = parser.parse_args(argv)
    settings = get_settings()
    conn = get_conn(settings.db_path)
    init_db(conn)

    if args.cmd == "init":
        print(f"已建库：{settings.db_path}")
        return 0

    if args.cmd == "collect":
        from jobstar.pipeline import run_collect

        report = run_collect(
            conn, keyword=args.keyword, city_code=args.city, pages=args.pages
        )
        print(
            f"列表 {report.listed} 条，新增 {report.new}，"
            f"预门禁刷掉 {report.gated_out}，抓详情 {report.detail_fetched}"
        )
        if report.listed == 0 and report.errors:
            # fetch_list 把「关键词零结果」和「页面被拦截」合并成同一种
            # CollectError（二者在采集器这一层无法区分），run_collect 把它
            # 转成了 report.errors 里的一条记录而不是让异常往外炸。这里不
            # 装作能分辨到底是哪一种，只如实告诉用户这条歧义，让人自己去
            # Boss 上核实关键词是否真的没有匹配。report.errors 在这条路径下
            # 只有这一条、且说的是同一件事，不再用下面的通用循环重复打印一遍。
            print(
                "  ⚠️  本次没有抓到任何列表条目——可能是关键词真的零匹配，"
                "也可能是页面被拦截，无法自动区分，请人工核实",
                file=sys.stderr,
            )
        else:
            for err in report.errors:
                print(f"  ! {err}", file=sys.stderr)
        return 0

    if args.cmd == "score":
        from jobstar.pipeline import run_score

        report = run_score(conn, limit=args.limit)
        print(
            f"打分 {report.scored}，门禁刷掉 {report.gated_out}，"
            f"失败 {report.failed}，入队 {report.enqueued}"
        )
        for err in report.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 0

    if args.cmd == "send":
        from jobstar.executor import run_queue

        report = run_queue(conn)
        print(
            f"已发送 {report.sent}，失败 {report.failed}，跳过 {report.skipped}，"
            f"触达配额 {report.quota_hit}"
        )
        if report.uncertain:
            # uncertain 不是一个普通数字：它意味着发送按钮已经点了、但读回
            # 确认失败，消息很可能已经真的发出去了。不能和 sent/failed 混在
            # 一行里让人一扫而过——必须让人明确知道这几条要去 Boss 对话列表
            # 里人工核实，不能自己再批准重发。
            print(
                f"⚠️  {report.uncertain} 条发送结果不确定：消息可能已经发出，"
                "请到 Boss 对话列表人工核实，不要重新批准这些动作",
                file=sys.stderr,
            )
        if report.login_required:
            print(
                "⚠️  登录态失效，本轮执行已中止，请重新登录 Boss 后再运行 send",
                file=sys.stderr,
            )
        for err in report.errors:
            print(f"  ! {err}", file=sys.stderr)
        return 0

    if args.cmd == "serve":
        import uvicorn

        from jobstar.panel.app import app

        uvicorn.run(app, host="127.0.0.1", port=args.port)
        return 0

    if args.cmd == "cards":
        from jobstar.evidence import cards_to_prompt_block, load_cards

        cards = load_cards(settings.cards_path)
        block = cards_to_prompt_block(cards)
        print(f"{len(cards)} 张卡片，prompt 块 {len(block)} 字符")
        from collections import Counter

        for strength, count in Counter(c.strength.value for c in cards).items():
            print(f"  证据强度 {strength}: {count} 张")
        if len(cards) > 200 or len(block) > 30000:
            print("⚠️  已超过设计文档 §5.2 的重新评估阈值，考虑换成向量检索")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
