import asyncio
import aiohttp
import json
import os
import time
from datetime import datetime
from functools import reduce
from hashlib import md5
import urllib.parse
import threading
import argparse
import requests
import smtplib
from email.header import Header
from email.mime.text import MIMEText

# ==================== WBI 签名模块 (解决 -352 风控) ====================
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

_wbi_keys_cache = {"img_key": "", "sub_key": "", "ts": 0}
_cache_lock = threading.Lock()
_CACHE_TTL = 24 * 3600


def get_mixin_key(orig: str) -> str:
    return reduce(lambda s, i: s + orig[i], MIXIN_KEY_ENC_TAB, '')[:32]


def get_wbi_keys(sessdata: str = "") -> tuple:
    global _wbi_keys_cache
    now = time.time()
    with _cache_lock:
        if _wbi_keys_cache["img_key"] and now - _wbi_keys_cache["ts"] < _CACHE_TTL:
            return _wbi_keys_cache["img_key"], _wbi_keys_cache["sub_key"]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.bilibili.com/',
    }
    if sessdata:
        headers['Cookie'] = f'SESSDATA={sessdata}'

    try:
        resp = requests.get(
            'https://api.bilibili.com/x/web-interface/nav',
            headers=headers, timeout=10
        )
        data = resp.json()
        if data.get('code') == 0 and 'wbi_img' in data['data']:
            img_url = data['data']['wbi_img']['img_url']
            sub_url = data['data']['wbi_img']['sub_url']
            img_key = img_url.rsplit('/', 1)[-1].split('.')[0]
            sub_key = sub_url.rsplit('/', 1)[-1].split('.')[0]
            _wbi_keys_cache.update({"img_key": img_key, "sub_key": sub_key, "ts": now})
            return img_key, sub_key
    except Exception as e:
        print(f"[WBI] 获取密钥失败: {e}")

    return "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    mixin_key = get_mixin_key(img_key + sub_key)
    curr_time = round(time.time())
    signed_params = params.copy()
    signed_params['wts'] = curr_time
    signed_params = dict(sorted(signed_params.items()))
    signed_params = {
        k: ''.join(filter(lambda c: c not in "!'()*", str(v)))
        for k, v in signed_params.items()
    }
    query = urllib.parse.urlencode(signed_params, doseq=True).replace('+', '%20')
    wbi_sign = md5((query + mixin_key).encode()).hexdigest()
    signed_params['w_rid'] = wbi_sign
    return signed_params


def sign_popular_params(pn: int, ps: int, sessdata: str = "") -> dict:
    img_key, sub_key = get_wbi_keys(sessdata)
    return enc_wbi({'pn': pn, 'ps': ps}, img_key, sub_key)


# ==================== 配置 ====================
# 云端（GitHub Actions）用：状态目录、邮件账号全部走环境变量
STATE_DIR = os.environ.get('BILI_STATE_DIR') or os.path.dirname(os.path.abspath(__file__))
BLACKLIST_FILE = os.path.join(STATE_DIR, 'blacklist.json')

SMTP_SENDER = os.environ.get('SMTP_SENDER', '202046940@qq.com')
SMTP_RECEIVER = os.environ.get('SMTP_RECEIVER', '202046940@qq.com')
SMTP_AUTH_CODE = os.environ.get('SMTP_AUTH_CODE', '')
SMTP_SERVER = 'smtp.qq.com'
SMTP_PORT = 465

# 本次运行新拉黑的记录，跑完汇总发邮件
BLOCKED_RECORDS = []

FILTER_KEYWORD = '鸣潮'

# 白名单：这些 UP 主永远不会被拉黑
WHITELIST_MIDS = {23084818}

# 请填入你的完整 Cookie (需包含 SESSDATA, bili_jct, DedeUserID)
COOKIE = os.environ.get('BILI_COOKIE', '').strip()


# ==================== 本地黑名单存取 ====================
def load_blacklist() -> dict:
    os.makedirs(os.path.dirname(BLACKLIST_FILE) or '.', exist_ok=True)
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[黑名单] 读取失败: {e}")
    return {}


def save_blacklist(blacklist: dict):
    try:
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(blacklist, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[黑名单] 保存失败: {e}")


# ==================== 邮件通知 ====================
def send_block_email(records: list, total_page: int):
    """只在本次有新拉黑时发送；没拉黑就静默跳过"""
    if not records:
        print("📭 本次没有拉黑任何人，不发邮件。")
        return False
    if not SMTP_AUTH_CODE:
        print("⚠ 没配置 SMTP_AUTH_CODE，跳过邮件发送")
        return False

    lines = [f"本次拉黑 {len(records)} 位 UP 主：", ""]
    for i, r in enumerate(records, 1):
        lines.append(f"{i}. {r['name'] or '(未知昵称)'}")
        lines.append(f"   mid: {r['mid']}")
        lines.append(f"   视频: {r['title']}")
        if r.get('bvid'):
            lines.append(f"   链接: https://www.bilibili.com/video/{r['bvid']}")
        lines.append(f"   时间: {r['time']}")
        lines.append("")
    lines.append(f"共扫描 {total_page} 页热门榜，关键词：{FILTER_KEYWORD}")
    body = "\n".join(lines)

    subject = f"B站热门拉黑 {len(records)} 位（{datetime.now():%m-%d %H:%M}）"
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['From'] = Header(SMTP_SENDER)
        msg['To'] = Header(SMTP_RECEIVER)
        msg['Subject'] = Header(subject)
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=25)
        server.login(SMTP_SENDER, SMTP_AUTH_CODE)
        server.sendmail(SMTP_SENDER, [SMTP_RECEIVER], msg.as_string())
        server.quit()
        print(f"📧 邮件已发送 -> {SMTP_RECEIVER}")
        return True
    except Exception as e:
        print(f"❌ 邮件发送失败: {type(e).__name__}: {e}")
        return False


# ==================== B站 API 封装 ====================
class BiliAPI:
    def __init__(self, cookie: str):
        self.cookie = cookie
        self.csrf = self._extract_csrf()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.bilibili.com/",
            "Cookie": cookie,
            "Content-Type": "application/x-www-form-urlencoded"
        }
        self.session = None

    def _extract_csrf(self):
        import re
        m = re.search(r'bili_jct=([^;]+)', self.cookie)
        return m.group(1) if m else ''

    def _extract_sessdata(self):
        import re
        m = re.search(r'SESSDATA=([^;]+)', self.cookie)
        return m.group(1) if m else ""

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(limit_per_host=5, ssl=False)
        self.session = aiohttp.ClientSession(connector=connector, headers=self.headers)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def get_popular(self, pn=1, ps=50):
        signed_params = sign_popular_params(pn, ps, self._extract_sessdata())
        url = 'https://api.bilibili.com/x/web-interface/popular'
        async with self.session.get(url, params=signed_params) as resp:
            data = await resp.json()
            if data['code'] == -352:
                global _wbi_keys_cache
                with _cache_lock:
                    _wbi_keys_cache["ts"] = 0
                signed_params = sign_popular_params(pn, ps, self._extract_sessdata())
                async with self.session.get(url, params=signed_params) as resp2:
                    data = await resp2.json()
            if data['code'] == 0:
                return data['data']['list'], not data['data'].get('no_more', True)
            print(f"[热门] 获取失败: {data.get('message')}")
            return None, False

    async def get_video_tags(self, bvid: str) -> list:
        url = 'https://api.bilibili.com/x/web-interface/view/detail/tag'
        async with self.session.get(url, params={'bvid': bvid}) as resp:
            data = await resp.json()
            if data['code'] == 0:
                return [tag['tag_name'] for tag in data['data']]
            return []

    async def block_user(self, target_mid: int) -> bool:
        if not self.csrf:
            print("缺少 bili_jct，无法拉黑")
            return False
        url = 'https://api.bilibili.com/x/relation/modify'
        data = {'fid': target_mid, 'act': 5, 'csrf': self.csrf}
        async with self.session.post(url, data=data) as resp:
            result = await resp.json()
            if result['code'] == 0:
                return True
            print(f"❌ 拉黑失败 {target_mid}: {result.get('message')}")
            return False

    async def fetch_online_blacklist(self) -> set:
        """抓取线上全部黑名单用户 mid"""
        mids = set()
        pn = 1
        while True:
            url = 'https://api.bilibili.com/x/relation/blacks'
            params = {'re_version': 0, 'pn': pn, 'ps': 50, 'jsonp': 'jsonp', 'web_location': 333.33}
            async with self.session.get(url, params=params) as resp:
                data = await resp.json()
            if data['code'] != 0:
                print(f"[黑名单] 线上拉取失败(第{pn}页): {data.get('message')}")
                break
            page_list = data['data']['list']
            if not page_list:
                break
            for item in page_list:
                mids.add(int(item['mid']))
            total = data['data']['total']
            print(f"   已拉取 {len(mids)}/{total}")
            if len(mids) >= total:
                break
            pn += 1
            await asyncio.sleep(0.5)
        return mids


# ==================== 主流程 ====================
async def sync_online_blacklist(api: BiliAPI, blacklist: dict) -> dict:
    """从线上抓取全部黑名单，合并进本地记录"""
    print("📥 正在抓取线上黑名单...")
    mids = await api.fetch_online_blacklist()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_count = 0
    for mid in mids:
        key = str(mid)
        if key not in blacklist:
            blacklist[key] = {"mid": mid, "reason": "线上黑名单同步", "time": now}
            new_count += 1
    save_blacklist(blacklist)
    print(f"✅ 线上黑名单共 {len(mids)} 人，新增记录 {new_count} 条")
    return blacklist


async def process_page(api: BiliAPI, page: int, keyword: str, blacklist: dict,
                       blocked_now: set) -> tuple:
    """处理一页热门视频：内存中筛选并直接拉黑，返回 (拉黑数量, 是否还有更多)"""
    videos, has_more = await api.get_popular(pn=page)
    if not videos:
        return 0, False

    # 并发取标签（仅内存）
    async def fetch_tags(v):
        v['tags'] = await api.get_video_tags(v.get('bvid'))
        return v

    await asyncio.gather(*[fetch_tags(v) for v in videos])

    count = 0
    for v in videos:
        title = v.get('title', '')
        reason = v.get('rcmd_reason', {}).get('content', '')
        tags = ', '.join(v.get('tags', []))
        mid = v.get('owner', {}).get('mid', 0)

        if not (keyword in title or keyword in reason or keyword in tags):
            continue

        print(f"🎯 匹配: {title[:30]}... UP主: {mid}({v.get('owner', {}).get('name', '')})")

        if mid in WHITELIST_MIDS:
            print(f"   ⚪ {mid} 在白名单，跳过")
            continue
        if str(mid) in blacklist or mid in blocked_now:
            print(f"   ⏭️ {mid} 已在黑名单")
            continue

        if await api.block_user(mid):
            blacklist[str(mid)] = {
                "mid": mid,
                "reason": f"关键词[{keyword}]匹配: {title[:50]}",
                "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            save_blacklist(blacklist)
            blocked_now.add(mid)
            BLOCKED_RECORDS.append({
                "mid": mid,
                "name": v.get('owner', {}).get('name', ''),
                "title": title,
                "bvid": v.get('bvid', ''),
                "time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            print(f"   ✅ 已拉黑 {mid}")
            count += 1
        await asyncio.sleep(0.5)

    return count, has_more


async def main():
    parser = argparse.ArgumentParser(description='B站热门视频关键词拉黑脚本')
    parser.add_argument('keyword', nargs='?', default=FILTER_KEYWORD, help='过滤关键词')
    parser.add_argument('-s', '--sync', action='store_true',
                        help='强制重新从线上抓取黑名单更新本地记录')
    args = parser.parse_args()

    print(f"🔍 关键词: {args.keyword}")
    print(f"⚪ 白名单: {WHITELIST_MIDS}")

    if not COOKIE:
        print("❌ 错误: 没有 cookie，请设置环境变量 BILI_COOKIE")
        return

    blacklist = load_blacklist()
    # 云端首轮状态文件是空的（或缓存被清了），这种情况同样要同步一次线上黑名单，
    # 否则会对着早就拉黑过的用户反复发无效请求、而且"黑名单共 N 人"会少算
    first_run = not blacklist

    async with BiliAPI(COOKIE) as api:
        if not api.csrf:
            print("❌ Cookie 中缺少 bili_jct，拉黑功能不可用")
            return

        # 云端的状态文件随时可能对不上（缓存被清、换了 runner、记录不全），
        # 所以默认每轮都同步一次线上黑名单（1200 人约 20 秒），
        # 避免对着早就拉黑过的人反复发无效请求、以及"黑名单共 N 人"少算。
        # 想恢复成「只在首次运行或加 -s 时同步」，把环境变量 BILI_SYNC_EVERY_RUN 设为 0。
        sync_every_run = os.environ.get('BILI_SYNC_EVERY_RUN', '1') == '1'
        if first_run or args.sync or sync_every_run:
            tag = "🆕 首次运行" if first_run else ("🔁 指定 -s" if args.sync else "🔄 常规同步")
            print(f"{tag}，抓取线上黑名单...")
            blacklist = await sync_online_blacklist(api, blacklist)
        else:
            print(f"📋 本地黑名单: {len(blacklist)} 个用户")

        page = 1
        blocked_total = 0
        has_more = True
        max_pages = 50
        blocked_now = set()

        while has_more and page <= max_pages:
            print(f"\n📄 正在处理第 {page} 页...")
            count, has_more = await process_page(api, page, args.keyword,
                                                 blacklist, blocked_now)
            blocked_total += count
            page += 1
            await asyncio.sleep(1.5)

        print(f"\n✅ 运行完成！本次新拉黑 {blocked_total} 人，黑名单共 {len(blacklist)} 人")

        # 只在本次真的拉黑了人才发邮件
        send_block_email(BLOCKED_RECORDS, page - 1)


if __name__ == '__main__':
    print("=" * 50)
    print("B站热门视频关键词拉黑 v4.0 (精简版)")
    print("=" * 50)
    asyncio.run(main())
