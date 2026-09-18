# -*- coding: utf-8 -*-
"""邮件通知（QQ 邮箱 SMTP over SSL）。

沿用原脚本的发送方式与"退出阶段无害错误"过滤逻辑，
只是把内容组装抽成几个函数。
"""

import html
import smtplib
import ssl
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

import config
from . import core

# QQ 邮箱在会话关闭阶段偶发抛这个异常，邮件其实已经送达，不该报失败。
_HARMLESS = "(-1, b'\\x00\\x00\\x00')"


def send(subject, html_body):
    """发送一封 HTML 邮件。成功返回 True。"""
    cfg = config.EMAIL_CONFIG
    if not (cfg.get("auth_code") or "").strip():
        core.log("没有可用的 SMTP 授权码（环境变量 SMTP_AUTH_CODE 为空），跳过发信", "error")
        return False
    try:
        msg = MIMEText(html_body, "html", "utf-8")
        msg["From"] = cfg["sender"]
        msg["To"] = Header(cfg["receiver"], "utf-8")
        msg["Subject"] = Header(subject, "utf-8")

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["smtp_server"], cfg["smtp_port"],
                              context=context, timeout=config.EMAIL_TIMEOUT) as server:
            server.login(cfg["sender"], cfg["auth_code"])
            server.sendmail(cfg["sender"], [cfg["receiver"]], msg.as_string())

        core.log(f"邮件已发送：{subject}")
        return True

    except Exception as exc:                            # noqa: BLE001
        if str(exc) == _HARMLESS:
            core.log(f"邮件已发送（忽略会话退出时的无害错误）：{subject}")
            return True
        core.log(f"邮件发送失败：{type(exc).__name__}: {exc}", "error")
        return False


# --------------------------------------------------------------------------
# 邮件模板
# --------------------------------------------------------------------------

_STYLE = (
    "font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
    "font-size:14px;line-height:1.7;color:#222;"
)
_CARD = (
    "border-left:3px solid #185FA5;background:#F4F8FD;"
    "padding:10px 14px;margin:12px 0;border-radius:4px;"
)


def _esc(text):
    return html.escape(str(text or ""))


def _candidate_block(candidate, doc_path=None):
    meta = candidate.metadata or {}
    rows = [
        ("标题", candidate.title),
        ("发文字号", meta.get("发文字号", "")),
        ("生成日期", meta.get("生成日期", "")),
        ("信息索引", meta.get("信息索引", "")),
        ("命中来源", candidate.source),
    ]
    lines = "".join(
        f"<li><b>{_esc(k)}：</b>{_esc(v)}</li>" for k, v in rows if v
    )
    tail = ""
    if doc_path:
        tail = f"<p style='color:#666'>正文已保存到：<code>{_esc(doc_path)}</code></p>"
    return (
        f"<div style='{_CARD}'>"
        f"<ul style='margin:0;padding-left:18px'>{lines}</ul>"
        f"<p style='margin:10px 0 0'>"
        f"<a href='{_esc(candidate.url)}'>{_esc(candidate.url)}</a></p>"
        f"{tail}</div>"
    )


def _signature(state):
    return (
        f"<p style='color:#888;font-size:12px;margin-top:24px'>"
        f"发送时间 {datetime.now():%Y-%m-%d %H:%M:%S}　|　"
        f"累计检查 {state.get('total_checks', 0)} 轮　|　"
        f"监控年份 {config.TARGET_YEAR}</p>"
    )


def build_startup(state, candidates, errors):
    """启动通知：告诉当前状态，顺便验证邮件通道是否可用。"""
    year = config.TARGET_YEAR
    if candidates:
        head = (f"<h3 style='color:#A32D2D;margin:0 0 8px'>"
                f"已发现 {year} 年相关文件</h3>")
        blocks = "".join(_candidate_block(c) for c in candidates)
    else:
        head = (f"<h3 style='margin:0 0 8px'>尚未发布 {year} 年相关文件</h3>"
                f"<p>监控已启动，命中后会立刻通知你。</p>")
        blocks = ""

    err_block = ""
    if errors:
        items = "".join(f"<li>{_esc(e)}</li>" for e in errors)
        err_block = (f"<p style='color:#A32D2D'><b>本轮异常：</b></p>"
                     f"<ul style='color:#A32D2D'>{items}</ul>")

    return (
        f"<div style='{_STYLE}'>"
        f"<h3 style='color:#185FA5;margin:0 0 12px'>教育部招生规定监控 · 已启动</h3>"
        f"<p>每 {config.CHECK_INTERVAL // 60} 分钟检查一次教育部官方文件列表与新闻通稿，"
        f"关键词：<b>{year} 年 + {_esc(config.CORE_KEYWORD)}</b>。</p>"
        f"{head}{blocks}{err_block}"
        f"{_signature(state)}"
        f"</div>"
    )


def build_hit(state, candidate, doc_path=None):
    year = config.TARGET_YEAR
    kind_label = "文件本体" if candidate.is_file else "相关信号"
    return (
        f"<div style='{_STYLE}'>"
        f"<h3 style='color:#A32D2D;margin:0 0 4px'>"
        f"命中：{year} 年全国硕士研究生招生工作管理规定</h3>"
        f"<p style='color:#666;margin:0 0 12px'>类型：{kind_label}</p>"
        f"{_candidate_block(candidate, doc_path)}"
        f"<p>请尽快到教育部官网核对原文。</p>"
        f"{_signature(state)}"
        f"</div>"
    )


def build_failure_alert(state, errors, cycles):
    items = "".join(f"<li>{_esc(e)}</li>" for e in errors) or "<li>（无详细错误）</li>"
    return (
        f"<div style='{_STYLE}'>"
        f"<h3 style='color:#A32D2D;margin:0 0 12px'>监控异常：连续 {cycles} 轮抓取失败</h3>"
        f"<p>脚本仍在运行，但已连续 {cycles} 轮无法从任何数据源取到数据。"
        f"请检查网络、本机时间，或确认网站结构是否调整。</p>"
        f"<p><b>最近一轮错误：</b></p><ul>{items}</ul>"
        f"<p style='color:#666'>日志文件：<code>{_esc(config.LOG_FILE)}</code></p>"
        f"{_signature(state)}"
        f"</div>"
    )


# --------------------------------------------------------------------------
# 便捷发送
# --------------------------------------------------------------------------

def notify_startup(state, candidates, errors):
    """启动通知。去重登记由调用方（monitor）统一负责，这里只负责发信。"""
    return send(f"【已启动】教育部{config.TARGET_YEAR}年招生规定监控",
                build_startup(state, candidates, errors))


def notify_hit(state, candidate, doc_path=None):
    ok = send(f"【命中】教育部{config.TARGET_YEAR}年硕士研究生招生工作管理规定",
              build_hit(state, candidate, doc_path))
    if ok:
        from . import state as state_mod
        state = state_mod.mark_notified(state, candidate.url)
    return ok, state


def notify_failure(state, errors, cycles):
    return send(f"【监控异常】连续 {cycles} 轮抓取失败", build_failure_alert(state, errors, cycles))
