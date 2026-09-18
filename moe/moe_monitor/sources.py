# -*- coding: utf-8 -*-
"""数据源适配器：从各个列表页取出 (标题, 链接)，需要时再跟进正文页。

所有源都只依赖"+ 详情页 URL 的日期数字模式"，不依赖任何绝对 XPath。
页面改版最多让某个源失效，不会让整个脚本哑掉 —— 而且失效会被记进日志、
累计到阈值就发告警邮件。
"""

import os
import re
from datetime import datetime

import config
from . import core


class Candidate:
    """一条候选记录。"""

    __slots__ = ("title", "url", "source", "kind", "metadata", "body", "doc_path")

    def __init__(self, title, url, source, kind):
        self.title = title
        self.url = url
        self.source = source
        self.kind = kind          # "FILE" | "SIGNAL"
        self.metadata = {}
        self.body = ""
        self.doc_path = None

    @property
    def is_file(self):
        return self.kind == "FILE"

    def __repr__(self):
        return f"<Candidate {self.kind} {self.title[:28]}>"


def parse_list(html, base_url, pattern):
    """把列表页解析成 [(title, url)]，按出现顺序。"""
    soup = core.soup_of(html)
    rx = re.compile(pattern)
    items, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not rx.search(href):
            continue
        title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
        if not title or len(title) < 6:
            continue
        url = core.make_absolute(href, base_url)
        if url in seen:
            continue
        seen.add(url)
        items.append((title, url))

    return items


def check_source(source):
    """检查单个源。返回 (候选列表, 错误信息或 None)。"""
    name, url = source["name"], source["url"]
    try:
        html = core.fetch(url)
    except Exception as exc:                            # noqa: BLE001
        return [], f"{name} 抓取失败：{exc}"

    try:
        items = parse_list(html, url, source["detail_pattern"])
    except Exception as exc:                            # noqa: BLE001
        return [], f"{name} 解析失败：{exc}"

    if not items:
        # 抓到页面但一条都解析不出来 = 页面结构变了，必须当成错误上报，
        # 不能当成"没有新文件"。
        return [], f"{name} 未解析到任何条目（页面结构可能已改版）"

    hits = []
    for title, href in items:
        kind = core.classify(title)
        if kind:
            hits.append(Candidate(title, href, name, kind))

    core.log(f"  [{name}] 列表 {len(items)} 条，命中 {len(hits)} 条")
    return hits, None


def enrich(candidate):
    """跟进正文页，补齐元数据与正文。失败不影响主流程。"""
    try:
        html = core.fetch(candidate.url)
    except Exception as exc:                            # noqa: BLE001
        core.log(f"  正文抓取失败（不影响通知）：{candidate.url} -> {exc}", "warning")
        return candidate

    soup = core.soup_of(html)
    candidate.metadata = core.extract_metadata(soup)
    candidate.body = core.extract_body(soup)

    page_title = core.clean_page_title(soup)
    if page_title and len(page_title) > len(candidate.title):
        candidate.title = page_title
        # 正文页标题可能把分类判得更准（比如列表页标题被截断）
        if core.classify(page_title) == "FILE":
            candidate.kind = "FILE"

    return candidate


def save_document(candidate):
    """把正文落盘，返回文件路径。"""
    os.makedirs(config.DOCUMENT_DIR, exist_ok=True)
    year = config.TARGET_YEAR
    for m in re.finditer(r"(20\d{2})", candidate.title):
        year = m.group(1)
        break

    meta = candidate.metadata
    header = [
        f"标题: {candidate.title}",
        f"发文字号: {meta.get('发文字号', '(未提供)')}",
        f"生成日期: {meta.get('生成日期', '(未提供)')}",
        f"信息索引: {meta.get('信息索引', '(未提供)')}",
        f"发文机构: {meta.get('发文机构', '(未提供)')}",
        f"来源URL: {candidate.url}",
        f"抓取时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "=" * 60,
        "",
    ]
    safe = re.sub(r"[\\/:*?\"<>|]", "_", candidate.title)[:60]
    path = os.path.join(config.DOCUMENT_DIR, f"{year}_{safe}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header))
        f.write(candidate.body or "(未取到正文，请打开来源URL查看)")
    return path


def run_all_sources():
    """跑一遍所有源，返回 (候选列表, 错误列表)。"""
    candidates, errors = [], []
    for source in config.SOURCES:
        hits, err = check_source(source)
        candidates.extend(hits)
        if err:
            errors.append(err)

    # 同一个 URL 命中多次时按"文件本体优先"合并
    merged = {}
    for c in candidates:
        key = c.url
        if key not in merged or (c.is_file and not merged[key].is_file):
            merged[key] = c

    return list(merged.values()), errors
