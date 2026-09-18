#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NUAA 机电学院「研究生招生」公告监测程序
目标页面: https://cmee.nuaa.edu.cn/11462/list.htm

特性:
  * 纯 Python 标准库实现，无需 pip install 任何第三方包
  * 通过文章 URL 中的文章 ID (c11462aNNNNNN) 作为唯一标识判断新公告
  * 分页自动扫描：逐页抓取，直到遇到已知公告为止（保证不漏检）
  * 网络异常自动重试 + 指数退避
  * 页面结构变化检测（解析到 0 条时报警，不清空已知记录）
  * 多种通知方式：桌面弹窗(Windows)、本地日志、HTML 报告、可选邮件/Webhook
  * 支持作为常驻守护进程运行，也可配合 Windows 任务计划程序 --once 模式运行
"""

import argparse
import ctypes
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import threading
import time
import urllib.error
import urllib.request

# ----------------------------- 配置 -----------------------------
DEFAULT_CONFIG = {
    "base_url": "https://cmee.nuaa.edu.cn/11462/list.htm",
    "base_domain": "https://cmee.nuaa.edu.cn",
    "check_interval_minutes": 30,   # 监测频率（分钟）
    "max_pages": 0,                 # 最多扫描页数；0 = 自动扫描到遇到已知公告
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "request_timeout": 20,          # 单次请求超时（秒）
    "max_retries": 3,               # 网络失败重试次数
    "retry_backoff": 5,             # 重试退避基数（秒），第 n 次等待 backoff*n
    "data_file": "seen_announcements.json",   # 已知公告持久化文件
    "report_html": "new_announcements.html",  # 新公告 HTML 报告
    "log_file": "monitor.log",
    "desktop_notify": True,         # Windows 桌面弹窗
    # 邮件配置：云端从环境变量读（SMTP_SENDER / SMTP_AUTH_CODE / SMTP_RECEIVER），
    # 三者齐全才启用；本地原样保持（None = 不发邮件，只弹窗+写报告）
    "email": (
        {
            "smtp_host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
            "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
            "user": os.environ.get("SMTP_SENDER", ""),
            "password": os.environ.get("SMTP_AUTH_CODE", ""),
            "to": os.environ.get("SMTP_RECEIVER", ""),
        }
        if (os.environ.get("SMTP_SENDER") and os.environ.get("SMTP_AUTH_CODE"))
        else None
    ),
    "webhook": None,                # 例如 "https://hooks.example.com/xxx" (POST JSON)
}

# ----------------------------- 异常 -----------------------------
class NetworkError(Exception):
    """网络层持续失败（重试后仍无法获取页面）"""


class StructureChanged(Exception):
    """页面结构变化，无法解析出公告列表"""


# ----------------------------- 解析 -----------------------------
# 匹配 <ul ... class="news_ul" ...> ... </ul>
UL_RE = re.compile(
    r'<ul\b[^>]*?\bclass\s*=\s*["\']?[^"\'>]*news_ul[^"\'>]*["\']?[^>]*>(.*?)</ul>',
    re.DOTALL | re.IGNORECASE,
)
LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.DOTALL | re.IGNORECASE)
HREF_RE = re.compile(r"<a\b[^>]*\bhref\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
TITLE_RE = re.compile(r"<a\b[^>]*\btitle\s*=\s*['\"]([^'\"]*)['\"]", re.IGNORECASE)
SPAN_RE = re.compile(r"<span[^>]*>([^<]*)</span>", re.IGNORECASE)
ID_RE = re.compile(r"c11462a(\d+)")
# 分页信息：<span class="all_count">总共 <em class="all_count">21</em> 记录 </span>
PAGING_TOTAL_RE = re.compile(r"总共\s*<em[^>]*>(\d+)</em>", re.IGNORECASE)
PAGING_PER_RE = re.compile(r"每页\s*<em[^>]*>(\d+)</em>", re.IGNORECASE)


def parse_announcements(page_html, base_domain):
    """从列表页 HTML 中解析出公告条目列表。

    返回: [{'id','title','url','date'}, ...] （保持页面原有顺序，最新在前）
    页面结构异常时抛出 StructureChanged。
    """
    m = UL_RE.search(page_html)
    if not m:
        raise StructureChanged("未找到 class='news_ul' 的公告列表容器，页面结构可能已变化")
    block = m.group(1)
    items = []
    for li in LI_RE.finditer(block):
        li_html = li.group(1)
        hm = HREF_RE.search(li_html)
        if not hm:
            continue  # 不是公告条目（如纯文本 li），跳过
        href = hm.group(1).strip()
        if not href.startswith("http"):
            href = base_domain + (href if href.startswith("/") else "/" + href)
        tm = TITLE_RE.search(li_html)
        title = html.unescape(tm.group(1).strip()) if tm else ""
        if not title:  # 兜底：用链接文本
            title = html.unescape(re.sub(r"<[^>]+>", "", li_html)).strip()
        sm = SPAN_RE.search(li_html)
        date = sm.group(1).strip() if sm else ""
        idm = ID_RE.search(href)
        aid = idm.group(0) if idm else href  # 文章 ID 优先，否则用完整 URL
        items.append({"id": aid, "title": title, "url": href, "date": date})
    # 注意：列表为空（末页/暂无公告）属正常情况，返回空列表即可，不视为结构异常。
    # 只有上面的 <ul class="news_ul"> 容器本身缺失，才判定为页面结构变化。
    return items


def get_total_pages(page1_html):
    """从第 1 页的分页信息推算总页数：总共 N 记录 / 每页 M。"""
    tm = PAGING_TOTAL_RE.search(page1_html)
    pm = PAGING_PER_RE.search(page1_html)
    if tm and pm:
        try:
            total = int(tm.group(1))
            per = int(pm.group(1)) or 20
            return max(1, (total + per - 1) // per)
        except ValueError:
            return None
    return None


def page_url(base_url, n):
    """构造第 n 页 URL。第 1 页为 base_url（list.htm），第 n>=2 页为 list{n}.htm"""
    if n <= 1:
        return base_url
    if base_url.endswith("list.htm"):
        return base_url[: -len("list.htm")] + f"list{n}.htm"
    # 其他情况：在末尾追加
    sep = "" if base_url.endswith("/") else "/"
    return f"{base_url}{sep}list{n}.htm"


# ----------------------------- 抓取 -----------------------------
def fetch_html(url, cfg):
    """带重试与指数退避的页面抓取，返回解码后的 HTML 文本。"""
    last_err = None
    for attempt in range(1, cfg["max_retries"] + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": cfg["user_agent"],
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=cfg["request_timeout"]) as resp:
                data = resp.read()
                encoding = resp.headers.get_content_charset() or "utf-8"
                return data.decode(encoding, errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < cfg["max_retries"]:
                wait = cfg["retry_backoff"] * attempt
                log(f"[网络] 第 {attempt} 次抓取失败: {e}，{wait}s 后重试...", cfg)
                time.sleep(wait)
    raise NetworkError(f"{url} 在 {cfg['max_retries']} 次重试后仍无法获取: {last_err}")


def get_current_items(cfg, seen_ids, local_file=None):
    """获取当前全部公告条目（覆盖所有真实分页）。

    检测新公告的依据是「文章 ID 是否已见过」的全集比对，与条目在列表中的
    前后顺序无关——即使存在「置顶」公告导致新公告不在最前，也能正确检出。

    扫描页数由第 1 页的分页信息（总共 N 记录/每页 M）精确推算；若无法推算则
    以上限兜底。遇到末页（空列表）即停止；后续页若结构异常则停止翻页（不致命）。
    local_file 不为 None 时（测试/离线模式）只解析该本地文件，不联网。
    """
    items = []
    upper = cfg["max_pages"] if (cfg["max_pages"] and cfg["max_pages"] > 0) else 10
    for n in range(1, upper + 1):
        try:
            if local_file and n == 1:
                with open(local_file, "r", encoding="utf-8") as f:
                    page_html = f.read()
            else:
                page_html = fetch_html(page_url(cfg["base_url"], n), cfg)
        except NetworkError as e:
            log(f"[网络] 第 {n} 页抓取失败: {e}", cfg)
            break  # 后续页大概率也失败，停止本轮扫描

        # 第 1 页解析出总页数，精确限制后续扫描范围
        if n == 1:
            tp = get_total_pages(page_html)
            if tp:
                upper = min(upper, tp)

        try:
            page_items = parse_announcements(page_html, cfg["base_domain"])
        except StructureChanged as e:
            if n == 1:
                raise  # 第 1 页结构异常是致命的，交给上层统一报警
            log(f"[结构] 第 {n} 页解析异常，停止继续翻页: {e}", cfg)
            break
        if not page_items:
            break  # 到达末页
        items.extend(page_items)
        if local_file:
            break  # 离线测试只解析第 1 页
    return items


# ----------------------------- 持久化 -----------------------------
def load_seen(cfg):
    path = cfg["data_file"]
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", {})
    except (json.JSONDecodeError, OSError):
        log("[数据] 已记录文件损坏，将重新初始化", cfg)
        return {}


def save_seen(cfg, items_dict):
    data = {
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items_dict),
        "items": items_dict,
    }
    tmp = cfg["data_file"] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg["data_file"])  # 原子写入，防止写一半崩溃损坏数据


# ----------------------------- 通知 -----------------------------
def log(msg, cfg):
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(cfg["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _toast_worker(title, message):
    """在独立线程中弹出 Windows 消息框，避免阻塞监测主循环。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)  # MB_OK | MB_ICONINFORMATION
    except Exception:
        pass


def notify_desktop(title, message):
    t = threading.Thread(target=_toast_worker, args=(title, message), daemon=True)
    t.start()


def render_report(cfg, new_items):
    """生成/追加新公告 HTML 报告。"""
    rows = ""
    for it in new_items:
        rows += (
            f'      <li><a href="{it["url"]}" target="_blank">{html.escape(it["title"])}</a>'
            f' <span class="date">[{it["date"]}]</span></li>\n'
        )
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>NUAA 机电学院 新公告提醒</title>
<style>
 body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;margin:40px;color:#222}}
 h1{{font-size:20px}} .date{{color:#888;font-size:13px}}
 ul{{line-height:2}} a{{color:#c00;text-decoration:none}}
 .meta{{color:#666;font-size:12px;margin-top:20px}}
</style></head><body>
<h1>🔔 检测到 {len(new_items)} 条新公告</h1>
<ul>\n{rows}</ul>
<div class="meta">生成时间：{ts} ｜ 来源：{cfg['base_url']}</div>
</body></html>"""
    try:
        with open(cfg["report_html"], "w", encoding="utf-8") as f:
            f.write(html_doc)
    except OSError as e:
        log(f"[通知] 写 HTML 报告失败: {e}", cfg)


def notify_email(cfg, new_items):
    ec = cfg.get("email")
    if not ec:
        return
    try:
        from email.mime.text import MIMEText
        lines = "\n".join(f"{it['title']}  ({it['date']})\n{it['url']}" for it in new_items)
        msg = MIMEText(f"检测到 {len(new_items)} 条新公告：\n\n{lines}", "plain", "utf-8")
        msg["Subject"] = f"[公告监测] {len(new_items)} 条新公告"
        msg["From"] = ec["user"]
        msg["To"] = ec["to"]
        with smtplib.SMTP_SSL(ec["smtp_host"], ec["smtp_port"]) as s:
            s.login(ec["user"], ec["password"])
            s.send_message(msg)
        log("[通知] 邮件已发送", cfg)
    except Exception as e:
        log(f"[通知] 邮件发送失败: {e}", cfg)


def notify_webhook(cfg, new_items):
    wc = cfg.get("webhook")
    if not wc:
        return
    try:
        payload = json.dumps(
            {"event": "new_announcements", "count": len(new_items), "items": new_items},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            wc,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()
        log("[通知] Webhook 已触发", cfg)
    except Exception as e:
        log(f"[通知] Webhook 失败: {e}", cfg)


def notify_all(cfg, new_items):
    if not new_items:
        return
    titles = "\n".join(f"· {it['title']} ({it['date']})" for it in new_items)
    msg = f"NUAA 机电学院「研究生招生」发布 {len(new_items)} 条新公告：\n{titles}"
    if cfg.get("desktop_notify"):
        notify_desktop("🔔 新公告提醒", msg[:2000])
    render_report(cfg, new_items)
    notify_email(cfg, new_items)
    notify_webhook(cfg, new_items)
    log(f"[通知] 已对 {len(new_items)} 条新公告触发提醒", cfg)


# ----------------------------- 核心逻辑 -----------------------------
def check_once(cfg, first_run=False, local_file=None):
    seen = load_seen(cfg)
    seen_ids = set(seen.keys())
    try:
        current = get_current_items(cfg, seen_ids, local_file=local_file)
    except StructureChanged as e:
        # 结构变化：报警，但不清空已知记录（避免误判漏检）
        log(f"[告警] 页面结构可能已变化：{e}", cfg)
        notify_desktop("⚠️ 监测异常", "公告页面结构发生变化，请检查监测程序。")
        return
    except NetworkError as e:
        log(f"[告警] 网络异常，本轮跳过：{e}", cfg)
        return

    # 判断新公告的唯一依据：文章 ID（URL 中的 c11462aNNNNNN）是否从未见过。
    # 这与条目在列表中的排序、是否被「置顶」无关——即使新公告不在列表最前也能检出。
    # 发布日期仅用于展示与排序，不作为去重主键（同日多条/置顶旧日期会导致误判）。
    new_items = [it for it in current if it["id"] not in seen_ids]
    # 按发布日期倒序，便于阅读（无法解析日期的排最后）
    new_items.sort(key=lambda x: x["date"], reverse=True)

    if first_run:
        # 首次运行：仅建立基线，不通知
        for it in current:
            seen[it["id"]] = it
        save_seen(cfg, seen)
        log(f"[初始化] 已建立基线，记录 {len(current)} 条历史公告，开始监测", cfg)
        return

    if new_items:
        for it in new_items:
            seen[it["id"]] = it
        save_seen(cfg, seen)
        log(f"[发现] {len(new_items)} 条新公告：", cfg)
        for it in new_items:
            log(f"        - {it['title']}  ({it['date']})  {it['url']}", cfg)
        notify_all(cfg, new_items)
    else:
        log(f"[巡检] 无新公告（当前已知 {len(seen_ids)} 条）", cfg)


def run_persistent(cfg):
    log("=== NUAA 机电学院公告监测 启动 ===", cfg)
    log(f"目标: {cfg['base_url']}  频率: 每 {cfg['check_interval_minutes']} 分钟", cfg)
    # 首次运行建立基线
    check_once(cfg, first_run=True)
    interval = cfg["check_interval_minutes"] * 60
    try:
        while True:
            time.sleep(interval)
            check_once(cfg)
    except KeyboardInterrupt:
        log("=== 已手动停止 ===", cfg)


# ----------------------------- 命令行入口 -----------------------------
def main():
    p = argparse.ArgumentParser(description="NUAA 机电学院研究生招生公告监测")
    p.add_argument("--url", default=None, help="列表页 URL")
    p.add_argument("--once", action="store_true", help="只检查一次后退出（适合任务计划程序）")
    p.add_argument("--interval", type=int, default=None, help="监测间隔(分钟)")
    p.add_argument("--pages", type=int, default=None, help="最多扫描页数(0=自动)")
    p.add_argument("--file", default=None, help="本地 HTML 文件(离线测试/解析)")
    p.add_argument("--data-file", default=None, help="已知公告数据文件路径")
    p.add_argument("--report-html", default=None, help="HTML 报告路径")
    p.add_argument("--log-file", default=None, help="日志文件路径")
    p.add_argument("--no-desktop", action="store_true", help="关闭桌面弹窗")
    p.add_argument("--init", action="store_true", help="强制重新初始化基线(不通知)")
    p.add_argument("--test-parse", action="store_true", help="仅解析 --file 并打印条目后退出")
    args = p.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    if args.url:
        cfg["base_url"] = args.url
    if args.interval:
        cfg["check_interval_minutes"] = args.interval
    if args.pages is not None:
        cfg["max_pages"] = args.pages
    if args.data_file:
        cfg["data_file"] = args.data_file
    if args.report_html:
        cfg["report_html"] = args.report_html
    if args.log_file:
        cfg["log_file"] = args.log_file
    if args.no_desktop:
        cfg["desktop_notify"] = False

    if args.test_parse:
        if not args.file:
            print("错误: --test-parse 需要配合 --file")
            sys.exit(1)
        with open(args.file, "r", encoding="utf-8") as f:
            html_text = f.read()
        items = parse_announcements(html_text, cfg["base_domain"])
        print(f"解析到 {len(items)} 条公告:")
        for it in items:
            print(f"  [{it['date']}] {it['id']}  {it['title']}\n      {it['url']}")
        return

    if args.file and not args.once and not args.init:
        # 离线测试模式：默认单次
        args.once = True

    if args.once or args.init:
        check_once(cfg, first_run=args.init, local_file=args.file)
    else:
        run_persistent(cfg)


if __name__ == "__main__":
    main()
