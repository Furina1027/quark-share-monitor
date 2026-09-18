# -*- coding: utf-8 -*-
"""基础设施：日志、HTTP 会话、文本归一化、正文与元数据抽取。

这里没有任何业务判断，只提供"拿到干净的网页 + 干净的文本"的能力。
"""

import logging
import logging.handlers
import os
import re
import sys
import time
import unicodedata

import requests
from bs4 import BeautifulSoup

import config

_logger = None


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

def get_logger(name="moe"):
    """返回同时输出到控制台和轮转文件的 logger。"""
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs(config.LOG_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # 日志落盘：这是原版最要命的缺口——跑挂了没人知道。
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    _logger = logger
    return logger


def log(msg, level="info"):
    lg = get_logger()
    getattr(lg, level)(msg)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_session = None


def get_session():
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        _session = s
    return _session


def _decode(response):
    """优先信任页面自己声明的 charset，其次 UTF-8，最后再猜。"""
    raw = response.content
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig", "replace")
    head = raw[:4096]
    m = re.search(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", head, re.I)
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    if enc.lower() in ("gb2312", "gbk"):
        enc = "gb18030"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def fetch(url, timeout=None, retries=None):
    """抓一个页面，失败自动重试并退避。返回 HTML 文本；彻底失败抛异常。"""
    timeout = timeout or config.REQUEST_TIMEOUT
    retries = config.REQUEST_RETRIES if retries is None else retries
    last_error = None

    for attempt in range(retries + 1):
        try:
            resp = get_session().get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code >= 400:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            return _decode(resp)
        except Exception as exc:                       # noqa: BLE001
            last_error = exc
            if attempt < retries:
                wait = config.RETRY_BACKOFF * (2 ** attempt)
                log(f"  请求失败（第 {attempt + 1}/{retries + 1} 次）：{url} -> "
                    f"{type(exc).__name__}: {exc}；{wait}s 后重试", "warning")
                time.sleep(wait)

    raise RuntimeError(f"抓取失败：{url}（{type(last_error).__name__}: {last_error}）")


def soup_of(html):
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:                                   # noqa: BLE001
        return BeautifulSoup(html, "html.parser")


def make_absolute(href, base_url):
    from urllib.parse import urljoin
    return urljoin(base_url, href)


# --------------------------------------------------------------------------
# 文本归一化与匹配
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def normalize(text):
    """把标题压成"可比对"的形式。

    做三件事：全角转半角（NFKC）、去掉所有空白、统一大小写。
    这样「教育部关于印发《２０２７年…》的通知 」（带尾随空格、全角数字）
    和配置里的关键词就能对上了 —— 原版用 == 全等比较，空格一多就永远匹配不上。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", "")
    text = _WS.sub("", text)
    return text


def contains_all(haystack, needles):
    n = normalize(haystack)
    return all(normalize(x) in n for x in needles)


def classify(title, year=None, core=None, signal=None):
    """判断一条标题属于哪一类。

    返回 "FILE"（文件本体）/ "SIGNAL"（部署新闻等强相关信号）/ None。
    """
    year = year or config.TARGET_YEAR
    core = core or config.CORE_KEYWORD
    signal = signal or config.SIGNAL_KEYWORDS

    n = normalize(title)
    if not n:
        return None
    if year not in n:
        return None
    if normalize(core) in n:
        return "FILE"
    if all(normalize(k) in n for k in signal):
        return "SIGNAL"
    return None


# --------------------------------------------------------------------------
# 正文与元数据抽取
# --------------------------------------------------------------------------

_META_LABELS = ("信息名称", "信息索引", "生成日期", "发文机构", "发文字号", "信息类别")
_CONTENT_HINT = re.compile(r"(content|TRS_Editor|article|detail|zhengwen|xxgk)", re.I)


def extract_metadata(soup):
    """从公开文件页抽取结构化元数据。"""
    flat = _WS.sub(" ", soup.get_text(" ", strip=True))
    meta = {}
    for label in _META_LABELS:
        m = re.search(label + r"\s*[：:]\s*([^：:]{1,60}?)(?=\s*(?:" +
                      "|".join(_META_LABELS) + r")\s*[：:]|$)", flat)
        if m:
            meta[label] = m.group(1).strip()
    return meta


def extract_body(soup):
    """抽取正文。

    不写死 CSS 路径 —— 在所有"看起来像内容容器"的节点里，
    选文本量最大的那个。这样页面小改版不会让抽取失效。
    """
    candidates = []
    for tag in soup.find_all(["div", "article", "section"]):
        ident = " ".join(filter(None, [tag.get("id"), " ".join(tag.get("class") or [])]))
        if ident and _CONTENT_HINT.search(ident):
            text = tag.get_text("\n", strip=True)
            if len(text) > 200:
                candidates.append(text)

    if not candidates:
        return soup.get_text("\n", strip=True)

    best = max(candidates, key=len)
    lines = [ln.strip() for ln in best.splitlines()]
    # 去掉连续空行
    out, blank = [], False
    for ln in lines:
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


def clean_page_title(soup):
    if not soup.title:
        return ""
    t = soup.title.get_text(strip=True)
    return re.sub(r"\s*[-_|]\s*中华人民共和国教育部.*$", "", t).strip()
