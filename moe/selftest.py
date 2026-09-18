# -*- coding: utf-8 -*-
"""自测：验证"如果 2027 年文件出现，脚本一定能命中"。

跑法：python selftest.py

第一部分是纯逻辑测试（离线，快）。
第二部分拿 2026 年那份真实存在的文件当靶子，
把年份临时改成 2026 走一遍完整链路，确认能抓到、能识别、能抽取正文。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

import config
from moe_monitor import core

PASS, FAIL = "[PASS]", "[FAIL]"
_failures = []


def check(label, got, expected):
    ok = got == expected
    print(f"  {PASS if ok else FAIL} {label}")
    if not ok:
        print(f"         期望 {expected!r}，实际 {got!r}")
        _failures.append(label)


print("=" * 72)
print("第一部分：匹配逻辑（离线）")
print("=" * 72)

print("\n[归一化] 全角数字 / 空白 / 尾随空格")
check("正常标题", core.normalize("教育部关于印发《2027年全国硕士研究生招生工作管理规定》的通知"),
      "教育部关于印发《2027年全国硕士研究生招生工作管理规定》的通知")
check("全角数字 ２０２７", core.normalize("２０２７年招生"), "2027年招生")
check("中间有空格", core.normalize("2027 年 招生\n工作"), "2027年招生工作")
check("全角空格", core.normalize("2027\u3000年"), "2027年")

print("\n[分类] TARGET_YEAR = 2027")
Y = "2027"
check("文件本体（带《》和通知后缀）",
      core.classify("教育部关于印发《2027年全国硕士研究生招生工作管理规定》的通知", year=Y), "FILE")
check("文件本体（带尾随空格）",
      core.classify("教育部关于印发《2027年全国硕士研究生招生工作管理规定》的通知  ", year=Y), "FILE")
check("文件本体（全角数字）",
      core.classify("教育部关于印发《２０２７年全国硕士研究生招生工作管理规定》的通知", year=Y), "FILE")
check("文件本体（无书名号）",
      core.classify("2027年全国硕士研究生招生工作管理规定", year=Y), "FILE")
check("部署新闻 -> SIGNAL",
      core.classify("教育部部署2027年全国硕士研究生考试招生工作", year=Y), "SIGNAL")
check("部署新闻（表述变体）-> SIGNAL",
      core.classify("教育部部署做好2027年全国硕士研究生招生工作", year=Y), "SIGNAL")

print("\n[分类] 必须排除的干扰项（这些命中就是误报）")
check("旧年份 2026 的文件",
      core.classify("教育部关于印发《2026年全国硕士研究生招生工作管理规定》的通知", year=Y), None)
check("成人高校招生（非硕士研究生）",
      core.classify("教育部办公厅关于做好2027年全国成人高校招生工作的通知", year=Y), None)
check("成人高校招生（2026）",
      core.classify("教育部办公厅关于做好2026年全国成人高校招生工作的通知", year=Y), None)
check("其他 2027 年文件",
      core.classify("教育部关于公布第三届全国大学生职业规划大赛2027年获奖名单的通知", year=Y), None)
check("空标题", core.classify("", year=Y), None)
check("纯导航文字", core.classify("微言教育", year=Y), None)

print()
print("=" * 72)
print("第二部分：完整链路（联网，用 2026 年真实文件当靶子）")
print("=" * 72)

TARGET_URL = "http://www.moe.gov.cn/srcsite/A15/moe_778/s3261/202509/t20250918_1413836.html"
TARGET_TITLE = "教育部关于印发《2026年全国硕士研究生招生工作管理规定》的通知"

print(f"\n[分类] 把年份设为 2026，判断该文件能否被识别")
check("识别为文件本体", core.classify(TARGET_TITLE, year="2026"), "FILE")

print(f"\n[抓取] {TARGET_URL}")
try:
    html = core.fetch(TARGET_URL)
    soup = core.soup_of(html)
    print(f"  {PASS} 抓取成功，HTML {len(html)} 字符")

    meta = core.extract_metadata(soup)
    print(f"  元数据：{meta}")
    check("发文字号", meta.get("发文字号"), "教学〔2025〕2号")
    check("生成日期", meta.get("生成日期"), "2025-09-16")
    check("信息索引", bool(re.match(r"360A15-07-2025-\d{4}-\d", meta.get("信息索引", ""))), True)

    body = core.extract_body(soup)
    print(f"  正文 {len(body)} 字符")
    check("正文含『第一章 总则』", "第一章" in body and "总则" in body, True)
    check("正文含『第十一章』", "信息公开" in body, True)
    check("正文含发文字号", "教学〔2025〕2号" in body, True)
    check("正文未混入整页导航（长度合理）", 8000 < len(body) < 60000, True)

    pt = core.clean_page_title(soup)
    check("页面标题清洗", pt, TARGET_TITLE)
except Exception as exc:                                # noqa: BLE001
    print(f"  {FAIL} 抓取/解析异常：{type(exc).__name__}: {exc}")
    _failures.append("联网链路")

print()
print("=" * 72)
print("第三部分：列表页解析（合成 HTML，模拟教育部真实列表结构）")
print("=" * 72)

FAKE_LIST = """<html><body><div class="list">
<a href="./202609/t20260916_1450989.html" title="教育部办公厅 中央网信办秘书局关于开展首个全国校园网络文明月活动的通知">a</a>
<a href="./202609/t20260916_1451040.html" title="教育部办公厅关于开展2026年“基础教育精品课”遴选工作的通知">b</a>
<a href="./202509/t20250918_1413836.html" title="教育部关于印发《2026年全国硕士研究生招生工作管理规定》的通知">c</a>
<a href="/jyb_sy/sy_wb/201301/t20130129_147290.html" title="微言教育">d</a>
</div></body></html>"""

from moe_monitor import notify, sources, state as state_mod

items = sources.parse_list(FAKE_LIST,
                           "http://www.moe.gov.cn/srcsite/A15/moe_778/s3261/",
                           r"/t\d{8}_\d+\.html")
print(f"  解析出 {len(items)} 条（导航链接『微言教育』应被过滤）")
check("条目数（排除导航短标题）", len(items), 3)
check("URL 已补全为绝对路径",
      items[2][1], "http://www.moe.gov.cn/srcsite/A15/moe_778/s3261/202509/t20250918_1413836.html")
hits = [t for t, _ in items if core.classify(t, year="2026")]
check("其中恰好命中 1 条", len(hits), 1)
check("命中目标文件", hits[0] if hits else None, TARGET_TITLE)

print()
print("=" * 72)
print("第四部分：落盘与邮件生成（端到端）")
print("=" * 72)

cand = sources.Candidate(TARGET_TITLE, TARGET_URL, "教育部文件（官方文件列表）", "FILE")
sources.enrich(cand)
check("补全后的标题", cand.title, TARGET_TITLE)
check("识别为文件本体", cand.is_file, True)

doc_path = sources.save_document(cand)
print(f"  正文已落盘：{doc_path}")
check("正文文件存在", os.path.exists(doc_path), True)
saved = open(doc_path, encoding="utf-8").read()
check("落盘正文含发文字号", "教学〔2025〕2号" in saved, True)
check("落盘正文含关键条款", "自主确定并公布报考本单位临床医学" in saved, True)

st = {"total_checks": 1, "first_run_at": "2026-09-17 21:00:00"}
mail = notify.build_hit(st, cand, doc_path)
check("邮件含标题", TARGET_TITLE in mail, True)
check("邮件含发文字号", "教学〔2025〕2号" in mail, True)
check("邮件含原文链接", TARGET_URL in mail, True)
check("邮件含落盘路径", "documents" in mail, True)

startup = notify.build_startup(st, [cand], ["模拟的源异常"])
check("启动邮件含命中提示", "已发现" in startup, True)
check("启动邮件含异常提示", "模拟的源异常" in startup, True)

alert = notify.build_failure_alert(st, ["教育部文件 抓取失败：超时"], 3)
check("告警邮件含连续失败轮数", "连续 3 轮" in alert, True)

print()
print("=" * 72)
if _failures:
    print(f"结果：{len(_failures)} 项未通过 -> {_failures}")
    sys.exit(1)
print("结果：全部通过。2027 年文件一旦发布，脚本可以命中。")
sys.exit(0)
