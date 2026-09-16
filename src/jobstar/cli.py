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
    p_score.add_argument(
        "--rescore",
        action="store_true",
        help="Minor 4：清掉 scoring_failed/pitch_failed/第二遍门禁刷掉这三种"
        "吸收态，让对应岗位能被重新处理（不传时行为和以前完全一样）",
    )
    p_score.add_argument(
        "--job-id",
        default=None,
        help="配合 --rescore 只重跑这一个岗位；不传则重跑所有符合条件的岗位",
    )

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
        from datetime import datetime

        from jobstar.collector.boss import LoginRequired
        from jobstar.config import set_setting
        from jobstar.pipeline import run_collect

        # 采集健康状态（Task 13）：写进 settings，供面板顶部横幅读取
        # （/api/health）。这是被动信号——只在真的跑了一次 collect 之后才
        # 更新，没有主动探活。last_collect_at 记的是「最近一次尝试」的完成
        # 时刻，成功失败都更新；last_collect_ok_at 只在真正成功（清空
        # last_collect_error 的同一条路径）时才前进。二者不相等，就说明
        # 最近一次尝试其实失败了——横幅据此判断能不能说「未见异常」，不能
        # 只看 last_collect_error 是否已被人工点掉（fix round 1，review
        # finding 1）。时间戳都在 run_collect 跑完/抛出之后才取，反映的是
        # 「这次尝试结束的时刻」而不是开始的时刻（fix round 1，finding 2）。
        try:
            report = run_collect(
                conn, keyword=args.keyword, city_code=args.city, pages=args.pages
            )
        except LoginRequired as exc:
            # run_collect 故意让 LoginRequired 原样往外炸穿（见 pipeline.py
            # 的注释）——登录态失效是会话级别的硬故障，不能被当成这一条采集
            # 的失败吞掉。这里接住它、写健康状态、给出明确的下一步动作。
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_setting(
                conn, "last_collect_error", f"Boss 登录态失效，采集已中止：{exc}"
            )
            set_setting(conn, "last_collect_at", now)
            print(f"采集中止：{exc}", file=sys.stderr)
            print(
                "请在 Chrome 里重新扫码登录 Boss 直聘后再运行 collect",
                file=sys.stderr,
            )
            return 2

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
            ambiguous = (
                "本次没有抓到任何列表条目——可能是关键词真的零匹配，"
                "也可能是页面被拦截，无法自动区分，请人工核实"
            )
            print(f"  ⚠️  {ambiguous}", file=sys.stderr)
            set_setting(conn, "last_collect_error", ambiguous)
        else:
            for err in report.errors:
                print(f"  ! {err}", file=sys.stderr)
            if report.errors:
                set_setting(
                    conn,
                    "last_collect_error",
                    f"{len(report.errors)} 个岗位抓取失败（可能是页面结构变更）："
                    + "；".join(report.errors[:3]),
                )
            else:
                set_setting(conn, "last_collect_error", None)
                set_setting(conn, "last_collect_ok_at", now)
        set_setting(conn, "last_collect_at", now)
        return 0

    if args.cmd == "score":
        from jobstar.pipeline import clear_for_rescore, run_score

        if args.job_id and not args.rescore:
            parser.error("--job-id 必须配合 --rescore 使用")

        if args.rescore:
            cleared = clear_for_rescore(conn, job_id=args.job_id)
            print(f"已清除 {cleared} 个岗位的吸收态（scoring_failed/pitch_failed/"
                  "第二遍门禁刷掉），准备重新处理")

        report = run_score(conn, limit=args.limit)
        print(
            f"打分 {report.scored}，门禁刷掉 {report.gated_out}，"
            f"失败 {report.failed}，话术失败 {report.pitch_failed}，"
            f"入队 {report.enqueued}"
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
