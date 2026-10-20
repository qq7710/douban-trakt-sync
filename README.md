# 豆瓣「想看」 → Trakt「待看」 自动同步

> ## 💡 配合 ReX / ExP 这类「带 Trakt 待看清单」的播放器使用，效果更佳
>
> ## 🤖 AI 产物 —— 别问我怎么用，问 AI 去吧
>
> （作者不提供使用指导，遇到问题请把报错丢给 AI）

---

每隔一段时间检查你的豆瓣「想看」列表，把**新出现**的条目自动加进 Trakt「待看」（watchlist）。
纯 Python，无数据库，无 Agent，适合丢在 NAS / 服务器 / Docker 上长期跑。

---

## 它能做什么 / 不做什么

**做：**

- 定时把豆瓣新增的「想看」同步到 Trakt 待看
- **准实时模式**：守护进程每 30 秒探一次，新增想看后最多半分钟就同步过去
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

然后**一条命令完成部署**：

```bash
cd /你的/项目目录 && bash nas_setup.sh
```

它会自动：检测 python3 → 建虚拟环境装依赖 → 设权限 → **写入 crontab（每 10 分钟）** → 立刻试跑一次并打印日志。
脚本是幂等的，重复执行不会重复写 crontab。

---

## 首次授权 Trakt（只需一次，约 1 分钟）

```bash
# 方式 A：本地回调（在能开浏览器的机器上用，最简单）
python3 auth_trakt.py --config config.json

# 方式 B：粘贴模式（SSH 到 NAS 等无浏览器环境时用）
python3 auth_trakt.py --config config.json --mode paste
```

**方式 A**：脚本会打印一个链接 → 浏览器打开 → 登录 Trakt → 点 **Allow** → 自动完成，
页面显示"授权成功"即可关闭。

**方式 B**：脚本打印链接 → 你在**自己电脑**的浏览器打开并点 Allow →
浏览器会跳到一个打不开的 `localhost:5173/callback?code=...` 页面（**这是正常的**）→
把地址栏**完整 URL** 复制，粘贴回终端回车 → 完成。

成功后生成 `token.json`。**该令牌有效期 7 天，脚本会自动续期，之后不用再管。**

---

## 部署方式

### 方式 1：crontab（最通用）

`nas_setup.sh` 已经帮你写好了。手动查看/修改：

```bash
crontab -l
# */10 * * * * /bin/bash /你的目录/run_sync.sh >> /你的目录/cron.log 2>&1
```

改频率：把 `*/10` 改成 `*/30`（30 分钟）或 `*/5`（5 分钟）。

### 方式 2：准实时守护进程（推荐，新增后 ~30 秒内同步）

cron 是"到点就全量跑一遍"，有点笨。想要"一加想看就马上同步"，用守护进程：

```bash
chmod +x watch_sync.sh
./watch_sync.sh start      # 后台常驻
./watch_sync.sh status     # 看是否在跑
./watch_sync.sh log        # 实时看日志（Ctrl+C 退出，不会杀进程）
./watch_sync.sh stop       # 停止
```

它做的事：

- 启动时先跑一次全量，把停机期间漏掉的补上，并给"已知条目"打底
- 之后**每 30 秒只拉最新 5 条想看（1 次请求）**，一旦发现没见过的条目 id → 立刻触发完整同步
- 连续 30 分钟没动静，也会跑一次兜底全量（顺便重试冷却结束的失败条目）
- 探测失败会自动退避（30→60→…→最多 600 秒），不会把豆瓣惹毛

静默期开销约 **2880 次请求/天**，每次只有几十字节，比"每 10 分钟全量跑一次"还省。

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

> 换成守护进程后，原来 `*/10` 那条全量 cron **可以删掉**（或留着做双保险，重复运行无害）。

### 方式 3：NAS 自带的「任务计划」类应用

飞牛 fnOS 可在**应用中心**安装「任务计划」；群晖在**控制面板 → 任务计划**。

| 项 | 填 |
|---|---|
| 任务名称 | 豆瓣想看→Trakt待看 |
| 执行用户 | root |
| 触发方式 | 定时 |
| Cron | `*/10 * * * *`（**注意是 5 个字段，小时位用 `*`**） |
| 脚本内容 | `bash /你的目录/run_sync.sh` |

> ⚠️ 常见坑：Cron 填成 `*/10 0 * * *` 表示"只在凌晨 0 点这一小时里每 10 分钟"，
> 结果一天只跑几次。正确写法是 `*/10 * * * *`。

### 方式 4：Docker

```bash
touch synced.json          # 必须先创建，否则 Docker 会把它建成目录
docker compose up -d --build
```

频率在 `Dockerfile` 的 `sleep 600`（秒）里改，改完重新 `docker compose up -d --build`。
看日志：`docker compose logs -f`

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
| `watch.interval` | `30` | 守护进程轮询间隔（秒）。怕风控就改 60 |
| `watch.probe_count` | `5` | 每轮拉最新几条来判断有没有新增 |
| `watch.full_sync_minutes` | `30` | 静默多久后跑一次兜底全量 |
| `watch.state_file` | `watch_state.json` | 守护进程状态（已知条目 id） |

---

## 工作原理（想了解再看）

1. **抓取**：豆瓣移动端 `interests` 接口，带 `status=mark` 只取「想看」（1 次请求）
2. **匹配**：优先取豆瓣条目的 IMDb 号；取不到时用豆瓣原名/中文标题在 Trakt 搜索
   （带 `fields=title,translations,aliases`，中文译名和别名也能命中）
3. **去重**：查本地 `synced.json`，已推过的直接跳过
4. **提交**：`POST /sync/watchlist`，幂等
5. **记录**：只有提交成功才写入清单，失败的下轮会重试

**关于"看完会不会被塞回待看"**：Trakt 的待看列表在你看完第 1 集/电影后会自动移除该条目。
如果没有本地清单，工具下一轮会发现"豆瓣想看里有、Trakt 待看里没有"，于是又推一遍 —— 无限循环。
本地清单就是为了解决这个：**每个豆瓣条目只推一次**。

---

## 常见问题

**抓到 0 条想看？**
豆瓣列表可能被设为私密或被反爬拦截 → 在 `config.json` 的 `douban.cookie` 填入浏览器 Cookie。

**提交报 401？**
token 失效。重新跑一次 `auth_trakt.py`。

**某部片匹配不上？**
多为 Trakt 上根本没有该条目（尤其是没有 IMDb 号的小众国产片）。
这类条目会尝试 3 次后进入 48 小时冷却，不会每轮白跑；Trakt 以后收录了会自动补上。

**想让它全部重推一次？**
```bash
python3 wishlist_sync.py --config config.json --force
```
（或删掉 `synced.json`。**注意：这会把你看完的条目也重新推回待看**）

**只想预览不提交？**
```bash
python3 wishlist_sync.py --config config.json --dry-run
```

**看不到日志？**
```bash
tail -f /你的目录/sync.log      # 同步日志
tail -f /你的目录/watch.log     # 守护进程的 console 输出
tail -f /你的目录/cron.log      # cron 的输出与报错
```

**想手动触发一次？**
```bash
python3 wishlist_sync.py --config config.json          # 全量同步一次
./watch_sync.sh once                                    # 只探测一次，有新增才同步
```

**会不会把豆瓣/Trakt 访问太频繁？**
全量同步一次约 1（列表）+ N（匹配）次请求；守护进程静默期每轮只有 **1 次**极小的探测请求。
默认 30 秒一轮 ≈ 2880 次/天，全是 `m.douban.com` 的同一个接口，量级很低。
真被风控了脚本会自动退避，加大 `watch.interval` 到 60 即可。

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `wishlist_sync.py` | 主脚本 |
| `auth_trakt.py` | Trakt 一次性授权（生成 token.json） |
| `nas_setup.sh` | 一键部署（装依赖 + 写 crontab + 试跑） |
| `run_sync.sh` | 定时任务调用的包装脚本 |
| `watch_sync.py` | 准实时守护进程（轻量轮询 + 事件触发） |
| `watch_sync.sh` | 守护进程启停脚本（start/stop/restart/status/log/once/ensure） |
| `douban-trakt-watch.service` | systemd unit（可选） |
| `config.example.json` | 配置模板 |
| `requirements.txt` | 依赖：requests + certifi |
| `Dockerfile` / `docker-compose.yml` | Docker 部署（可选） |

> 🔒 请勿分享 `config.json` 和 `token.json`，它们包含你的个人凭据。

---

## 许可

MIT
