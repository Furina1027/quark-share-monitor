# -*- coding: utf-8 -*-
"""
夸克网盘分享更新监控（定向监控）
================================

监控分享 https://pan.quark.cn/s/a5643d3d2ffa 里指定目录的更新，默认每小时一次。
发现新增 / 删除 / 修改 / 改名时，打印报告 + 写日志 + 发 QQ 邮件。

监控范围（CONFIG['watch_paths']，已按你的要求配置）
    专业课：只盯两个「机械」
        渠道一  27考研课程/2027 Svip专业课/2027 Svip专业课/13.2027 机械
        渠道二  27考研课程/2027 Svip专业课（渠道二 不加密）/2027 专业课/2027考研SVIP专业/06.机械【不加密】
    公共课：全监控
        27考研课程/2027 Svip公共课
        27考研PDF/{00.27考研冲刺PDF汇总, 01.27数学PDF, 02.27英语PDF, 03.27政治PDF}

原理
----
分享页是前端渲染，数据来自接口：
    POST /1/clouddrive/share/sharepage/token   换 stoken
    GET  /1/clouddrive/share/sharepage/detail  列目录（pdir_fid 指定目录，_size 上限 100）
注意：夸克目录的 updated_at 只反映「该目录直接子项的增删」，不会随深层文件变化向上传播，
所以无法靠时间戳剪枝，只能对监控范围内的目录做完整遍历（用多线程并发压缩耗时）。

用法
----
    python quark_share_monitor.py              # 常驻，每 60 分钟扫一次
    python quark_share_monitor.py --once       # 只扫一次（给 Windows 计划任务用）
    python quark_share_monitor.py --reset      # 删快照，下次重建基线
    python quark_share_monitor.py --test-email # 发一封测试邮件
    python quark_share_monitor.py --list-watch # 只看监控目录能否解析到

Cookie
------
优先级：环境变量 `QUARK_COOKIE` > 脚本所在目录下的 cookie 文件（内容形如 "a=1; b=2"）。
本地跑就改那个文件；放到 GitHub Actions / 云函数上就把 cookie 写进环境变量，不用动代码。

可选环境变量
------------
    QUARK_COOKIE         cookie 字符串（设了就优先用它）
    QUARK_MONITOR_DIR    快照/日志的输出目录（默认 = 脚本所在目录）

产物（都在脚本同目录）
    quark_share_snapshot.json.gz  快照（对比用，别删）
    quark_share_monitor.log       运行日志
    quark_share_updates.log       只追加「有更新」的报告

无窗口运行
----------
带窗口：双击 run_monitor.bat
静默无窗口：用 pythonw.exe 启动（脚本会把所有输出自动转写进日志文件）
    pythonw.exe quark_share_monitor.py
"""

import argparse
import gzip
import json
import os
import re
import smtplib
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText

import requests

# ============================== 配置区 ==============================

CONFIG = {
    # 分享链接（带 #/list/share 无所谓，脚本只取 /s/ 后面的 id）
    'share_url': 'https://pan.quark.cn/s/a5643d3d2ffa#/list/share',
    'passcode': '',                 # 提取码，没有就留空

    # 要监控的目录（路径是从分享根目录开始的完整路径，改这里就能换监控目标）
    'watch_paths': [
        # —— 专业课：只监控两个「机械」 ——
        '/2027考研资料（持续更新）/27考研课程/2027 Svip专业课/2027 Svip专业课/13.2027 机械',
        '/2027考研资料（持续更新）/27考研课程/2027 Svip专业课（渠道二 不加密）/2027 专业课/2027考研SVIP专业/06.机械【不加密】',
        # —— 公共课：全监控 ——
        '/2027考研资料（持续更新）/27考研课程/2027 Svip公共课',
        '/2027考研资料（持续更新）/27考研PDF/00.27考研冲刺PDF汇总',
        '/2027考研资料（持续更新）/27考研PDF/01.27数学PDF',
        '/2027考研资料（持续更新）/27考研PDF/02.27英语PDF',
        '/2027考研资料（持续更新）/27考研PDF/03.27政治PDF',
    ],
    # 报告里去掉这个前缀，路径短一点
    'strip_prefix': '/2027考研资料（持续更新）',

    'interval_minutes': 60,         # 扫描间隔（分钟）
    'workers': 12,                  # 并发线程数，太大容易被风控
    'page_size': 100,               # 每页条数（夸克上限就是 100）
    'max_retry': 3,                 # 单请求失败重试次数
    'request_timeout': 30,          # 请求超时（秒）
    'request_interval': 0.02,       # 单请求最小间隔（秒）
    'max_report_items': 200,        # 报告最多列多少条（完整记录仍写 updates 日志）

    # 邮件报告的粒度：
    #   'source' = 只到「老师 / 机构」层级（18.2027大牙、06.2027高途【唐静】…），不列具体文件
    #   'detail' = 旧行为，逐条列到文件
    # 明细不论选哪个都会写进 quark_share_updates.log，邮件只是摘要。
    'report_level': 'source',
    'max_report_groups': 60,        # source 粒度下最多列多少个老师/机构

    'cookie_file': '',              # 留空 = 自动在脚本目录里找

    # 邮件通知（沿用了出版署.py 的 QQ 邮箱配置）
    'notify_email': {
        'enabled': True,
        'sender': '202046940@qq.com',
        'receiver': '202046940@qq.com',
        'auth_code': '',   # 这里留空，靠环境变量 SMTP_AUTH_CODE 注入（别把授权码提交进仓库）
        'smtp_server': 'smtp.qq.com',
        'smtp_port': 465,
    },
}

# ====================================================================

BASE_DIR = os.environ.get('QUARK_MONITOR_DIR') or os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE_DIR, 'quark_share_snapshot.json.gz')
SNAPSHOT_FILE_OLD = os.path.join(BASE_DIR, 'quark_share_snapshot.json')   # 兼容旧版明文快照
LOG_FILE = os.path.join(BASE_DIR, 'quark_share_monitor.log')
UPDATES_FILE = os.path.join(BASE_DIR, 'quark_share_updates.log')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
API_HOST = 'https://drive-m.quark.cn'
TOKEN_API = API_HOST + '/1/clouddrive/share/sharepage/token'
DETAIL_API = API_HOST + '/1/clouddrive/share/sharepage/detail'

PWD_ID = ''
COOKIE_STR = ''
STOKEN = ''

_tls = threading.local()
_rate_lock = threading.Lock()
_last_req_ts = [0.0]

# cookie 文件里最后一次修改时间，用于提示 cookie 是否可能过期
REQ_COUNT = [0]


# ------------------------------ 基础工具 ------------------------------

def log(msg, echo=True):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    # 静默模式下 sys.stdout 已经是 _LogStream，再 print 会造成重复写一行
    if echo and not getattr(sys.stdout, 'is_log_sink', False):
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


class _LogStream:
    """pythonw.exe / 计划任务（无控制台）下 sys.stdout 是 None，这个假流把输出落到日志文件，
    保证所有 print 都不会炸。"""

    is_log_sink = True

    def write(self, s):
        if s and s.strip():
            try:
                with open(LOG_FILE, 'a', encoding='utf-8') as f:
                    f.write(s if s.endswith('\n') else s + '\n')
            except Exception:
                pass

    def flush(self):
        pass

    def reconfigure(self, **kw):
        pass


def human_size(n):
    try:
        n = float(n or 0)
    except Exception:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{int(n)} B" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024


def ts2str(ms):
    if not ms:
        return '-'
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return '-'


def short_path(p):
    pre = CONFIG.get('strip_prefix') or ''
    if pre and p.startswith(pre):
        return p[len(pre):] or '/'
    return p


def parse_pwd_id(url):
    m = re.search(r'/s/([0-9a-zA-Z]+)', url or '')
    return m.group(1) if m else (url or '').strip()


def find_cookie_file():
    """在脚本目录里找 cookie 文件：跳过脚本/日志/快照，取最新修改的含 cookie 文本文件"""
    if CONFIG.get('cookie_file'):
        p = CONFIG['cookie_file']
        return p if os.path.isabs(p) else os.path.join(BASE_DIR, p)

    skip_kw = ('quark_share_monitor', 'quark_share_snapshot', 'quark_share_updates',
               '__pycache__')
    cands = []
    for name in os.listdir(BASE_DIR):
        fp = os.path.join(BASE_DIR, name)
        if not os.path.isfile(fp):
            continue
        low = name.lower()
        if any(k in low for k in skip_kw) or low.endswith(('.log', '.py', '.pyc', '.md', '.bat')):
            continue
        if os.path.splitext(low)[1] not in ('.txt', '.json', '.cookie', '.dat', ''):
            continue
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                head = f.read(20000)
        except Exception:
            continue
        if head.count('=') >= 3 and ';' in head:      # 像 cookie
            cands.append((os.path.getmtime(fp), fp))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def get_session():
    """每个线程一个 Session"""
    s = getattr(_tls, 'session', None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            'User-Agent': UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Origin': 'https://pan.quark.cn',
            'Referer': f"https://pan.quark.cn/s/{PWD_ID}",
            'Cookie': COOKIE_STR,
        })
        _tls.session = s
    return s


def throttle():
    interval = float(CONFIG.get('request_interval') or 0)
    if interval <= 0:
        return
    while True:
        with _rate_lock:
            now = time.time()
            if now - _last_req_ts[0] >= interval:
                _last_req_ts[0] = now
                return
            wait = interval - (now - _last_req_ts[0])
        time.sleep(min(wait, interval))


# ------------------------------ 接口 ------------------------------

class AuthError(Exception):
    """cookie 失效 / 需要提取码 / 分享不存在"""


def init_share():
    """访问分享页拿游客 cookie，再换 stoken"""
    global STOKEN
    s = get_session()
    try:
        s.get(f"https://pan.quark.cn/s/{PWD_ID}", timeout=CONFIG['request_timeout'])
    except Exception as e:
        log(f"访问分享页失败（不致命）：{e}")

    body = {'pwd_id': PWD_ID, 'passcode': CONFIG.get('passcode', '')}
    last = ''
    for attempt in range(1, CONFIG['max_retry'] + 1):
        try:
            throttle()
            j = s.post(TOKEN_API, params={'pr': 'ucpro', 'fr': 'pc'},
                       json=body, timeout=CONFIG['request_timeout']).json()
        except Exception as e:
            last = str(e)
            log(f"获取 stoken 失败（第 {attempt} 次）：{e}")
            time.sleep(2 * attempt)
            continue

        code = j.get('code')
        msg = j.get('message') or ''
        if j.get('status') == 200 and (j.get('data') or {}).get('stoken'):
            d = j['data']
            STOKEN = d['stoken']
            return d
        last = f"{code} {msg}"
        if code in (41010, 41011, 41012) or '提取码' in msg or 'passcode' in str(j).lower():
            raise AuthError(f"该分享需要提取码，请填 CONFIG['passcode']（接口：{last}）")
        if code in (31001, 31003, 31004) or '登录' in msg:
            raise AuthError(f"cookie 已失效，请重新导出更新 cookie 文件（接口：{last}）")
        log(f"获取 stoken 异常返回：status={j.get('status')} code={code} msg={msg}")
        time.sleep(2 * attempt)

    raise AuthError(f"多次获取 stoken 均失败：{last}")


def list_dir(fid, page, size):
    """列一个目录的一页"""
    s = get_session()
    params = {
        'pr': 'ucpro', 'fr': 'pc',
        'pwd_id': PWD_ID, 'stoken': STOKEN,
        'pdir_fid': fid, 'force': '0',
        '_page': page, '_size': size,
        '_fetch_banner': '0', '_fetch_share': '0', '_fetch_total': '0',
        '_sort': 'file_type:asc,updated_at:desc',
    }
    throttle()
    j = s.get(DETAIL_API, params=params, timeout=CONFIG['request_timeout']).json()
    if j.get('status') != 200:
        raise RuntimeError(f"status={j.get('status')} code={j.get('code')} msg={j.get('message')}")
    REQ_COUNT[0] += 1
    return (j.get('data') or {}).get('list') or []


def list_dir_all(fid):
    """列一个目录的全部条目（自动翻页 + 重试）"""
    items, page = [], 1
    while True:
        lst = None
        for attempt in range(1, CONFIG['max_retry'] + 1):
            try:
                lst = list_dir(fid, page, CONFIG['page_size'])
                break
            except Exception:
                if attempt >= CONFIG['max_retry']:
                    raise
                time.sleep(1.5 * attempt)
        if not lst:
            break
        items.extend(lst)
        if len(lst) < CONFIG['page_size']:
            break
        page += 1
    return items


# ------------------------------ 扫描 ------------------------------

def resolve_path(path, cache):
    """按路径逐层解析出 fid（中间层结果缓存复用）"""
    parts = [p for p in path.strip('/').split('/') if p]
    fid, cur = '0', ''
    for part in parts:
        cur += '/' + part
        if cur in cache:
            fid = cache[cur]
            continue
        try:
            items = list_dir_all(fid)
        except Exception as e:
            raise RuntimeError(f"解析路径失败于「{cur}」：{e}")
        hit = next((i for i in items if (i.get('file_name') or '') == part), None)
        if not hit:
            return None
        fid = hit['fid']
        cache[cur] = fid
    return fid


def scan_tree():
    """
    扫描所有监控目录。
    返回 (items, failed, resolved)
      items:  {fid: {'p': 完整路径, 's': 大小, 't': 修改时间戳, 'd': 是否目录}}
      failed: [(监控路径, 原因)]
      resolved: {监控路径: fid}
    """
    items, failed, resolved = {}, [], {}
    cache = {}

    # 1. 解析监控起点
    for p in CONFIG['watch_paths']:
        try:
            fid = resolve_path(p, cache)
        except Exception as e:
            failed.append((p, str(e)))
            log(f"  ✗ 监控目录解析异常：{short_path(p)} -> {e}")
            continue
        if fid is None:
            failed.append((p, '目录不存在（可能被改名或删除）'))
            log(f"  ✗ 监控目录不存在：{short_path(p)}")
            continue
        resolved[p] = fid
        # 起点目录本身也作为一个条目记录（这样起点被替换也能发现）
        items[fid] = {'p': p, 's': 0, 't': 0, 'd': True}

    if not resolved:
        return items, failed, resolved

    # 2. 从各起点并发遍历
    frontier = [(fid, p) for p, fid in resolved.items()]
    depth, t0 = 0, time.time()
    while frontier:
        next_frontier = []

        def work(node):
            fid, path = node
            try:
                return path, list_dir_all(fid), None
            except Exception as e:
                return path, [], str(e)

        with ThreadPoolExecutor(max_workers=max(1, int(CONFIG['workers']))) as ex:
            for path, lst, err in ex.map(work, frontier):
                if err:
                    failed.append((path, err))
                    log(f"  ✗ 目录读取失败：{short_path(path)} -> {err}")
                    continue
                for it in lst:
                    fid = it.get('fid')
                    name = it.get('file_name') or ''
                    full = f"{path.rstrip('/')}/{name}"
                    is_dir = bool(it.get('dir'))
                    items[fid] = {'p': full, 's': it.get('size') or 0,
                                  't': it.get('updated_at') or 0, 'd': is_dir}
                    if is_dir:
                        next_frontier.append((fid, full))

        depth += 1
        if next_frontier:
            print(f"    第 {depth} 层：已收录 {len(items)} 项，待扫目录 {len(next_frontier)}，"
                  f"耗时 {time.time() - t0:.0f}s", flush=True)
        frontier = next_frontier

    return items, failed, resolved


def load_snapshot():
    for path, op in ((SNAPSHOT_FILE, gzip.open), (SNAPSHOT_FILE_OLD, open)):
        if not os.path.exists(path):
            continue
        try:
            with op(path, 'rt', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log(f"读取快照失败（将重建基线）：{os.path.basename(path)} {e}")
    return None


def save_snapshot(items, note=''):
    """gzip 存快照（4 万多条目，压缩后约 2MB，比明文省 8 倍）"""
    tmp = SNAPSHOT_FILE + '.tmp'
    with gzip.open(tmp, 'wt', encoding='utf-8') as f:
        json.dump({
            'scan_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'pwd_id': PWD_ID,
            'count': len(items),
            'note': note,
            'items': items,
        }, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, SNAPSHOT_FILE)
    if os.path.exists(SNAPSHOT_FILE_OLD):       # 旧的明文快照用不上了
        try:
            os.remove(SNAPSHOT_FILE_OLD)
        except Exception:
            pass


# ------------------------------ 对比 ------------------------------

def diff_snapshots(old_items, new_items, ignore_deleted=False):
    added, removed, renamed, modified = [], [], [], []
    old_items = old_items or {}
    for fid, nv in new_items.items():
        ov = old_items.get(fid)
        if ov is None:
            added.append(nv)
            continue
        if ov['p'] != nv['p']:
            renamed.append((ov, nv))
        if ov['s'] != nv['s'] or ov['t'] != nv['t']:
            modified.append((ov, nv))
    if not ignore_deleted:
        for fid, ov in old_items.items():
            if fid not in new_items:
                removed.append(ov)
    return added, removed, renamed, modified


def collapse_added(added):
    """新增的大目录不逐条列子孙，只保留最顶层那个新增目录并统计其中新增数"""
    if not added:
        return []
    add_dirs = {v['p'] for v in added if v['d']}
    tops = []
    for v in sorted(added, key=lambda x: x['p'].count('/')):
        parent = v['p'].rsplit('/', 1)[0] or '/'
        if parent in add_dirs:
            continue
        tops.append(v)
    out = []
    for v in tops:
        cnt = 0
        if v['d']:
            pre = v['p'] + '/'
            cnt = sum(1 for x in added if x['p'].startswith(pre))
        out.append({'item': v, 'children': cnt})
    return out


# 「编号目录」形如 18.2027大牙 / 08.2027新东方【王江涛 易熙人】 / 13.2027 机械 / 25.2027八哥
# 注意学科那一级也长这样（01.2027 Svip政治），所以取的是路径里最深的一个。
# 课程级目录（06.命题规律解析、17.【冲刺阶段-二轮刷题】、08.27考研…）都不带 4 位年份，不会被误判。
_UNIT_RE = re.compile(r'^\d{1,3}\s*[.．、]\s*20\d{2}')


def _disp_width(s):
    """中文按 2 列算，用来对齐报告里的文字"""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)


def _pad(s, width):
    return s + ' ' * max(0, width - _disp_width(s))


def source_of(path, is_dir):
    """把一个路径归到「老师 / 机构」这一层。

    分享树是「学科 / 编号.老师或机构 / 课程 / 文件」，学科和老师都带编号前缀，
    所以取路径里**最深**的那个「编号.20XX」目录（18.2027大牙、06.2027高途【唐静】…）。
    一个都没有时（如 PDF 区的 27徐涛PDF），目录取自身、文件取它的上一级目录。
    """
    parts = [p for p in (path or '').split('/') if p]
    if not parts:
        return '/'
    dirs = parts if is_dir else parts[:-1]
    if not dirs:
        return '/'
    for i in range(len(dirs) - 1, -1, -1):
        if _UNIT_RE.match(dirs[i]):
            return '/' + '/'.join(dirs[:i + 1])
    return '/' + '/'.join(dirs)


def source_label(key, keep=2):
    """老师/机构层级显示成「学科 / 老师」；层数太多只留最后 keep 段"""
    segs = [s for s in short_path(key).split('/') if s]
    dedup = [s for i, s in enumerate(segs) if i == 0 or s != segs[i - 1]]   # 去掉重复的相邻层
    if not dedup:
        return short_path(key)
    return ' / '.join(dedup[-keep:])


def group_by_source(added, removed, renamed, modified):
    """把四类变化按老师/机构层级归并，供摘要报告使用。

    返回按变化总数从多到少排序的列表，每项含各类型的条数、最新时间、新增目录数。
    """
    groups = {}

    def put(key, kind, ts=0, is_dir=False):
        g = groups.setdefault(key, {'key': key, 'added': 0, 'removed': 0,
                                    'renamed': 0, 'modified': 0,
                                    'total': 0, 'newest': 0, 'dirs': 0})
        g[kind] += 1
        g['total'] += 1
        if ts and ts > g['newest']:
            g['newest'] = ts
        if is_dir:
            g['dirs'] += 1

    for v in added:
        put(source_of(v['p'], v['d']), 'added', v.get('t'), v['d'])
    for v in removed:
        put(source_of(v['p'], v['d']), 'removed', v.get('t'), v['d'])
    for _, nv in renamed:
        put(source_of(nv['p'], nv['d']), 'renamed', nv.get('t'), nv['d'])
    for _, nv in modified:
        put(source_of(nv['p'], nv['d']), 'modified', nv.get('t'), nv['d'])

    out = list(groups.values())
    for g in out:
        g['label'] = source_label(g['key'])
    out.sort(key=lambda g: (-g['total'], g['key']))
    return out


def format_source_report(added, removed, renamed, modified, incomplete=False):
    """摘要报告：只到老师 / 机构层级，不列具体文件（邮件用）"""
    n = CONFIG.get('max_report_groups', 60)
    groups = group_by_source(added, removed, renamed, modified)
    if not groups:
        return "本次无变化。"

    total = len(added) + len(removed) + len(renamed) + len(modified)
    lines = [f"涉及 {len(groups)} 个老师 / 机构，共 {total} 项变化", '']

    for title, kind, show_dirs in (('新增', 'added', True),
                                   ('内容变化', 'modified', False),
                                   ('改名 / 移动', 'renamed', False),
                                   ('删除', 'removed', False)):
        rows = [g for g in groups if g[kind]]
        if not rows:
            continue
        rows.sort(key=lambda g: (-g[kind], g['key']))
        lines.append(f"【{title}】{sum(g[kind] for g in rows)} 项，"
                     f"{len(rows)} 个老师 / 机构")
        head = rows[:n]
        width = min(max((_disp_width(g['label']) for g in head), default=0), 44)
        for g in head:
            tail = f"{g[kind]} 项"
            if show_dirs and g['dirs']:
                tail += f"（含 {g['dirs']} 个新目录）"
            if g['newest']:
                tail += f"　最新 {ts2str(g['newest'])[5:16]}"
            lines.append(f"  · {_pad(g['label'], width)}  {tail}")
        if len(rows) > n:
            lines.append(f"  · … 另有 {len(rows) - n} 个来源未列出")
        lines.append('')

    lines.append("（要看具体是哪些文件，见 quark_share_updates.log 的明细段）")
    if incomplete:
        lines.append("⚠ 本次有目录读取失败，结果可能不完整，「删除」项已忽略。")
    return '\n'.join(lines).rstrip()


def format_diff_report(added, removed, renamed, modified, incomplete=False, level=None):
    level = level or CONFIG.get('report_level', 'detail')
    if level == 'source':
        return format_source_report(added, removed, renamed, modified, incomplete)

    n = CONFIG['max_report_items']
    tops = collapse_added(added)
    lines = [f"新增 {len(tops)} 个顶层项（含子孙共 {len(added)} 项）"]
    for t in tops[:n]:
        v = t['item']
        tag = '目录' if v['d'] else '文件'
        extra = f"，含 {t['children']} 个子项" if t['children'] else ''
        size = '' if v['d'] else f" [{human_size(v['s'])}]"
        lines.append(f"  [新增·{tag}] {short_path(v['p'])}{size}{extra}  ({ts2str(v['t'])})")
    if len(tops) > n:
        lines.append(f"  ...（还有 {len(tops) - n} 个新增项，见 quark_share_updates.log）")
    if not tops:
        lines.append("  （无）")

    if renamed:
        lines.append(f"\n改名/移动 {len(renamed)} 项")
        for ov, nv in renamed[:n]:
            lines.append(f"  [移动] {short_path(ov['p'])}\n      -> {short_path(nv['p'])}")

    if modified:
        lines.append(f"\n内容变化 {len(modified)} 项")
        for ov, nv in modified[:n]:
            if ov['s'] != nv['s']:
                lines.append(f"  [修改] {short_path(nv['p'])}  {human_size(ov['s'])} -> {human_size(nv['s'])}")
            else:
                lines.append(f"  [修改] {short_path(nv['p'])}  更新于 {ts2str(nv['t'])}")

    if removed:
        lines.append(f"\n删除 {len(removed)} 项")
        for v in removed[:n]:
            lines.append(f"  [删除·{'目录' if v['d'] else '文件'}] {short_path(v['p'])}")
        if len(removed) > n:
            lines.append(f"  ...（还有 {len(removed) - n} 项）")
        lines.append("\n（仅供参考：删除可能是分享者清理，也可能是对方改名/移动）")

    if incomplete:
        lines.append("\n⚠ 本次有目录读取失败，结果可能不完整，「删除」项已忽略。")
    return '\n'.join(lines)


# ------------------------------ 通知 ------------------------------

def send_email(subject, content):
    cfg = CONFIG['notify_email']
    if not cfg.get('enabled'):
        return False
    try:
        msg = MIMEText(content, 'plain', 'utf-8')
        msg['From'] = Header(cfg['sender'])
        msg['To'] = Header(cfg['receiver'])
        msg['Subject'] = Header(subject)
        server = smtplib.SMTP_SSL(cfg['smtp_server'], cfg['smtp_port'], timeout=25)
        server.login(cfg['sender'], cfg['auth_code'])
        server.sendmail(cfg['sender'], [cfg['receiver']], msg.as_string())
        server.quit()
        log(f"邮件已发送 -> {cfg['receiver']}")
        return True
    except Exception as e:
        log(f"邮件发送失败：{type(e).__name__}: {e}")
        return False


# ------------------------------ 主流程 ------------------------------

def check_once():
    """扫一次、对比、上报。返回本次差异总数。"""
    REQ_COUNT[0] = 0
    log("=" * 60)
    log(f"开始扫描 {len(CONFIG['watch_paths'])} 个监控目录")
    t0 = time.time()

    init_share()
    items, failed, resolved = scan_tree()
    elapsed = time.time() - t0

    n_files = sum(1 for v in items.values() if not v['d'])
    n_dirs = len(items) - n_files
    log(f"扫描完成：{n_dirs} 个目录 / {n_files} 个文件，"
        f"{REQ_COUNT[0]} 次请求，耗时 {elapsed:.0f}s，失败 {len(failed)} 处")

    if failed:
        for p, why in failed:
            log(f"  ! {short_path(p)} -> {why}")

    snap = load_snapshot()
    if snap is None or not snap.get('items'):
        save_snapshot(items, note='首次基线')
        print(f"\n首次运行：已记录基线（{n_dirs} 个目录 / {n_files} 个文件）。"
              f"下次扫描开始报告更新。\n")
        log("已建立基线快照")
        return 0

    ignore_deleted = bool(failed)
    added, removed, renamed, modified = diff_snapshots(snap['items'], items,
                                                       ignore_deleted=ignore_deleted)
    total = len(added) + len(removed) + len(renamed) + len(modified)
    now = datetime.now()

    head = (f"【夸克考研资料更新】{now:%Y-%m-%d %H:%M}\n"
            f"分享：2027考研资料（持续更新）\n"
            f"范围：专业课(机械×2) + 公共课(政治/英语/数学)\n"
            f"当前 {n_dirs} 个目录 / {n_files} 个文件\n"
            f"{'-' * 50}\n")
    report = head + format_diff_report(added, removed, renamed, modified,
                                       incomplete=ignore_deleted, level='source')

    if total:
        n_src = len(group_by_source(added, removed, renamed, modified))
        log(f"检测到更新：新增 {len(added)} / 删除 {len(removed)} / "
            f"移动 {len(renamed)} / 修改 {len(modified)}，涉及 {n_src} 个老师/机构")
        print("\n" + report + "\n")
        # 本地日志留全量明细（邮件只看得到摘要，出了事好回查）
        try:
            with open(UPDATES_FILE, 'a', encoding='utf-8') as f:
                f.write("\n" + "=" * 70 + "\n" + report + "\n"
                        + "-" * 70 + "\n【明细】\n"
                        + format_diff_report(added, removed, renamed, modified,
                                             incomplete=ignore_deleted, level='detail')
                        + "\n")
        except Exception:
            pass
        send_email(f"夸克考研资料更新（{n_src} 个老师/机构有更新）", report)
    else:
        log("无更新")
        print("  无更新\n")

    save_snapshot(items,
                  note=f"+{len(added)}/-{len(removed)}/改{len(modified)}"
                       + ("（不完整）" if failed else ""))
    return total


def main():
    global PWD_ID, COOKIE_STR

    ap = argparse.ArgumentParser(description='夸克网盘分享更新监控（定向）')
    ap.add_argument('--once', action='store_true', help='只扫描一次（给计划任务用）')
    ap.add_argument('--reset', action='store_true', help='删除快照，下次重建基线')
    ap.add_argument('--interval', type=int, help='扫描间隔（分钟）')
    ap.add_argument('--workers', type=int, help='并发线程数')
    ap.add_argument('--only', help='只监控指定目录（完整路径，可重复用逗号分隔）')
    ap.add_argument('--no-email', action='store_true', help='本次不发邮件（手动调试用）')
    ap.add_argument('--test-email', action='store_true', help='发一封测试邮件')
    ap.add_argument('--list-watch', action='store_true', help='只检查监控目录能否解析到')
    ap.add_argument('--report-level', choices=('source', 'detail'),
                    help='报告粒度：source=只到老师/机构层级（默认），detail=逐条列文件')
    args = ap.parse_args()

    # 无控制台环境（pythonw / 计划任务静默运行）下把 stdout/stderr 接到日志文件
    if sys.stdout is None or sys.stderr is None:
        _fake = _LogStream()
        if sys.stdout is None:
            sys.stdout = _fake
        if sys.stderr is None:
            sys.stderr = _fake
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    if args.interval:
        CONFIG['interval_minutes'] = args.interval
    if args.workers:
        CONFIG['workers'] = args.workers
    if args.only:
        CONFIG['watch_paths'] = [p.strip() for p in args.only.split(',') if p.strip()]
    if args.report_level:
        CONFIG['report_level'] = args.report_level
    if args.no_email:
        CONFIG['notify_email']['enabled'] = False

    # 邮件账号信息允许被环境变量覆盖（放到公开仓库 / 云函数上时不用把授权码写进代码）
    if os.environ.get('SMTP_AUTH_CODE'):
        CONFIG['notify_email']['auth_code'] = os.environ['SMTP_AUTH_CODE'].strip()
    if os.environ.get('SMTP_RECEIVER'):
        CONFIG['notify_email']['receiver'] = os.environ['SMTP_RECEIVER'].strip()

    PWD_ID = parse_pwd_id(CONFIG['share_url'])

    if args.test_email:
        ok = send_email("夸克分享监控 · 测试邮件",
                        f"这是一封测试邮件。\n时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
                        f"监控目录数：{len(CONFIG['watch_paths'])}")
        print("✅ 测试邮件已发送，请查收。" if ok else "❌ 测试邮件发送失败，看日志。")
        return 0 if ok else 1

    if args.reset:
        for p in (SNAPSHOT_FILE, SNAPSHOT_FILE_OLD):
            if os.path.exists(p):
                os.remove(p)
        print("已删除旧快照，下次扫描将重建基线。")

    # cookie 优先取环境变量 QUARK_COOKIE（云端 / GitHub Actions 用），否则读本地文件
    env_cookie = (os.environ.get('QUARK_COOKIE') or '').strip()
    if env_cookie:
        COOKIE_STR = env_cookie
        log(f"cookie：来自环境变量 QUARK_COOKIE（{len(COOKIE_STR)} 字符）")
    else:
        ck = find_cookie_file()
        if not ck:
            print(f"❌ 在 {BASE_DIR} 下没找到 cookie 文件。\n"
                  f"   请在 CONFIG['cookie_file'] 指定文件名，或把 cookie 文本放到该目录，\n"
                  f"   或设置环境变量 QUARK_COOKIE。")
            return 1
        try:
            with open(ck, 'r', encoding='utf-8', errors='ignore') as f:
                COOKIE_STR = f.read().strip()
        except Exception as e:
            print(f"❌ 读取 cookie 文件失败：{e}")
            return 1
        if len(COOKIE_STR) < 20:
            print(f"❌ cookie 文件内容过短（{ck}），请检查。")
            return 1
        log(f"cookie：{os.path.basename(ck)}（{len(COOKIE_STR)} 字符，"
            f"文件修改于 {datetime.fromtimestamp(os.path.getmtime(ck)):%Y-%m-%d %H:%M}）")

    try:
        if args.list_watch:
            init_share()
            cache = {}
            for p in CONFIG['watch_paths']:
                fid = resolve_path(p, cache)
                print(f"{'✅' if fid else '❌'} {short_path(p)}"
                      f"{'  fid=' + fid[:8] if fid else '  （没找到）'}")
            return 0
    except AuthError as e:
        print(f"\n❌ {e}\n")
        return 2

    if args.once:
        try:
            check_once()
            return 0
        except AuthError as e:
            log(f"鉴权失败：{e}")
            print(f"\n❌ {e}\n")
            return 2

    interval = int(CONFIG['interval_minutes'])
    print(f"夸克分享监控已启动：每 {interval} 分钟扫一次，Ctrl+C 退出。")
    print(f"监控范围：专业课(机械×2) + 公共课\n")
    while True:
        try:
            check_once()
        except AuthError as e:
            log(f"鉴权失败（cookie 可能过期）：{e}")
            print(f"\n❌ {e}\n")
            send_email("夸克分享监控 · cookie 失效",
                       f"cookie 可能已过期，请重新导出更新 cookie 文件。\n接口返回：{e}")
        except KeyboardInterrupt:
            print("\n已手动停止。")
            return 0
        except Exception as e:
            log(f"本轮扫描异常：{type(e).__name__}: {e}")

        nxt = time.time() + interval * 60
        print(f"下次扫描：{datetime.fromtimestamp(nxt):%Y-%m-%d %H:%M:%S}\n")
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            print("\n已手动停止。")
            return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
