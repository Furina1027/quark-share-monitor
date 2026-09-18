# -*- coding: utf-8 -*-
"""运行状态：记录已经通知过哪些链接，防止重复发信。"""

import json
import os
from datetime import datetime

import config
from . import core


def _empty():
    return {"notified_urls": [], "last_check": None, "consecutive_failures": 0,
            "total_checks": 0, "first_run_at": None}


def load():
    if not os.path.exists(config.STATE_FILE):
        return _empty()
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        base = _empty()
        base.update(data or {})
        return base
    except Exception as exc:                            # noqa: BLE001
        core.log(f"状态文件损坏，已重置：{exc}", "warning")
        return _empty()


def save(state):
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config.STATE_FILE)


def touch(state, checked_at=None):
    state["last_check"] = (checked_at or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    state["total_checks"] = int(state.get("total_checks") or 0) + 1
    if not state.get("first_run_at"):
        state["first_run_at"] = state["last_check"]
    return state


def is_notified(state, url):
    return url in set(state.get("notified_urls") or [])


def mark_notified(state, url):
    urls = set(state.get("notified_urls") or [])
    urls.add(url)
    state["notified_urls"] = sorted(urls)
    return state
