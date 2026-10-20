# 豆瓣「想看」 → Trakt「待看」 自动同步

> ## 💡 配合 ReX / ExP 这类「带 Trakt 待看清单」的播放器使用，效果更佳
>
> ## 🤖 AI 产物 —— 别问我怎么用，问 AI 去吧
>
> （作者不提供使用指导，遇到问题请把报错丢给 AI）

---

盯着你的豆瓣「想看」列表，把**新出现**的条目自动加进 Trakt「待看」（watchlist）。

采用**探测模式**：平时每轮只花 1 次请求看一眼有没有新增，有新增才做完整同步 ——
在豆瓣点下「想看」后 **1 分钟内**（守护进程模式 30 秒内）就出现在 Trakt 待看里。

纯 Python，无数据库，无 Agent，适合丢在 NAS / 服务器 / Docker 上长期跑。

---

## 它能做什么 / 不做什么

**做：**

- 准实时把豆瓣新增的「想看」同步到 Trakt 待看（探测模式，不是定时全量）
- 无新增时几乎零开销：每轮只有 1 次极小请求
- 自动匹配条目（优先 IMDb 号，拿不到时用豆瓣原名去 Trakt 搜，含译名与别名）
- 幂等：同一个条目只会推一次，重复运行不会重复添加

**不做：**

- ❌ **不会**把你"看完"的条目塞回待看（Trakt 看完会自动移出待看，本工具靠本地清单记住"已推过"，不会重复推）
- ❌ 不会改动豆瓣、不会删除任何东西
- ❌ 不会同步"看过"历史，只同步"想看 → 待看"

---

## 开始前需要准备三样东西

### 1. 豆瓣数字 ID

打开你的豆瓣个人主页，URL 形如 `https://www.douban.com/people/12345678/`，
其中 `12345678` 就是用户 ID。

### 2. Trakt 应用的 client_id

1. 打开 <https://trakt.tv/oauth/applications> → **New Application**
2. 名称随便填
3. **Redirect URI 填**：`http://localhost:5173/callback`
4. 保存后复制 **Client ID**

> **不需要 client_secret**。本工具用的是 PKCE 授权码流程，公共客户端即可。

### 3. 一台能跑 Python 3 的机器

NAS（群晖/飞牛/UNRAID）、Linux 服务器、VPS、有 Docker 的机器都行。

---

## 快速开始（推荐：一条命令部署）

```bash
# 1) 把本目录整个放到你的机器上，例如
#    /opt/douban-trakt-sync  或  NAS 上任意目录，如 /volume1/docker/douban-trakt-sync

# 2) 复制配置模板并填写
cp config.example.json config.json
```

编辑 `config.json`：

```json
{
  "douban": {
    "user_id": "你的豆瓣数字ID",
    "cookie": ""
  },
  "trakt": {
    "client_id": "你的 Trakt Client ID",
    "client_secret": "",
    "token_file": "token.json"
  },
  "sync": {
    "request_interval": 0.8,
    "dry_run": false,
    "log_file": "sync.log",
    "fuzzy_match": true,
    "cross_type_fallback": false,
    "state_file": "synced.json",
    "max_attempts": 3,
    "retry_after_hours": 48
  },
  "watch": {
    "interval": 30,
    "probe_count": 5,
    "full_sync_minutes": 30,
    "state_file": "watch_state.json"
  }
}
```

> `cookie` 一般留空即可。只有当你把豆瓣列表设为私密、抓不到条目时才需要填浏览器 Cookie。

然后**一条命令完成部署**（需要能敲命令行的环境）：

```bash
cd /你的/项目目录 && bash nas_setup.sh
```

它会自动：检测 python3 → 建虚拟环境装依赖 → 设权限 →
**写入定时任务（每分钟跑一次探测）** → 立刻试跑一次并打印日志。
脚本是幂等的，重复执行不会重复写 crontab；旧版本留下的「每 10 分钟全量」任务会被自动清理。

只有图形界面（比如 fnOS 的「任务计划」）？直接跳到
[方式 1：任务计划每分钟跑一次](#方式-1任务计划--cron-每分钟跑一次推荐不用命令行)，**不用敲命令行**。

---

## 首次授权 Trakt（只需一次，约 1 分钟）

```bash
# 方式 A：本地回调（在能开浏览器的机器上用，最简单）
python3 sync.py auth

# 方式 B：粘贴模式（SSH 到 NAS 等无浏览器环境时用）
python3 sync.py auth --mode paste
```

> 老版等价命令是 `python3 auth_trakt.py --config config.json`，效果一样。

**方式 A**：脚本会打印一个链接 → 浏览器打开 → 登录 Trakt → 点 **Allow** → 自动完成，
页面显示"授权成功"即可关闭。

**方式 B**：脚本打印链接 → 你在**自己电脑**的浏览器打开并点 Allow →
浏览器会跳到一个打不开的 `localhost:5173/callback?code=...` 页面（**这是正常的**）→
把地址栏**完整 URL** 复制，粘贴回终端回车 → 完成。

成功后生成 `token.json`。**该令牌有效期 7 天，脚本会自动续期，之后不用再管。**

---

## 📦 单文件版 `sync.py`（新装推荐）

从这一版起，授权 / 全量同步 / 探测 / 常驻守护**全部合并进了一个文件 `sync.py`**。
新装只要两个文件：

```
sync.py
config.json        ← 复制 config.example.json 后填写
```

（`token.json` / `synced.json` / `watch_state.json` / `sync.log` 由脚本自己生成，不用管。）

```bash
python sync.py auth              # 首次授权 Trakt（只需一次）
python sync.py once              # 探测一次：有新增才同步（给定时任务每分钟调用）
python sync.py run               # 手动全量跑一次
python sync.py watch --daemon    # 后台常驻，30 秒一轮
python sync.py status            # 看守护进程在不在
python sync.py stop              # 停掉守护进程
python sync.py selftest          # 离线自检
```

几个省事的设计：

- **自动切到脚本所在目录**：从任何目录调用，`config.json` / `token.json` / 日志都找得到
- **自动找虚拟环境**：系统 python 没装 requests 时自动改用项目里的 `.venv` 重跑，
  不用先 `source venv/bin/activate`
- **自带互斥锁**：`once` 被每分钟调用时，上一轮没跑完就跳过，不会堆积
  （用 Python 实现，不再依赖 shell 的 `trap` —— 之前踩过 `exec` 让 `trap` 永不执行的坑）
- 不需要 `chmod +x`

> 老版本的多文件（`wishlist_sync.py` / `watch_sync.py` / `auth_trakt.py` / 各种 `.sh`）
> **继续保留、功能完全一致**，已经在跑的不用动。新装建议直接用 `sync.py`。

---

## 部署方式

> 先说清楚：这个项目**不是**"定时全量跑一遍"。它跑的是**探测模式** ——
> 平时只花 1 次请求看一眼有没有新东西，有新增才做完整同步。下面三种方式
> 只是"谁来负责触发这次探测"的区别，同步逻辑完全一样。

### 方式 1：任务计划 / cron 每分钟跑一次（推荐，不用命令行）

如果你只有 NAS 的图形界面（比如飞牛 fnOS 应用中心的「任务计划」；群晖在
**控制面板 → 任务计划**），填这张表就行，**不需要开守护进程**：

| 项 | 填 |
|---|---|
| 任务名称 | 豆瓣想看→Trakt待看 |
| 执行用户 | root |
| 触发方式 | 定时 |
| Cron | `* * * * *`（每分钟一次，5 个字段） |
| 脚本内容 | `/你的目录/.venv/bin/python /你的目录/sync.py once` |

（`/你的目录` 换成实际路径；没建虚拟环境、requests 装在系统 python 里的话写
`python3 /你的目录/sync.py once`）

`once` = 「探测一次，有新增才同步」。静默期每轮只有 1 次请求，
新增想看后 **1 分钟内**同步。自带互斥锁，上一轮没跑完不会堆积。

> 老版写法是 `bash /你的目录/watch_sync.sh once`，效果一样。

> ⚠️ 常见坑：Cron 填成 `*/10 0 * * *` 表示"只在凌晨 0 点这一小时里每 10 分钟"，
> 结果一天只跑几次。要每分钟就是 `* * * * *`。

命令行环境下等价的写法：

```bash
* * * * * /你的目录/.venv/bin/python /你的目录/sync.py once >> /你的目录/sync.log 2>&1
```

> 想更快（30 秒内）就用方式 2 的守护进程（`python sync.py watch --daemon`）。

### 方式 2：守护进程常驻（新增后 ~30 秒内同步）

想连"每分钟触发"这道工序都省掉，让进程自己盯着：

```bash
chmod +x watch_sync.sh
./watch_sync.sh start      # 后台常驻
./watch_sync.sh status     # 看是否在跑
./watch_sync.sh log        # 实时看日志（Ctrl+C 退出，不会杀进程）
./watch_sync.sh stop       # 停止
```

用单文件版的话连 shell 脚本都不需要（它自己会后台化、自己写 pid 和 watch.log）：

```bash
python sync.py watch --daemon   # 后台常驻
python sync.py status           # 是否在跑
python sync.py stop             # 停掉
```

它做的事：

- 启动时先跑一次全量，把停机期间漏掉的补上，并给"已知条目"打底
- 之后**每 30 秒只拉最新 5 条想看（1 次请求）**，一旦发现没见过的条目 id → 立刻触发完整同步
- 连续 30 分钟没动静，也会跑一次兜底全量（顺便重试冷却结束的失败条目）
- 探测失败会自动退避（30→60→…→最多 600 秒），不会把豆瓣惹毛

静默期开销约 **2880 次请求/天**，每次只有几十字节，比老版"每 10 分钟全量跑一次"还省。

> **关于"能不能真的监听"**：不能。豆瓣开放 API 多年前已关闭，也没有「想看变更」的
> Webhook，做不到真正的事件推送。上面这种"极轻量轮询 + 事件触发"是能达到的最接近方案。

让它开机自启 + 挂了自动拉起（cron 当看门狗）：

```bash
crontab -e
@reboot /bin/bash /你的目录/watch_sync.sh start >> /你的目录/watch.log 2>&1
*/5 * * * * /bin/bash /你的目录/watch_sync.sh ensure >> /你的目录/watch.log 2>&1
```

`ensure` 是幂等的：已在跑就静默退出，没在跑才拉起来。

有 systemd 的机器也可以用自带的 unit：

```bash
sudo cp douban-trakt-watch.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/douban-trakt-watch.service   # 改里面的路径
sudo systemctl daemon-reload && sudo systemctl enable --now douban-trakt-watch
```

> 用了守护进程就**别再留**方式 1 那条每分钟 cron，否则两套会重复探测
> （不会重复添加条目，只是多花一次请求）。

### 方式 3：Docker

```bash
touch synced.json watch_state.json   # 必须先创建，否则 Docker 会把它建成目录
docker compose up -d --build
```

容器内跑的就是探测模式（默认 30 秒一轮）。改频率改 `config.json` 的 `watch.interval`，
改完重新 `docker compose up -d --build`。看日志：`docker compose logs -f`

---

## 配置项说明

| 键 | 默认 | 说明 |
|---|---|---|
| `douban.user_id` | — | 豆瓣数字 ID |
| `douban.cookie` | `""` | 列表私密时才需要填 |
| `trakt.client_id` | — | Trakt 应用 Client ID |
| `sync.request_interval` | `0.8` | 每次请求间隔（秒），防风控 |
| `sync.dry_run` | `false` | 设为 true 则只预览不提交 |
| `sync.log_file` | `sync.log` | 日志（5MB 自动轮转，保留 3 个） |
| `sync.fuzzy_match` | `true` | IMDb 拿不到时用原名/中文名搜 Trakt。**建议保持开启** |
| `sync.cross_type_fallback` | `false` | 电影/剧集互搜，容易误匹配，建议关 |
| `sync.state_file` | `synced.json` | 已同步清单 |
| `sync.max_attempts` | `3` | 匹配失败几次后转入冷却 |
| `sync.retry_after_hours` | `48` | 冷却时长（小时） |
| `sync.network_retries` | `1` | 网络瞬断时额外重试几次（间隔 3 秒、6 秒…） |
| `watch.interval` | `30` | 守护进程轮询间隔（秒）。怕风控就改 60 |
| `watch.probe_count` | `5` | 每轮拉最新几条来判断有没有新增 |
| `watch.full_sync_minutes` | `30` | 静默多久后跑一次兜底全量 |
| `watch.state_file` | `watch_state.json` | 守护进程状态（已知条目 id） |

---

## 工作原理（想了解再看）

1. **探测**（`sync.py once` / `sync.py watch`）：每 N 秒拉一次豆瓣最新 5 条想看（1 次请求），
   比对 `watch_state.json` 里记过的 id，没见过的才触发下面的完整同步
2. **抓取**：豆瓣移动端 `interests` 接口，带 `status=mark` 只取「想看」（1 次请求）
3. **匹配**：优先取豆瓣条目的 IMDb 号；取不到时用豆瓣原名/中文标题在 Trakt 搜索
   （带 `fields=title,translations,aliases`，中文译名和别名也能命中）
4. **去重**：查本地 `synced.json`，已推过的直接跳过
5. **提交**：`POST /sync/watchlist`，幂等
6. **记录**：只有提交成功才写入清单，失败的下轮会重试；
   匹配失败 3 次后进入 48 小时冷却，不会每轮白跑

> 网络瞬断（`Network is unreachable` 之类）会在这一步自动重试，见 FAQ。

**关于"看完会不会被塞回待看"**：Trakt 的待看列表在你看完第 1 集/电影后会自动移除该条目。
如果没有本地清单，工具下一轮会发现"豆瓣想看里有、Trakt 待看里没有"，于是又推一遍 —— 无限循环。
本地清单就是为了解决这个：**每个豆瓣条目只推一次**。

---

## 常见问题

**抓到 0 条想看？**
豆瓣列表可能被设为私密或被反爬拦截 → 在 `config.json` 的 `douban.cookie` 填入浏览器 Cookie。

**提交报 401？**
token 失效。重新跑一次 `python3 sync.py auth`。

**某部片匹配不上？**
多为 Trakt 上根本没有该条目（尤其是没有 IMDb 号的小众国产片）。
这类条目会尝试 3 次后进入 48 小时冷却，不会每轮白跑；Trakt 以后收录了会自动补上。

**想让它全部重推一次？**
```bash
python3 sync.py run --force
```
（或删掉 `synced.json`。**注意：这会把你看完的条目也重新推回待看**）

**只想预览不提交？**
```bash
python3 sync.py run --dry-run
```

**看不到日志？**
```bash
tail -f /你的目录/sync.log      # 同步日志（匹配结果、提交结果）
tail -f /你的目录/watch.log     # 探测/守护进程的 console 输出与报错
```

**想手动触发一次？**
```bash
python3 sync.py run       # 全量同步一次
python3 sync.py once      # 只探测一次，有新增才同步
```

**会不会把豆瓣/Trakt 访问太频繁？**
不会。静默期每轮只有 **1 次**极小的探测请求（只拉最新 5 条的 id），
发现新增才做完整同步。默认节奏约 2880 次/天（守护进程）或 1440 次/天（每分钟一次），
全是 `m.douban.com` 的同一个接口，量级很低。真被风控了脚本会自动退避，
把 `watch.interval` 调到 60 即可。

**会不会占掉我的 Trakt 第三方应用名额？**
会占 **1 个**。Trakt 免费账号只能连 1 个社区应用（第三方 OAuth 应用），
VIP 才不限。官方应用和网页版不占名额。注意事项：

- 别在 Trakt **Settings → Apps → Connected Apps** 里随手 revoke，重连会被当作新授权并受限额约束
- token 每 7 天自动续期，只要 NAS 别长期关机就永远不需要重新授权
- 真失效了：先撤销旧的腾出格子，再跑 `python sync.py auth`（老版是 `auth_trakt.py`）
- 自己的多个脚本**共用同一个 client_id** 只算一个连接

**没有「新增想看」的日志，是不是坏了？**
不是。静默期每次只打一行 `探测一次，豆瓣用户 xxx`，没有 `🆕` 就说明豆瓣侧确实没新增。
想验证就去豆瓣随手加一条想看，一分钟内应该出现 `🆕 发现新增想看`。

**日志里出现 `Network is unreachable` / `请求失败 ... Max retries exceeded`？**

这是**网络层**的报错，跟脚本和豆瓣都没关系 —— 那一刻这台机器本身连不上外网。常见原因：

- NAS 刚好在重启 / 断网 / 从休眠唤醒（网卡还没起来）
- 有 IPv6 地址但上游没有 IPv6 路由 → 解析到 IPv6 后直接 `Errno 101`
- 任务计划在开机瞬间就触发，此时网络还没就绪

`sync.py` 遇到这种情况会自动重试（默认再试 1 次，间隔 3 秒），仍失败就等下一轮，
**不会漏条目**。偶发几行不用管；如果一直刷同样的报错，再去查这台机器的网络。

---

## 文件清单

| 文件 | 作用 |
|---|---|
| **`sync.py`** | **单文件版，新装推荐**：授权 / 全量 / 探测 / 常驻全在这一个文件里 |
| `wishlist_sync.py` | 老版主脚本（`sync.py` 已包含它，保留兼容） |
| `auth_trakt.py` | 老版 Trakt 授权脚本（`sync.py auth` 等价，保留兼容） |
| `watch_sync.py` | 老版守护进程（`sync.py watch` 等价，保留兼容） |
| `watch_sync.sh` | 老版守护进程启停脚本（start/stop/status/log/once/ensure） |
| `nas_setup.sh` | 一键部署（装依赖 + 写每分钟探测的定时任务 + 试跑） |
| `run_sync.sh` | 手动跑一次全量的包装脚本（日常用不上，排障时用） |
| `douban-trakt-watch.service` | systemd unit（可选） |
| `config.example.json` | 配置模板 |
| `requirements.txt` | 依赖：requests + certifi |
| `Dockerfile` / `docker-compose.yml` | Docker 部署（可选） |

> 🔒 请勿分享 `config.json` 和 `token.json`，它们包含你的个人凭据。

---

## 许可

MIT
