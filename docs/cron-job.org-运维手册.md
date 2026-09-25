# cron-job.org 运维手册

> 面向**后续接手维护的 Agent / 人**。看完这篇你应该能：查清任务状态、改触发时间、换 token、新建或停用任务，而不需要重新摸索。
>
> 最后核实日期：2026-09-25（API 细节对照官方文档 https://docs.cron-job.org/rest-api.html 核对过）

---

## 0. 它在这个项目里的角色（30 秒背景）

这个仓库的四个监控任务跑在 **GitHub Actions** 上，但**不用 GitHub 自带的 `schedule`** ——
早期实测它在本人账号/仓库下**完全不触发**（四个时间点一个都没来，平台会把排队超时的任务直接丢弃）。

所以改用 **cron-job.org 当外部定时器**：每小时按点向 GitHub 的 `workflow_dispatch` 接口发一次 POST，
走实时事件通道，**秒级创建运行**（实测误差 2 秒内）。

workflow 里的 `schedule` 仍然保留当兜底；两条路径叠加**不会重复发邮件**（脚本都是"有新内容才发"，
状态持久化在 Actions Cache 里，第二次跑会正确判定"无变化"）。

```
cron-job.org ──每小时 POST dispatches──▶ GitHub API ──▶ 创建 workflow run ──▶ 跑脚本 ──▶ 有变化才发邮件
```

---

## 1. 本账户现状：四个 job

全部时区 `Asia/Shanghai`，方法 `POST`，body `{"ref":"main"}`。

| jobId | 名称 | 触发分钟 | 目标 workflow | 目标仓库 |
|---|---|---|---|---|
| `8467348` | 夸克考研资料更新监控 | 每小时第 **07** 分 | `monitor.yml` | `Furina1027/quark-share-monitor` |
| `8467829` | 教育部 2027 招生规定监控 | 每小时第 **13** 分 | `moe-monitor.yml` | 同上 |
| `8467349` | B站热门关键词拉黑 | 每小时第 **23** 分 | `bili-block.yml` | 同上 |
| `8467350` | 南航招生公告监控（机电 + 研究生院） | 每小时第 **41** 分 | `nuaa-monitor.yml` | 同上 |

四个 job 的 URL 长得一样，只有 workflow 文件名不同：

```
https://api.github.com/repos/Furina1027/quark-share-monitor/actions/workflows/<workflow>.yml/dispatches
```

请求头：`Authorization: Bearer <GitHub token>` + `Content-Type: application/json`
（token 是 fine-grained PAT，只需 `Actions: Read and write`、只授权这一个仓库，**有效期设为不过期**）

> **⚠️ 不要改 workflow 的文件名**（`monitor.yml` / `bili-block.yml` / `nuaa-monitor.yml` / `moe-monitor.yml`）——
> job 的 URL 是写死的，改名等于四个定时器全部失效。workflow 里的 `name:` 字段随便改（不影响触发）。

---

## 2. 凭据

### API Key（操作 cron-job.org 用）

**本机没有存**（刻意不落盘）。要用时：

1. 先看 `E:\github\GitHub repo\quark-share-monitor\.cron-job-api-key` 是否存在（该文件已加入 `.gitignore`，可安全放明文）
2. 不存在就**向用户索取** —— key 在 cron-job.org 控制台的 **Settings** 页生成/查看
3. 拿到后可以顺手写进上面那个文件，省得每次问

> key 等同密码，可通过控制台限制来源 IP；**不要写进仓库、不要提交、不要贴进聊天记录长期留存**。

### GitHub token（四个 job 请求头里用的那个）

- fine-grained PAT，**只勾 `Furina1027/quark-share-monitor` 这一个仓库**，权限只有 `Actions: Read and write`
- 生成入口：GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
- **有效期务必设"不过期"**：免费版 cron-job.org 连续失败 25 次就会自动停用任务，token 一过期四个任务会陆续被停掉
- 轮换 token 后，要**逐个 PATCH 更新四个 job 的 `extendedData.headers`**（见 §4.6）

---

## 3. API 速查

- 端点：`https://api.cron-job.org`
- 认证：`Authorization: Bearer <API_KEY>`
- 有 body 的请求必须带 `Content-Type: application/json`（**少了这个头，payload 会被静默忽略**）
- **每日配额：默认 100 请求/天**（超出返回 429）—— 所以别写轮询脚本刷接口，改完 JOB 顺手核对一次就够

### 端点

| 方法 | 端点 | 用途 | 限流 |
|---|---|---|---|
| `GET` | `/jobs` | 列出全部 job | 5 次/秒 |
| `GET` | `/jobs/<jobId>` | 查单个 job 详情（**改之前先 GET 存一份**） | 5 次/秒 |
| `PUT` | `/jobs` | **创建** job（注意是 PUT，不是 POST） | **1 次/秒 且 5 次/分钟** |
| `PATCH` | `/jobs/<jobId>` | 更新 job（增量，只传要改的字段） | 5 次/秒 |
| `DELETE` | `/jobs/<jobId>` | 删除 job | 5 次/秒 |
| `GET` | `/jobs/<jobId>/history` | 执行历史（**用来验证是否真的按点触发**） | 5 次/秒 |
| `GET` | `/jobs/<jobId>/history/<identifier>` | 单条历史详情（含响应头/体） | 5 次/秒 |
| `GET/PUT/PATCH/DELETE` | `/folders...` | 分组目录（本项目没用到） | — |

### 错误码

| 码 | 含义 | 常见原因 |
|---|---|---|
| 200 | 成功 | |
| 400 | 参数无效 | schedule 字段类型写错 |
| 401 | API key 无效 | key 过期/复制少字符 |
| 403 | 来源不允许 | 控制台配了 IP 白名单，而调用方 IP 不在其中 |
| 404 | 资源不存在 | jobId 写错 / 该 job 已被删除 |
| 409 | 冲突 | folder 标题重复 |
| **429** | 配额或限流 | **当天 100 次用完**，或 PUT 超 5 次/分钟 |
| **500** | 服务端错误 | **本项目踩过：`extendedData.headers` 写成了 `[{key,value}]` 数组**，必须是字典 |

---

## 4. 常用操作

所有片段都假设：

```python
import json, os, requests

ENDPOINT = "https://api.cron-job.org"
KEY = open(r"E:\github\GitHub repo\quark-share-monitor\.cron-job-api-key").read().strip()
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

# 注意：本机 Agent 沙箱会注入连不通的假代理，跑之前必须清掉
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
s = requests.Session()
s.trust_env = False
```

### 4.1 列出全部 job

```python
r = s.get(f"{ENDPOINT}/jobs", headers=H, timeout=30)
for j in r.json()["jobs"]:
    print(j["jobId"], j["enabled"], j["title"], "|", j["schedule"]["minutes"], "|", j["url"].split("/")[-2])
```

关注字段：`enabled`（是否被自动停用）、`lastStatus`（0=没跑过、1=OK、4=HTTP 错误、5=超时）、
`nextExecution`（下次计划执行的 Unix 时间戳，秒）。

### 4.2 查单个 job（改之前先备份）

```python
job = s.get(f"{ENDPOINT}/jobs/8467350", headers=H, timeout=30).json()["job"]
print(json.dumps(job, ensure_ascii=False, indent=2))
```

### 4.3 查看执行历史 —— 判断"到底有没有按点触发"

```python
from datetime import datetime, timedelta, timezone
CST = timezone(timedelta(hours=8))
hist = s.get(f"{ENDPOINT}/jobs/8467350/history", headers=H, timeout=30).json()["history"]
for h in hist[:15]:
    t = datetime.fromtimestamp(h["date"], CST)
    planned = datetime.fromtimestamp(h["datePlanned"], CST)
    print(f'{t:%m-%d %H:%M:%S}  计划 {planned:%H:%M:%S}  抖动 {h["jitter"]}ms  '
          f'HTTP {h.get("httpStatus")}  状态 {h["status"]} {h.get("statusText","")}')
```

判读要点：
- `httpStatus == 204` = GitHub 接受了 dispatch（正常）
- `status == 1` = 执行成功；非 1 见 §6
- `jitter` 是调度抖动（毫秒），几十到几百毫秒都正常

### 4.4 改触发时间（例如把南航从第 41 分改到第 51 分）

```python
r = s.patch(f"{ENDPOINT}/jobs/8467350", headers=H, timeout=30,
            data=json.dumps({"job": {"schedule": {
                "timezone": "Asia/Shanghai",
                "hours": [-1], "minutes": [51], "mdays": [-1], "months": [-1], "wdays": [-1],
            }}}))
print(r.status_code, r.text)
```

> PATCH 是**增量**语义，但 `schedule` 是整个对象替换 —— 五个字段要给全。
> 改完记得核对：四个任务的分钟**不要撞车**（现在是 7 / 13 / 23 / 41），也别挤在整点（GitHub 整点最拥堵）。

### 4.5 暂停 / 启用

```python
s.patch(f"{ENDPOINT}/jobs/8467350", headers=H, timeout=30,
        data=json.dumps({"job": {"enabled": False}}))   # True 恢复
```

> 排查问题想临时停掉某个任务时用这个，**不要 DELETE** —— 删掉就得重建。

### 4.6 轮换 GitHub token（四 个 job 都要改）

```python
NEW_TOKEN = "github_pat_xxx"
for jid in (8467348, 8467829, 8467349, 8467350):
    r = s.patch(f"{ENDPOINT}/jobs/{jid}", headers=H, timeout=30,
                data=json.dumps({"job": {"extendedData": {
                    "headers": {"Authorization": f"Bearer {NEW_TOKEN}",
                                "Content-Type": "application/json"},
                    "body": json.dumps({"ref": "main"}),
                }}}))
    print(jid, r.status_code)
    time.sleep(0.5)   # PATCH 限 5 次/秒，稳一点
```

改完**务必**用 §5 的验证清单实测一轮，别只看返回 200。

### 4.7 新建一个 job（加了新的监控任务时）

```python
r = s.put(f"{ENDPOINT}/jobs", headers=H, timeout=30, data=json.dumps({"job": {
    "title": "任务名（中文可读）",
    "url": "https://api.github.com/repos/Furina1027/quark-share-monitor/actions/workflows/<新workflow>.yml/dispatches",
    "enabled": True,
    "saveResponses": True,          # 存响应体，方便排查
    "requestMethod": 1,             # 1 = POST（0=GET,1=POST,4=PUT,8=PATCH）
    "schedule": {
        "timezone": "Asia/Shanghai",
        "hours": [-1], "minutes": [53], "mdays": [-1], "months": [-1], "wdays": [-1],
    },
    "extendedData": {
        "headers": {"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"},
        "body": json.dumps({"ref": "main"}),
    },
    "notification": {"onFailure": True, "onFailureCount": 3, "onDisable": True},
}))
print(r.status_code, r.json())   # 期望 {"jobId": ...}
```

要点：
- **是 `PUT /jobs`**，body 最外层必须包一层 `{"job": {...}}`，只有 `url` 是必填字段
- `minutes` 只能用 `-1`（每分钟）或单个值/列表；本项目习惯"每小时固定某分钟" → `hours: [-1]` + `minutes: [53]`
- **创建限流 1 次/秒、5 次/分钟**，批量建要 `sleep`
- `extendedData.headers` **必须是字典**，写成数组会 500
- 顺手把 `notification.onFailureCount` 设成 3、`onDisable` 设成 true，任务被自动停用时会收到邮件提醒

### 4.8 删除 job

```python
s.delete(f"{ENDPOINT}/jobs/8467350", headers=H, timeout=30)
```

---

## 5. 改完必做的验证清单

改配置（换 token、改时间、新建 job）之后**一定要实测**，不能只看 HTTP 200：

1. **手动触发一次 dispatches**，确认 GitHub 侧能建 run：
   ```bash
   curl -X POST -H "Authorization: Bearer <GitHub token>" -H "Accept: application/vnd.github+json" \
        https://api.github.com/repos/Furina1027/quark-share-monitor/actions/workflows/nuaa-monitor.yml/dispatches \
        -d '{"ref":"main"}'
   # 期望 HTTP 204
   ```
2. **等目标分钟过去**（比如改到 51 分，就等到 :51），然后查 §4.3 的执行历史，
   确认出现一条 `httpStatus=204`、`status=1` 的记录
3. **去 GitHub 确认 run 真的建了**：
   ```bash
   gh run list -R Furina1027/quark-share-monitor --limit 10
   # 关注 event 列是否为 workflow_dispatch，created_at 是否落在目标分钟
   ```
4. 确认**邮件侧没有异常**（不该发的别发）：脚本"无变化就静默"，如果突然收到一封说明真检测到东西了

---

## 6. 故障排查

| 症状 | 原因 | 处置 |
|---|---|---|
| GitHub 完全没建 run，job 历史里 httpStatus 是 401 | GitHub token 失效/被撤销 | 生成新 token → §4.6 更新四个 job → §5 验证 |
| httpStatus 是 403 | 控制台配了 IP 白名单，或 token 权限不够 | 检查 token 是否只勾了这一个仓库 + `Actions: Read and write` |
| job 的 `enabled` 变成 `false`，自己停了 | **连续失败 25 次自动停用**（token 过期常见） | 修好 token 后 §4.5 重新 `enabled: True` |
| 历史里 `status=4`（HTTP 错误）/ `5`（超时） | dispatches 接口返回非 2xx，或网络超时 | 先手动 curl 复现（§5.1），再查 token / 仓库名 |
| 触发时间老是偏几分钟 | cron-job.org 调度抖动（`jitter`） | 正常现象，不必处理 |
| 调 API 返回 500 | **`extendedData.headers` 写成了数组** | 改成字典 `{"Header": "Value"}` |
| 调 API 返回 429 | 当天 100 次配额用完 / PUT 超 5 次每分钟 | 等次日或用 PATCH 代替（PATCH 限流松得多） |
| 收到 GitHub Actions 失败邮件 | 脚本本身出错，不是调度问题 | 看 run 日志。**用户明确说过：Actions 失败 GitHub 会自己发邮件，不要额外加失败告警逻辑** |

---

## 7. 禁区

1. **不要改 workflow 文件名** —— 打卡位都是按文件名写死的 URL
2. **不要 DELETE 现有 job** 再重建 —— 想停就 `enabled: false`，删了容易漏掉某个字段导致行为不一致
3. **不要把 API Key 提交进仓库** —— 放 `.cron-job-api-key`（已 gitignore）或跟用户要
4. **不要把分钟改到整点附近**（GitHub 整点最拥堵），也别让四个任务撞在同一分钟
5. **不要写轮询脚本刷 API** —— 每天只有 100 次配额
6. **不要在触发时间上"加保险"多建几条 job** —— 重复触发虽然不会导致重复邮件，但会白耗 Actions 时间和 API 配额

---

## 8. 字段速查

### schedule

| 字段 | 类型 | 说明 |
|---|---|---|
| `timezone` | string | 本项目统一用 `Asia/Shanghai`（不写默认 UTC，**容易差 8 小时**） |
| `hours` | int[] | 0-23；`[-1]` = 每小时 |
| `minutes` | int[] | 0-59；`[-1]` = 每分钟 |
| `mdays` | int[] | 1-31；`[-1]` = 每天 |
| `months` | int[] | 1-12；`[-1]` = 每月 |
| `wdays` | int[] | 0=周日 … 6=周六；`[-1]` = 每天 |
| `expiresAt` | int | 格式 `YYYYMMDDhhmmss`，`0` = 不过期 |

### requestMethod

`0`=GET `1`=POST `2`=OPTIONS `3`=HEAD `4`=PUT `5`=DELETE `6`=TRACE `7`=CONNECT `8`=PATCH
（本项目四个 job 都是 `1`）

### JobStatus（`lastStatus` / 历史里的 `status`）

`0` 未知/未执行 · `1` OK · `2` DNS 错误 · `3` 连不上主机 · `4` HTTP 错误 · `5` 超时 ·
`6` 响应数据过多 · `7` 无效 URL · `8` 内部错误 · `9` 未知

### 其它字段

| 字段 | 说明 |
|---|---|
| `saveResponses` | 是否保存响应头和响应体（排查时有用，**建议开**） |
| `requestTimeout` | 秒；`-1` = 用默认（30 秒）。dispatch 接口秒级返回，不用改 |
| `redirectSuccess` | 3xx 是否算成功，默认 false |
| `notification.onFailureCount` | 失败几次后发通知（最小 1），建议设 3 避免偶发抖动 |
| `notification.onDisable` | 被自动停用时通知，**建议开**（否则任务默默死掉）
