# -*- coding: utf-8 -*-
"""检查编排：跑一轮检查 -> 判断新命中 -> 发通知 -> 处理失败告警。"""

import random
import time
from datetime import datetime

import config
from . import core, notify, sources, state as state_mod


def do_check(state, first_run=False, send_mail=True):
    """执行一轮检查。返回 (state, 本轮新命中列表)。"""
    started = datetime.now()
    core.log("-" * 62)
    core.log(f"开始检查（第 {state.get('total_checks', 0) + 1} 轮，"
             f"目标年份 {config.TARGET_YEAR}）")

    candidates, errors = sources.run_all_sources()
    state = state_mod.touch(state)

    all_failed = len(errors) >= len(config.SOURCES)
    if all_failed:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    else:
        state["consecutive_failures"] = 0

    for err in errors:
        core.log(f"  源异常：{err}", "warning")

    # ---- 筛出新命中（已通知过的不再重复发） ----
    fresh = [c for c in candidates if not state_mod.is_notified(state, c.url)]
    for c in fresh:
        core.log(f"  新命中 [{c.kind}] {c.title}")

    if fresh:
        for c in fresh:
            if c.is_file:
                sources.enrich(c)
                c.doc_path = sources.save_document(c)
                core.log(f"  正文已保存：{c.doc_path}")
            else:
                # 信号类也跟进一下，多半能拿到指向文件本体的一手链接
                sources.enrich(c)

    # ---- 通知 ----
    if first_run:
        if send_mail:
            notify.notify_startup(state, fresh, errors)
        for c in fresh:
            state = state_mod.mark_notified(state, c.url)
    else:
        if fresh:
            for c in fresh:
                if send_mail:
                    _, state = notify.notify_hit(state, c, c.doc_path)
                else:
                    state = state_mod.mark_notified(state, c.url)
        else:
            core.log("  无新命中")

    # ---- 连续失败告警 ----
    threshold = config.FAILURE_ALERT_THRESHOLD
    fails = state["consecutive_failures"]
    alerted_at = int(state.get("alerted_at_failures") or 0)
    if all_failed and threshold and fails >= threshold and fails % threshold == 0 \
            and fails != alerted_at:
        core.log(f"连续 {fails} 轮全部数据源失败，发送告警邮件", "error")
        if send_mail:
            notify.notify_failure(state, errors, fails)
        state["alerted_at_failures"] = fails
    elif not all_failed:
        state["alerted_at_failures"] = 0

    # ---- 收尾 ----
    state_mod.save(state)
    elapsed = (datetime.now() - started).total_seconds()
    core.log(f"本轮结束，耗时 {elapsed:.1f}s，成功源 "
             f"{len(config.SOURCES) - len(errors)}/{len(config.SOURCES)}"
             + ("（全部失败）" if all_failed else ""))
    return state, fresh


def run_forever(send_mail=True):
    """常驻循环。任何单轮异常都不会让进程退出。"""
    state = state_mod.load()
    core.log("=" * 62)
    core.log(f"教育部 {config.TARGET_YEAR} 年硕士研究生招生工作管理规定 —— 发布监控")
    core.log(f"检查间隔：{config.CHECK_INTERVAL} 秒（约 "
             f"{config.CHECK_INTERVAL / 3600:.1f} 小时）")
    core.log(f"数据源：{len(config.SOURCES)} 个"
             + ("（邮件通知已启用）" if send_mail else "（试运行，不发邮件）"))
    core.log(f"日志文件：{config.LOG_FILE}")
    core.log("=" * 62)

    try:
        state, _ = do_check(state, first_run=True, send_mail=send_mail)
    except Exception as exc:                            # noqa: BLE001
        core.log(f"首轮检查异常（不影响后续循环）：{type(exc).__name__}: {exc}", "error")

    while True:
        wait = config.CHECK_INTERVAL + random.randint(0, max(0, config.JITTER))
        core.log(f"休眠 {wait} 秒，下次检查约 "
                 f"{datetime.fromtimestamp(time.time() + wait):%Y-%m-%d %H:%M:%S}")
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            core.log("收到中断信号，退出。")
            return

        try:
            state, _ = do_check(state, first_run=False, send_mail=send_mail)
        except Exception as exc:                        # noqa: BLE001
            core.log(f"本轮检查异常（继续下一轮）：{type(exc).__name__}: {exc}", "error")
            core.log(f"  详见日志：{config.LOG_FILE}", "error")


def run_once(send_mail=True, quiet_if_empty=True):
    """只跑一轮，适合手动验证或放进系统计划任务。"""
    state = state_mod.load()
    state, fresh = do_check(state, first_run=False, send_mail=send_mail)
    if not fresh and quiet_if_empty:
        core.log(f"未发现 {config.TARGET_YEAR} 年相关文件。")
    return len(fresh)
