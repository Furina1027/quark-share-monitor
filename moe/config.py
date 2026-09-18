# -*- coding: utf-8 -*-
"""
监控脚本的全部可调配置。

只想改监控年份 / 检查频率 / 收件邮箱时，改这个文件就够了，
不需要动 moe_monitor/ 下的任何代码。
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))

# ==========================================================================
# 一、监控目标
# ==========================================================================

# 要盯的招生年度。改成 "2027" 就是找 2027 年的那份规定。
TARGET_YEAR = "2027"

# 文件本体标题里必然出现的核心词（不含年份，年份单独判）。
CORE_KEYWORD = "全国硕士研究生招生工作管理规定"

# 次级信号：教育部在发文件前/后通常会发一条部署新闻，
# 标题形如「教育部部署2027年全国硕士研究生考试招生工作」。
# 命中"全部关键词 + 目标年份"就视为信号，会一并通知。
SIGNAL_KEYWORDS = ("全国硕士研究生", "招生")

# ==========================================================================
# 二、数据源
# ==========================================================================
# 每个源都是"列表页"，脚本会抓列表 -> 提取标题与链接 -> 按上面规则匹配。

SOURCES = [
    {
        "name": "教育部文件（官方文件列表）",
        "kind": "list",
        "url": "http://www.moe.gov.cn/was5/web/search?channelid=239993",
        "detail_pattern": r"/t\d{8}_\d+\.html",
        "weight": "primary",
    },
    {
        "name": "教育部新闻通稿",
        "kind": "list",
        "url": "http://www.moe.gov.cn/jyb_xwfb/gzdt_gzdt/s5987/",
        "detail_pattern": r"/t\d{8}_\d+\.html",
        "weight": "primary",
    },
]

# 网络请求
REQUEST_TIMEOUT = 25        # 单次请求超时（秒）
REQUEST_RETRIES = 3         # 每个地址的失败重试次数
RETRY_BACKOFF = 3           # 重试间隔基数（秒），退避为 backoff * 2**n
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
)

# ==========================================================================
# 三、运行节奏
# ==========================================================================

CHECK_INTERVAL = 3600       # 每轮检查间隔（秒），默认 1 小时
JITTER = 60                 # 每轮随机抖动上限（秒），避免每次都卡在同一秒
STARTUP_NOTIFY = True       # 启动时是否发一封"已启动 + 当前状态"邮件
FAILURE_ALERT_THRESHOLD = 3 # 连续多少轮"所有源都失败"才告警（避免偶发抖动刷屏）

# ==========================================================================
# 四、邮件通知
# ==========================================================================

EMAIL_CONFIG = {
    "sender": "202046940@qq.com",        # 发件人邮箱
    # ⚠️ 授权码不写进代码：从环境变量 SMTP_AUTH_CODE 读（GitHub Actions 里用 Secret 注入）。
    #    本地跑就设一下环境变量，或者把值填在这里 —— 但别提交上去。
    "auth_code": os.environ.get("SMTP_AUTH_CODE", ""),
    "receiver": os.environ.get("SMTP_RECEIVER", "202046940@qq.com"),  # 收件人邮箱
    "smtp_server": "smtp.qq.com",        # QQ 邮箱 SMTP 服务器
    "smtp_port": 465,                    # QQ 邮箱 SSL 端口
}
EMAIL_TIMEOUT = 20          # SMTP 连接超时（秒）

# ==========================================================================
# 五、文件落盘
# ==========================================================================

DATA_DIR = os.path.join(_HERE, "data")              # 抓到的正文、状态、清单
LOG_DIR = os.path.join(_HERE, "logs")               # 运行日志
DOCUMENT_DIR = os.path.join(DATA_DIR, "documents")  # 命中的文件正文（txt）
STATE_FILE = os.path.join(DATA_DIR, "state.json")   # 已通知记录，用于去重
LOG_FILE = os.path.join(LOG_DIR, "monitor.log")
LOG_MAX_BYTES = 2 * 1024 * 1024                     # 单个日志文件上限
LOG_BACKUP_COUNT = 5                                # 日志轮转保留份数
