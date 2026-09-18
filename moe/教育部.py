# -*- coding: utf-8 -*-
"""教育部硕士研究生招生工作管理规定 —— 发布监控（入口）

用法：
    python 教育部.py                     常驻运行，按 config.CHECK_INTERVAL 循环检查
    python 教育部.py --once              只检查一轮就退出（适合计划任务 / GitHub Actions）
    python 教育部.py --dry-run           只检查、不发邮件，用来验证抓取是否正常
    python 教育部.py --interval 600      临时把检查间隔改成 600 秒
    python 教育部.py --test-mail         只发一封测试邮件，确认邮箱通道
    python 教育部.py --state-dir DIR     把状态/日志/正文都写到 DIR 下（云端用，
                                         不传就用 config.py 里的 data/ 和 logs/）

监控年份、关键词、数据源、邮箱都在 config.py 里改。
"""

import argparse
import os
import sys


def _prepare_console():
    """Windows 控制台默认 GBK，直接打印中文可能抛编码错误。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                               # noqa: BLE001
            pass


def _apply_state_dir(config, base):
    """把产物目录整体挪到 base 下（GitHub Actions 里指到 actions/cache 的目录）。"""
    base = os.path.abspath(base)
    config.DATA_DIR = os.path.join(base, "data")
    config.LOG_DIR = os.path.join(base, "logs")
    config.DOCUMENT_DIR = os.path.join(config.DATA_DIR, "documents")
    config.STATE_FILE = os.path.join(config.DATA_DIR, "state.json")
    config.LOG_FILE = os.path.join(config.LOG_DIR, "monitor.log")


def main(argv=None):
    _prepare_console()

    code_dir = os.path.dirname(os.path.abspath(__file__))
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)

    parser = argparse.ArgumentParser(
        description="监控教育部《全国硕士研究生招生工作管理规定》发布情况")
    parser.add_argument("--once", action="store_true", help="只检查一轮后退出")
    parser.add_argument("--dry-run", action="store_true", help="不发邮件（试运行）")
    parser.add_argument("--interval", type=int, default=None, help="检查间隔（秒）")
    parser.add_argument("--test-mail", action="store_true", help="只发测试邮件")
    parser.add_argument("--state-dir", default=None,
                        help="状态/日志/正文的输出目录（默认用 config.py 里的设置）")
    args = parser.parse_args(argv)

    import config

    if args.state_dir:
        _apply_state_dir(config, args.state_dir)

    if args.interval and args.interval > 0:
        config.CHECK_INTERVAL = args.interval

    from moe_monitor import core, monitor, notify, state as state_mod

    if args.test_mail:
        state_mod.load()
        ok = notify.send(
            f"【测试】教育部{config.TARGET_YEAR}年招生规定监控邮件通道",
            f"<p>这是一封测试邮件。收到即表示 SMTP 通道可用。</p>"
            f"<p>监控年份：{config.TARGET_YEAR}<br>"
            f"检查间隔：{config.CHECK_INTERVAL} 秒</p>")
        core.log("测试邮件发送成功。" if ok else "测试邮件发送失败，请查看上面的错误。")
        return 0 if ok else 1

    send_mail = not args.dry_run
    if args.once:
        monitor.run_once(send_mail=send_mail)
        return 0

    monitor.run_forever(send_mail=send_mail)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
