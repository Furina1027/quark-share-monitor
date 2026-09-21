#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京航空航天大学 招生公告监测程序（多源合并）

监控源:
  1. 机电学院「研究生招生」  https://cmee.nuaa.edu.cn/11462/list.htm
  2. 研究生院「硕士招生」    https://www.graduate.nuaa.edu.cn/sszs/list.htm

两个源一次运行、合并成一封邮件；每个源的基线各自维护，互不干扰。

特性:
  * 纯 Python 标准库实现，无需 pip install 任何第三方包
  * 通过文章 URL 中的文章 ID (cNNNNaNNNNNN) 作为唯一标识判断新公告
    —— 与条目在列表中的位置、是否被「置顶」完全无关
  * 分页自动扫描：逐页抓取，直到遇到空页为止（保证不漏检）
  * 新增监测源时自动「静默建基线」，不会把该源的历史公告当新公告轰炸
  * 网络异常自动重试 + 指数退避；单个源失败不影响其它源
  * 页面结构变化检测（解析到 0 条时报警，不清空已知记录）
  * 多种通知方式：桌面弹窗(Windows)、本地日志、HTML 报告、可选邮件/Webhook
  * 支持作为常驻守护进程运行，也可配合 GitHub Actions / 任务计划程序 --once 运行
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
import urllib.parse
import urllib.request

# ----------------------------- 配置 -----------------------------
DEFAULT_CONFIG = {
    # 监测源列表。以后要加源，只在这里追加一项即可（会自动静默建基线）。
    "sources": [
        {
            "key": "cmee",
            "name": "机电学院·研究生招生",
            "url": "https://cmee.nuaa.edu.cn/11462/list.htm",
            "domain": "https://cmee.nuaa.edu.cn",
        },
        {
            "key": "grad",
            "name": "研究生院·硕士招生",
            "url": "https://www.graduate.nuaa.edu.cn/sszs/list.htm",
            "domain": "https://www.graduate.nuaa.edu.cn",
        },
    ],
    "check_interval_minutes": 30,   # 监测频率（分钟）
    "max_pages": 0,                 # 每个源最多扫描页数；0 = 自动（按分页信息，最多 10 页）
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
# 文章 ID：c<栏目号>a<文章号>，如 c11462a409933 / c13487a410574
ARTICLE_ID_RE = re.compile(r"(c\d+a\d+)")
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
        idm = ARTICLE_ID_RE.search(href)
        aid = idm.group(1) if idm else href  # 文章 ID 优先，否则用完整 URL
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


def get_source_items(cfg, src, local_file=None):
    """抓取单个监测源的全部公告条目。

    返回 (items, ok)：
      ok=True  至少成功取到第 1 页（items 可能为空 = 该栏目当前无公告）
      ok=False 第 1 页网络失败，本轮应跳过该源（不清空基线）

    检测新公告的依据是「文章 ID 是否已见过」的全集比对，与条目在列表中的
    前后顺序无关——即使存在「置顶」公告导致新公告不在最前，也能正确检出。
    同一轮内重复出现的条目（置顶项跨页重复）按 ID 保序去重。

    local_file 不为 None 时（测试/离线模式）只解析该本地文件，不联网。
    """
    items = []
    seen_in_round = set()
    upper = cfg["max_pages"] if (cfg["max_pages"] and cfg["max_pages"] > 0) else 10
    for n in range(1, upper + 1):
        try:
            if local_file and n == 1:
                with open(local_file, "r", encoding="utf-8") as f:
                    page_html = f.read()
            else:
                page_html = fetch_html(page_url(src["url"], n), cfg)
        except NetworkError as e:
            log(f"[网络] 「{src['name']}」第 {n} 页抓取失败: {e}", cfg)
            if n == 1:
                return [], False   # 首页都拿不到，本轮跳过这个源
            break                  # 后续页大概率也失败，停止该源翻页

        # 第 1 页解析出总页数，精确限制后续扫描范围
        if n == 1:
            tp = get_total_pages(page_html)
            if tp:
                upper = min(upper, tp)

        try:
            page_items = parse_announcements(page_html, src["domain"])
        except StructureChanged as e:
            if n == 1:
                raise  # 第 1 页结构异常是致命的，交给上层统一报警
            # 后续页拿不到列表，通常只是「翻过了最后一页」（该页不存在），属正常停止
            log(f"[翻页] 「{src['name']}」第 {n} 页无公告列表，视为已到末页，停止翻页", cfg)
            break
        if not page_items:
            break  # 到达末页
        for it in page_items:
            if it["id"] in seen_in_round:
                continue  # 置顶条目跨页重复出现，去重
            seen_in_round.add(it["id"])
            it["source"] = src["key"]
            it["source_name"] = src["name"]
            items.append(it)
        if local_file:
            break  # 离线测试只解析第 1 页
    return items, True


# ----------------------------- 持久化 -----------------------------
def _infer_initialized(items, sources):
    """旧版状态文件没有 initialized_sources 字段时，从条目的 URL 反推哪些源已建过基线。"""
    done = set()
    for src in sources:
        host = src["domain"].split("//")[-1]
        for it in items.values():
            if host in (it.get("url") or ""):
                done.add(src["key"])
                break
    return done


def load_state(cfg):
    """读取状态文件，返回 {'items': {...}, 'initialized': set()}。

    兼容旧格式（只有 items，没有 initialized_sources）：
    此时按条目 URL 推断哪些源已经建过基线，避免升级后把老源的公告当新公告。
    """
    path = cfg["data_file"]
    if not os.path.exists(path):
        return {"items": {}, "initialized": set()}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        log("[数据] 已记录文件损坏，将重新初始化", cfg)
        return {"items": {}, "initialized": set()}
    items = data.get("items", {})
    if "initialized_sources" in data:
        initialized = set(data["initialized_sources"])
    else:
        initialized = _infer_initialized(items, cfg["sources"])
        if initialized:
            log(f"[数据] 旧格式状态文件，推断已建基线的源: {sorted(initialized)}", cfg)
    return {"items": items, "initialized": initialized}


def save_state(cfg, items_dict, initialized):
    data = {
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(items_dict),
        "initialized_sources": sorted(initialized),
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


def group_by_source(new_items, cfg=None):
    """把新公告按来源分组：组的顺序跟随配置里的源顺序，组内按发布日期倒序。"""
    by_key, names = {}, {}
    for it in new_items:
        key = it.get("source") or "other"
        by_key.setdefault(key, []).append(it)
        names[key] = it.get("source_name") or key
    order = [s["key"] for s in (cfg or {}).get("sources", [])]
    keys = [k for k in order if k in by_key] + [k for k in by_key if k not in order]
    return [(names[k], sorted(by_key[k], key=lambda x: x["date"], reverse=True)) for k in keys]


def render_report(cfg, new_items):
    """生成/追加新公告 HTML 报告（按来源分组）。"""
    blocks = ""
    for name, items in group_by_source(new_items, cfg):
        rows = ""
        for it in items:
            rows += (
                f'      <li><a href="{it["url"]}" target="_blank">{html.escape(it["title"])}</a>'
                f' <span class="date">[{it["date"]}]</span></li>\n'
            )
        blocks += f'<h2>{html.escape(name)} <span class="cnt">{len(items)} 条</span></h2>\n<ul>\n{rows}</ul>\n'
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>南航招生公告 新公告提醒</title>
<style>
 body{{font-family:-apple-system,"Microsoft YaHei",sans-serif;margin:40px;color:#222}}
 h1{{font-size:20px}} h2{{font-size:16px;margin:26px 0 6px}}
 .cnt{{color:#888;font-size:13px;font-weight:400}}
 .date{{color:#888;font-size:13px}}
 ul{{line-height:2;margin:0 0 10px}} a{{color:#c00;text-decoration:none}}
 .meta{{color:#666;font-size:12px;margin-top:24px}}
</style></head><body>
<h1>🔔 检测到 {len(new_items)} 条新公告</h1>
{blocks}
<div class="meta">生成时间：{ts} ｜ 来源：{' / '.join(s['url'] for s in cfg['sources'])}</div>
</body></html>"""
    try:
        with open(cfg["report_html"], "w", encoding="utf-8") as f:
            f.write(html_doc)
    except OSError as e:
        log(f"[通知] 写 HTML 报告失败: {e}", cfg)


def _plain_items_text(items):
    return "\n".join(f"· {it['title']}  ({it['date']})\n  {it['url']}" for it in items)


def notify_email(cfg, new_items):
    ec = cfg.get("email")
    if not ec:
        return
    try:
        from email.mime.text import MIMEText
        parts = []
        for name, items in group_by_source(new_items, cfg):
            parts.append(f"【{name}】{len(items)} 条\n{_plain_items_text(items)}")
        body = f"检测到 {len(new_items)} 条新公告：\n\n" + "\n\n".join(parts)
        msg = MIMEText(body, "plain", "utf-8")
        nsrc = len(group_by_source(new_items, cfg))
        src_tag = f"（{nsrc} 个栏目）" if nsrc > 1 else ""
        msg["Subject"] = f"[南航公告监测] {len(new_items)} 条新公告{src_tag}"
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
    parts = []
    for name, items in group_by_source(new_items, cfg):
        parts.append(f"【{name}】{len(items)} 条\n" +
                     "\n".join(f"· {it['title']} ({it['date']})" for it in items))
    msg = f"南航招生公告更新，共 {len(new_items)} 条：\n\n" + "\n\n".join(parts)
    if cfg.get("desktop_notify"):
        notify_desktop("🔔 新公告提醒", msg[:2000])
    render_report(cfg, new_items)
    notify_email(cfg, new_items)
    notify_webhook(cfg, new_items)
    log(f"[通知] 已对 {len(new_items)} 条新公告触发提醒", cfg)


# ----------------------------- 核心逻辑 -----------------------------
def check_once(cfg, first_run=False, local_file=None):
    state = load_state(cfg)
    seen = state["items"]
    initialized = state["initialized"]

    # 逐源抓取；单个源失败只跳过它，不影响其它源
    results = []
    for src in cfg["sources"]:
        try:
            cur, ok = get_source_items(cfg, src, local_file=local_file)
        except StructureChanged as e:
            log(f"[告警] 「{src['name']}」页面结构可能已变化：{e}", cfg)
            notify_desktop("⚠️ 监测异常", f"「{src['name']}」页面结构发生变化，请检查监测程序。")
            results.append((src, [], False))
            continue
        results.append((src, cur, ok))

    initialized_now = []   # 本轮新建立基线的源
    new_items = []
    scanned = []

    for src, cur, ok in results:
        if not ok:
            continue  # 该源本轮网络失败，跳过
        scanned.append(f"{src['name']} {len(cur)} 条")
        if first_run or src["key"] not in initialized:
            # 首次运行 / 新增源：只建立基线，不通知
            for it in cur:
                seen[it["id"]] = it
            initialized.add(src["key"])
            initialized_now.append((src, len(cur)))
            continue
        # 判断新公告的唯一依据：文章 ID 是否从未见过。
        # 与条目排序、是否置顶无关——即使新公告不在列表最前也能检出。
        # 发布日期仅用于展示与排序，不作为去重主键。
        for it in cur:
            if it["id"] not in seen:
                new_items.append(it)

    if initialized_now:
        for src, cnt in initialized_now:
            log(f"[初始化] 「{src['name']}」已建立基线，记录 {cnt} 条历史公告", cfg)
        save_state(cfg, seen, initialized)

    if not scanned:
        log("[告警] 所有监测源本轮均抓取失败，跳过本轮（基线未改动）", cfg)
        return

    if first_run:
        save_state(cfg, seen, initialized)
        log(f"[初始化] 完成，共记录 {len(seen)} 条历史公告，开始监测。扫描情况: {'；'.join(scanned)}", cfg)
        return

    if new_items:
        for it in new_items:
            seen[it["id"]] = it
        save_state(cfg, seen, initialized)
        log(f"[发现] {len(new_items)} 条新公告：", cfg)
        for name, items in group_by_source(new_items, cfg):
            log(f"        【{name}】{len(items)} 条", cfg)
            for it in items:
                log(f"          - {it['title']}  ({it['date']})  {it['url']}", cfg)
        notify_all(cfg, new_items)
    else:
        log(f"[巡检] 无新公告（已记录 {len(seen)} 条）；扫描: {'；'.join(scanned)}", cfg)


def run_persistent(cfg):
    log("=== 南航招生公告监测 启动（多源）===", cfg)
    for s in cfg["sources"]:
        log(f"  源 {s['key']}: {s['name']}  {s['url']}", cfg)
    log(f"频率: 每 {cfg['check_interval_minutes']} 分钟", cfg)
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
    p = argparse.ArgumentParser(description="南京航空航天大学 招生公告监测（多源）")
    p.add_argument("--url", default=None, help="覆盖第一个源（机电学院）的列表页 URL")
    p.add_argument("--only", default=None, help="只监测指定源，逗号分隔（如 --only grad）")
    p.add_argument("--once", action="store_true", help="只检查一次后退出（适合任务计划程序）")
    p.add_argument("--interval", type=int, default=None, help="监测间隔(分钟)")
    p.add_argument("--pages", type=int, default=None, help="每个源最多扫描页数(0=自动)")
    p.add_argument("--file", default=None, help="本地 HTML 文件(离线测试/解析)")
    p.add_argument("--data-file", default=None, help="已知公告数据文件路径")
    p.add_argument("--report-html", default=None, help="HTML 报告路径")
    p.add_argument("--log-file", default=None, help="日志文件路径")
    p.add_argument("--no-desktop", action="store_true", help="关闭桌面弹窗")
    p.add_argument("--init", action="store_true", help="强制重新初始化基线(不通知)")
    p.add_argument("--test-parse", action="store_true", help="仅解析 --file 并打印条目后退出")
    args = p.parse_args()

    cfg = dict(DEFAULT_CONFIG)
    cfg["sources"] = [dict(s) for s in DEFAULT_CONFIG["sources"]]
    if args.url:
        cfg["sources"][0]["url"] = args.url
        cfg["sources"][0]["domain"] = "https://" + urllib.parse.urlparse(args.url).netloc
    if args.only:
        keys = {k.strip() for k in args.only.split(",") if k.strip()}
        cfg["sources"] = [s for s in cfg["sources"] if s["key"] in keys]
        if not cfg["sources"]:
            print("错误: --only 没有匹配到任何监测源")
            sys.exit(1)
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
        src = cfg["sources"][0]
        items = parse_announcements(html_text, src["domain"])
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
