#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆瓣「想看」 -> Trakt「待看(watchlist)」 同步工具（单文件，全部功能都在这里）

子命令
------
    sync.py auth              一次性授权 Trakt（PKCE，不需要 client_secret）
    sync.py run               跑一次全量同步（默认）
    sync.py once              探测一次：只在发现新增想看时才同步（给定时任务每分钟调用）
    sync.py watch             常驻探测，默认 30 秒一轮
    sync.py watch --daemon    后台常驻
    sync.py status            看看后台守护进程在不在
    sync.py selftest          离线自检（不发网络请求）

常用参数
--------
    --config config.json      （默认就是 config.json，相对路径按本文件所在目录算）
    --dry-run                 只预览不提交
    --force                   run 时忽略已同步清单，全量重推（慎用：会把看完的也推回待看）
    --interval 60             watch 的轮询间隔（秒）
    --mode paste              auth 时改用"手动粘贴回调 URL"模式

设计要点
--------
1. 抓取：豆瓣 m 端 rexxar API，带 status=mark 只取「想看」（1 次请求）。
   合法值 mark=想看 / doing=在看 / done=看过；别传 'wish'（非法值，会被忽略返回全部）。
2. 匹配：优先取豆瓣条目的 IMDb 号；取不到时用「豆瓣原名 → 中文标题」在 Trakt 搜索兜底
   （带 fields=title,translations,aliases，中文译名和别名也能命中）。
3. 去重：本地 synced.json 记录每个豆瓣条目只推一次 —— 这样你在 Trakt 看完被移出待看后，
   豆瓣的想看不会被再次塞回来。
4. 降频：匹配失败的条目失败 3 次后进入 48 小时冷却，不会每轮白跑。
5. 准实时：豆瓣没有 webhook（开放 API 早就关了），做不到真事件推送。
   折中方案是利用 interests 接口「按 create_time 倒序返回」+ 支持 count 参数的特性，
   每轮只拉最新 5 条（1 次几十字节的请求），一旦发现没见过的 id 就立刻触发全量同步。
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

# 无论从哪个目录被调用，都把工作目录切到脚本所在目录，
# 这样 config.json / token.json / synced.json / sync.log 一律按相对路径就能找到（定时任务里尤其重要）
os.chdir(HERE)


def _bootstrap_venv():
    """如果当前 python 里没装 requests，但项目里有 .venv，就自动换 .venv 里的 python 重跑一次。

    省掉"必须先 source venv/bin/activate"这一步 —— 定时任务直接写 python3 sync.py once 也能跑。"""
    if os.environ.get("_DBT_REEXEC") == "1":
        return
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass
    for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
        cand = os.path.join(HERE, ".venv", rel)
        if os.path.exists(cand) and os.path.abspath(cand) != os.path.abspath(sys.executable):
            env = dict(os.environ)
            env["_DBT_REEXEC"] = "1"
            import subprocess
            sys.exit(subprocess.call([cand, os.path.abspath(__file__)] + sys.argv[1:], env=env))


_bootstrap_venv()

import requests  # noqa: E402
import certifi   # noqa: E402


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logging(log_file):
    logger = logging.getLogger("douban_trakt")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_file:
        try:
            # 自动轮转，避免 NAS 上日志无限增长（5MB/个，保留 3 个备份）
            fh = RotatingFileHandler(log_file, encoding="utf-8",
                                     maxBytes=5 * 1024 * 1024, backupCount=3)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except Exception as e:
            print("[WARN] 无法写日志文件 %s: %s" % (log_file, e))
    return logger


log = logging.getLogger("douban_trakt")

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# 配置 / 状态
# ---------------------------------------------------------------------------
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _abs(path):
    return path if os.path.isabs(path) else os.path.join(HERE, path)


def load_state(path):
    """返回 {豆瓣subject_id: {status,title,imdb,type,synced_at|attempts,last_attempt_at}}。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        log.warning("已同步状态文件读取失败（%s），按空处理: %s", path, e)
        return {}
    if not isinstance(d, dict):
        return {}
    items = d.get("items")
    return items if isinstance(items, dict) else {}


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_state(path, items):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": items}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("写入已同步状态失败 %s: %s", path, e)


def record_synced(path, state, matched):
    """把本次成功提交的条目记进状态，之后不再重复推送。"""
    now = _now_iso()
    n = 0
    for m in matched:
        sid = m.get("douban_id")
        if not sid:
            continue
        state[sid] = {
            "status": "synced",
            "title": m.get("title"),
            "imdb": m.get("id_value"),
            "type": m.get("type"),
            "synced_at": now,
        }
        n += 1
    if n:
        save_state(path, state)
        log.info("已记录 %d 条到已同步清单（%s），后续不再重复推送", n, path)


# ---------------------------------------------------------------------------
# Trakt token
# ---------------------------------------------------------------------------
def load_token(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_token(token, path):
    token = dict(token)
    token.setdefault("obtained_at", int(time.time()))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=4, ensure_ascii=False)
    log.info("Trakt token 已保存到 %s", path)


def refresh_token(token, client_id, client_secret):
    rt = token.get("refresh_token")
    if not rt:
        return None
    payload = {"refresh_token": rt, "client_id": client_id, "grant_type": "refresh_token"}
    if client_secret:  # PKCE 公共客户端没有 secret，留空即可
        payload["client_secret"] = client_secret
    try:
        r = requests.post("https://api.trakt.tv/oauth/token", json=payload,
                          timeout=REQUEST_TIMEOUT, verify=certifi.where())
        if r.status_code == 200:
            new = r.json()
            new["refresh_token"] = new.get("refresh_token") or rt
            return new
    except Exception as e:
        log.error("刷新 token 失败: %s", e)
    return None


def get_access_token(config):
    tf = _abs(config["trakt"]["token_file"])
    token = load_token(tf)
    if not token or not token.get("access_token"):
        log.error("找不到有效的 Trakt token（%s）。请先运行: python sync.py auth", tf)
        return None
    exp = token.get("expires_in")
    obtained = token.get("obtained_at")
    if exp and obtained and (time.time() - obtained) > (exp - 86400):
        log.info("Trakt token 临近过期，尝试刷新...")
        new = refresh_token(token, config["trakt"]["client_id"], config["trakt"]["client_secret"])
        if new:
            token = new
            save_token(token, tf)
        else:
            log.warning("token 刷新失败，仍尝试用旧 token 提交")
    return token["access_token"]


# ---------------------------------------------------------------------------
# 豆瓣抓取
# ---------------------------------------------------------------------------
def make_session(cookie):
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if cookie:
        s.headers["Cookie"] = cookie
    else:
        # 无登录态时至少带一个 bid，降低 rexxar 403 概率
        s.headers["Cookie"] = "bid=%s" % "".join(random.choice("0123456789abcdef") for _ in range(11))
    return s


def _rexxar_get(s, url, params, referer):
    try:
        r = s.get(url, params=params, headers={"Referer": referer},
                  timeout=REQUEST_TIMEOUT, verify=certifi.where())
    except Exception as e:
        log.error("请求失败 %s: %s", url, e)
        return None
    if r.status_code != 200:
        log.warning("rexxar 返回 %s (%s)", r.status_code, url)
        return None
    try:
        return r.json()
    except Exception:
        return None


INTERESTS_API = "https://m.douban.com/rexxar/api/v2/user/%s/interests"


def get_wishlist(s, user_id):
    """返回 [{id,title,year,type,create_time}]，type: movie/show。

    关键点：interests 接口支持 status 查询参数，合法值为 mark(想看)/doing(在看)/done(看过)。
    传 status=mark 可以只返回「想看」，避免把上百条「看过」也翻一遍 —— 每轮请求数从 3 次降到 1 次。
    注意别传 'wish'：它是非法值，会被忽略并返回全部条目（早年我因此误判该参数无效）。
    仍保留客户端 status 过滤，作为接口行为变更时的兜底。"""
    out = []
    start = 0
    page = 50  # 该接口单页最多返回 50 条
    while True:
        js = _rexxar_get(s, INTERESTS_API % user_id,
                         {"start": start, "count": page, "status": "mark"},
                         "https://m.douban.com/mine/movie")
        if not js:
            break
        arr = js.get("interests") or []
        if not arr:
            break
        for it in arr:
            if (it.get("status") or "") not in ("wish", "mark"):
                continue
            subj = it.get("subject") or {}
            sid = str(subj.get("id") or "").strip()
            if not sid:
                continue
            raw_type = (subj.get("type") or "").lower()
            typ = "show" if raw_type == "tv" else "movie"
            out.append({
                "id": sid,
                "title": subj.get("title") or "",
                "year": subj.get("year") or "",
                "type": typ,
                "create_time": it.get("create_time") or "",
            })
        if len(arr) < page:
            break
        start += page
        time.sleep(0.3 + random.random() * 0.3)
    return out


IMDB_RE = re.compile(r"tt\d{6,}")


def get_imdb_from_web(s, subject_id):
    """从豆瓣条目网页里抠 IMDb 号（API 不一定返回 imdb）。"""
    url = "https://movie.douban.com/subject/%s/" % subject_id
    try:
        r = s.get(url, headers={"Referer": "https://movie.douban.com/"},
                  timeout=REQUEST_TIMEOUT, verify=certifi.where())
    except Exception:
        return None
    if r.status_code != 200:
        return None
    m = IMDB_RE.search(r.text)
    return m.group(0) if m else None


def get_imdb_and_aliases(s, item):
    """返回 (imdb, aliases)。
    - imdb：优先 m-api 的 imdb 字段，没有再回退网页抠。
    - aliases：豆瓣「原名 / 又名」，可作为 Trakt 搜索的备选关键词（兜底匹配用）。
    结果会顺带缓存进 item['aliases']。"""
    sid = item["id"]
    aliases = []
    js = _rexxar_get(s, "https://m.douban.com/rexxar/api/v2/subject/%s" % sid,
                     {"for_mobile": "1"}, "https://m.douban.com/movie/subject/%s/" % sid)
    if js:
        imdb = js.get("imdb")
        orig = (js.get("original_title") or "").strip()
        if orig and orig != (item.get("title") or "").strip():
            aliases.append(orig)
        item["aliases"] = aliases
        if imdb and IMDB_RE.match(imdb):
            return imdb, aliases
    item["aliases"] = aliases
    return get_imdb_from_web(s, sid), aliases


# ---------------------------------------------------------------------------
# Trakt 匹配（兜底）
# ---------------------------------------------------------------------------
def normalize_title(title):
    t = title or ""
    t = re.sub(r"\s*[第\s]*\d+\s*季", "", t)
    t = re.sub(r"\s*[第\s]*[一二三四五六七八九十两〇零]+\s*季", "", t)
    t = re.sub(r"\s*[Ss]eason\s*\d+", "", t)
    t = re.sub(r"\s*[Ss]\s?\d+\b", "", t)
    t = re.sub(r"\s*[第\s]*[一二三四五六七八九十两〇零]+\s*部", "", t)
    t = re.sub(r"\s*[Pp]art\s*\d+", "", t)
    return t.strip(" ·-—:：()（）")


SEARCH_FIELDS = "title,translations,aliases"


def _trakt_search(client_id, typ, query, limit=10):
    """调 Trakt /search/{type}。
    关键：带上 fields=title,translations,aliases —— 默认只搜 title 时中文名基本搜不到，
    加上 translations（各语言译名）和 aliases（别名）后，中文片名才有机会命中。"""
    if not query:
        return []
    headers = {"trakt-api-version": "2", "trakt-api-key": client_id, "User-Agent": USER_AGENT}
    try:
        r = requests.get("https://api.trakt.tv/search/%s" % typ,
                         params={"query": query, "fields": SEARCH_FIELDS, "limit": limit},
                         headers=headers, timeout=REQUEST_TIMEOUT, verify=certifi.where())
    except Exception:
        return []
    if r.status_code != 200:
        return []
    try:
        return r.json() or []
    except Exception:
        return []


def _pick(items, typ, year):
    """从搜索结果挑一条：优先年份 ±1 命中，否则取第一条。返回 (imdb, trakt_title, trakt_year)。"""
    if not items:
        return None, None, None
    if year and str(year).isdigit():
        y = int(year)
        for it in items:
            obj = it.get(typ) or {}
            yy = obj.get("year")
            if yy and yy - 1 <= y <= yy + 1:
                ids = obj.get("ids") or {}
                if ids.get("imdb"):
                    return ids["imdb"], obj.get("title"), yy
    obj0 = items[0].get(typ) or {}
    ids0 = obj0.get("ids") or {}
    if ids0.get("imdb"):
        return ids0["imdb"], obj0.get("title"), obj0.get("year")
    return None, None, None


def search_trakt_imdb(client_id, item, cross_type=False):
    """中文标题兜底匹配。
    关键词优先级：豆瓣原名/又名 → 去掉季数后缀的标题 → 原标题。
    返回 (imdb, trakt_title, trakt_year, 命中的关键词)，没命中返回四个 None。
    cross_type=True 时电影/剧集互搜（更容易误匹配，默认关闭）。"""
    title = (item.get("title") or "").strip()
    year = item.get("year")
    typ = item.get("type") or "movie"

    cands = []
    for a in (item.get("aliases") or []):
        if a and a not in cands:
            cands.append(a)
    for t in (normalize_title(title), title):
        if t and t not in cands:
            cands.append(t)
    if not cands:
        return None, None, None, None

    for q in cands:
        imdb, ttitle, tyear = _pick(_trakt_search(client_id, typ, q), typ, year)
        if imdb:
            return imdb, ttitle, tyear, q
        time.sleep(0.3)

    if cross_type:
        other = "show" if typ == "movie" else "movie"
        for q in cands:
            imdb, ttitle, tyear = _pick(_trakt_search(client_id, other, q), other, year)
            if imdb:
                return imdb, ttitle, tyear, q
            time.sleep(0.3)
    return None, None, None, None


# ---------------------------------------------------------------------------
# 构造 payload + 提交
# ---------------------------------------------------------------------------
def to_iso_z(create_time):
    """豆瓣 create_time 可能是 unix 秒 或 'YYYY-MM-DD HH:MM:SS'。转成 ISO Z。"""
    if not create_time:
        return None
    if isinstance(create_time, (int, float)):
        return datetime.fromtimestamp(int(create_time), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(create_time).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            continue
    return None


def build_payload(matched):
    """matched: [{type, id_value, watchlisted_at}]，id_value 为 imdb 号。"""
    movies, shows = [], []
    for m in matched:
        entry = {"ids": {"imdb": m["id_value"]}}
        if m.get("watchlisted_at"):
            entry["watchlisted_at"] = m["watchlisted_at"]
        (shows if m["type"] == "show" else movies).append(entry)
    payload = {}
    if movies:
        payload["movies"] = movies
    if shows:
        payload["shows"] = shows
    return payload


def add_to_watchlist(client_id, access_token, payload):
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % access_token,
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
        "User-Agent": USER_AGENT,
    }
    return requests.post("https://api.trakt.tv/sync/watchlist", headers=headers, json=payload,
                         timeout=REQUEST_TIMEOUT, verify=certifi.where())


# ---------------------------------------------------------------------------
# 主流程：全量同步
# ---------------------------------------------------------------------------
def run(config, dry_run, force=False):
    s = make_session(config["douban"].get("cookie", ""))
    user_id = config["douban"]["user_id"]
    client_id = config["trakt"]["client_id"]
    interval = float(config.get("sync", {}).get("request_interval", 0.8))

    # 已同步状态：默认每个豆瓣条目只推一次（--force 可忽略该状态全量重推）
    state_path = _abs(config.get("sync", {}).get("state_file", "synced.json"))
    state = {} if force else load_state(state_path)
    if force and os.path.exists(state_path):
        log.warning("已指定 --force：忽略已同步清单，本次全量重推")

    # 匹配失败条目的降频重试
    max_attempts = int(config.get("sync", {}).get("max_attempts", 3))
    retry_hours = float(config.get("sync", {}).get("retry_after_hours", 48))
    now = datetime.now(timezone.utc)

    log.info("抓取豆瓣用户 %s 的「想看」列表...", user_id)
    wish = get_wishlist(s, user_id)
    if not wish:
        log.error("未抓到任何「想看」条目。若列表设为私密或被反爬拦截，请在 config 的 douban.cookie 填入浏览器 Cookie。")
        return False
    log.info("抓到 %d 条想看，开始匹配 Trakt...", len(wish))

    fuzzy = bool(config.get("sync", {}).get("fuzzy_match", True))
    cross_type = bool(config.get("sync", {}).get("cross_type_fallback", False))

    matched, not_found, already, cooldown = [], [], [], []
    for item in wish:
        rec = state.get(item["id"])
        # 注意：只有「清单里确实有记录」才可能跳过。查不到记录 = 新条目，必须正常处理。
        if rec is None:
            pass
        else:
            # 兼容旧版状态文件：有记录但没有 status 字段的，一律视为已同步
            if rec.get("status", "synced") == "synced":
                already.append(item)
                log.info("  - %s《%s》已同步过，跳过", item["type"], item["title"])
                continue
        if rec is not None and rec.get("status") == "failed":
            attempts = int(rec.get("attempts") or 0)
            if attempts >= max_attempts:
                last = _parse_iso(rec.get("last_attempt_at"))
                if last and (now - last).total_seconds() < retry_hours * 3600:
                    left_h = retry_hours - (now - last).total_seconds() / 3600
                    cooldown.append(item)
                    log.info("  - %s《%s》已失败 %d 次，冷却中（约 %.0f 小时后再试），跳过",
                             item["type"], item["title"], attempts, max(left_h, 0))
                    continue
                log.info("  ~ %s《%s》已失败 %d 次，冷却结束，再试一次",
                         item["type"], item["title"], attempts)

        imdb, _aliases = get_imdb_and_aliases(s, item)
        trakt_title = trakt_year = None
        if not imdb and fuzzy:
            imdb, trakt_title, trakt_year, _kw = search_trakt_imdb(client_id, item, cross_type=cross_type)
        if imdb:
            matched.append({
                "type": item["type"],
                "id_value": imdb,
                "watchlisted_at": to_iso_z(item["create_time"]),
                "douban_id": item["id"],
                "title": item["title"],
            })
            if trakt_title and trakt_title.strip() != (item["title"] or "").strip():
                # 兜底匹配到的：把 Trakt 上的片名一并打出，方便人工核对是否配错片
                log.info("  ✓ %s《%s》-> %s  [Trakt片名: %s]",
                         item["type"], item["title"], imdb, trakt_title)
                dy = str(item.get("year") or "")
                if trakt_year and dy.isdigit() and abs(int(trakt_year) - int(dy)) > 1:
                    log.warning("     ⚠️ 年份差异较大（豆瓣 %s vs Trakt %s），请核对是否配错片",
                                dy, trakt_year)
            else:
                log.info("  ✓ %s《%s》-> %s", item["type"], item["title"], imdb)
        else:
            not_found.append(item)
            log.warning("  ✗ 未匹配: %s《%s》", item["type"], item["title"])
        time.sleep(interval + random.random() * 0.4)

    extra = []
    if already:
        extra.append("已同步跳过 %d" % len(already))
    if cooldown:
        extra.append("失败冷却 %d" % len(cooldown))
    log.info("匹配成功 %d / 未匹配 %d%s", len(matched), len(not_found),
             (" / " + " / ".join(extra)) if extra else "")

    # dry-run 不写状态；失败后不成功就不算"已同步"，冷却结束后还会再试。
    if not_found and not dry_run:
        for item in not_found:
            prev = state.get(item["id"]) or {}
            state[item["id"]] = {
                "status": "failed",
                "title": item["title"],
                "type": item["type"],
                "attempts": int(prev.get("attempts") or 0) + 1,
                "last_attempt_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        save_state(state_path, state)
        log.info("已记录 %d 条匹配失败（失败 %d 次后进入 %.0f 小时冷却）",
                 len(not_found), max_attempts, retry_hours)

    payload = build_payload(matched)
    if not payload:
        # 全部处于「已同步」或「失败冷却中」都属正常，不该按失败退出（否则定时任务会报警）
        if already or cooldown:
            log.info("本次没有新条目需要同步（已同步 %d / 冷却中 %d），结束。",
                     len(already), len(cooldown))
            return True
        log.error("没有可提交的条目，结束。")
        return False

    if dry_run:
        log.info("[DRY-RUN] 将要提交到 Trakt 待看的条目数: movies=%d shows=%d",
                 len(payload.get("movies", [])), len(payload.get("shows", [])))
        log.info("[DRY-RUN] payload 预览: %s", json.dumps(payload, ensure_ascii=False)[:2000])
        return True

    token = get_access_token(config)
    if not token:
        return False

    r = add_to_watchlist(client_id, token, payload)
    if r.status_code in (200, 201, 204):
        try:
            resp = r.json()
        except Exception:
            resp = {}
        log.info("✅ 提交成功。新增到待看: %s；已存在跳过: %s",
                 resp.get("added", {}), resp.get("existing", {}))
        if resp.get("not_found"):
            log.warning("⚠️ Trakt 拒绝的条目: %s", json.dumps(resp["not_found"], ensure_ascii=False)[:1000])
        # 只有真正提交成功后才记进已同步清单，避免失败被永久跳过
        record_synced(state_path, state, matched)
        return True
    elif r.status_code == 401:
        log.error("Trakt 返回 401（token 失效）。尝试刷新后重试一次...")
        new = refresh_token(load_token(_abs(config["trakt"]["token_file"])), client_id,
                            config["trakt"]["client_secret"])
        if new:
            save_token(new, _abs(config["trakt"]["token_file"]))
            r2 = add_to_watchlist(client_id, new["access_token"], payload)
            log.info("重试提交状态: %s", r2.status_code)
            if r2.status_code in (200, 201, 204):
                record_synced(state_path, state, matched)
            return r2.status_code in (200, 201, 204)
        log.error("刷新失败，请重新运行: python sync.py auth")
        return False
    else:
        log.error("Trakt 提交失败 HTTP %s: %s", r.status_code, r.text[:500])
        return False


# ---------------------------------------------------------------------------
# 探测模式（准实时）
# ---------------------------------------------------------------------------
DEFAULT_INTERVAL = 30          # 轮询间隔（秒）
DEFAULT_PROBE_COUNT = 5        # 每轮只看最新的几条
DEFAULT_FULL_SYNC_MIN = 30     # 静默期兜底全量同步间隔（分钟）
DEFAULT_BACKOFF_MAX = 600      # 连续出错时的最大退避（秒）
WATCH_STATE_FILE = "watch_state.json"
PID_FILE = "watch.pid"
DAEMON_LOG = "watch.log"

_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("收到信号 %s，准备退出...", signum)


def load_watch_state(path):
    empty = {"known": [], "last_full_sync": None, "last_probe": None}
    if not os.path.exists(path):
        return empty
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        log.warning("探测状态读取失败（%s），按空处理: %s", path, e)
        return empty
    if not isinstance(d, dict):
        return empty
    d.setdefault("known", [])
    d.setdefault("last_full_sync", None)
    d.setdefault("last_probe", None)
    return d


def save_watch_state(path, st):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("写入探测状态失败 %s: %s", path, e)


def probe(s, user_id, n):
    """只拉最新 n 条想看，返回 [(id, title, create_time)]；接口异常返回 None。"""
    js = _rexxar_get(s, INTERESTS_API % user_id, {"start": 0, "count": n, "status": "mark"},
                     "https://m.douban.com/mine/movie")
    if js is None:
        return None
    out = []
    for it in js.get("interests") or []:
        if (it.get("status") or "") not in ("wish", "mark"):
            continue
        subj = it.get("subject") or {}
        sid = str(subj.get("id") or "").strip()
        if not sid:
            continue
        out.append((sid, subj.get("title") or "", it.get("create_time") or ""))
    return out


def watch(config, dry, interval, probe_count, full_sync_min, once=False):
    """once=True 只探一轮（给定时任务每分钟调用）；否则常驻循环。"""
    user_id = config["douban"]["user_id"]
    wpath = _abs(config.get("watch", {}).get("state_file", WATCH_STATE_FILE))
    st = load_watch_state(wpath)

    # 已同步清单里的条目也算"见过"，避免把早年的老条目当成新增
    spath = _abs(config.get("sync", {}).get("state_file", "synced.json"))
    known = set(str(k) for k in load_state(spath).keys())
    known |= set(str(x) for x in (st.get("known") or []))
    if len(known) > 2000:  # 只做增量保留，避免无限膨胀
        known = set(sorted(known)[-2000:])

    s = make_session(config["douban"].get("cookie", ""))

    def absorb_all():
        """把当前全部想看 id 吸收进 known（一次请求），同步后调用。"""
        try:
            allw = get_wishlist(s, user_id)
        except Exception as e:
            log.warning("拉取全量想看失败: %s", e)
            return
        for it in allw or []:
            known.add(str(it["id"]))

    def after_sync():
        """同步完成后：更新时间 + 吸收已知 id + 落盘。

        注意：无论匹配成功与否都记进 known。匹配失败的条目交给「兜底全量 + 冷却重试」
        去处理，否则它会每轮都被当成新增，导致疯狂全量同步。"""
        st["last_full_sync"] = _now_iso()
        absorb_all()
        if len(known) > 2000:
            for _x in sorted(known)[:len(known) - 2000]:
                known.discard(_x)
        st["known"] = sorted(known)
        save_watch_state(wpath, st)

    def tick():
        """一轮探测：True=正常，False=探测失败（需要退避）。"""
        try:
            items = probe(s, user_id, probe_count)
        except Exception as e:
            log.error("探测异常: %s", e)
            items = None
        if items is None:
            return False

        st["last_probe"] = _now_iso()
        fresh = [it for it in items if str(it[0]) not in known]
        if fresh:
            for sid, title, _ct in fresh:
                log.info("🆕 发现新增想看：《%s》(豆瓣 %s)", title, sid)
            log.info("===== 触发完整同步 =====")
            try:
                ok = run(config, dry, force=False)
            except Exception as e:
                log.error("完整同步异常: %s", e, exc_info=True)
                ok = False
            log.info("===== 完整同步结束（%s）=====", "成功" if ok else "失败")
            after_sync()
            return True

        last = _parse_iso(st.get("last_full_sync"))
        due = True if last is None else (
            datetime.now(timezone.utc) - last).total_seconds() >= full_sync_min * 60
        if due:
            log.info("静默 %s 分钟，执行一次兜底全量同步（也会重试冷却结束的失败条目）", full_sync_min)
            log.info("===== 触发完整同步 =====")
            try:
                ok = run(config, dry, force=False)
            except Exception as e:
                log.error("完整同步异常: %s", e, exc_info=True)
                ok = False
            log.info("===== 完整同步结束（%s）=====", "成功" if ok else "失败")
            after_sync()
        return True

    if once:
        log.info("探测一次，豆瓣用户 %s", user_id)
        ok = tick()
        save_watch_state(wpath, st)
        return ok

    log.info("守护进程启动：豆瓣用户 %s，轮询间隔 %ss，探测 %s 条，兜底全量 %s 分钟",
             user_id, interval, probe_count, full_sync_min)
    log.info("===== 触发完整同步（启动补漏）=====")
    try:
        run(config, dry, force=False)
    except Exception as e:
        log.error("启动全量异常: %s", e, exc_info=True)
    after_sync()
    log.info("启动基线完成：已知条目 %d 条", len(known))

    err_streak = 0
    while not _stop:
        if tick():
            err_streak = 0
        else:
            err_streak += 1
            backoff = min(interval * (2 ** min(err_streak, 5)), DEFAULT_BACKOFF_MAX)
            log.warning("探测失败（连续第 %d 次），退避 %.0f 秒后重试", err_streak, backoff)
            _sleep(backoff)
            continue
        _sleep(interval)

    log.info("守护进程已退出。")
    save_watch_state(wpath, st)
    return True


def _sleep(sec):
    """可中断的 sleep：收到 SIGTERM/SIGINT 立刻返回。"""
    end = time.time() + sec + random.random() * 2  # 加一点抖动，避免节奏过于机械
    while not _stop and time.time() < end:
        time.sleep(min(1.0, max(0.0, end - time.time())))


# ---------------------------------------------------------------------------
# once 的互斥锁（Python 自己做，不再依赖 shell 的 trap —— 之前踩过 exec 让 trap 失效的坑）
# ---------------------------------------------------------------------------
ONCE_LOCK = ".once.lock"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True


def acquire_once_lock():
    """拿到锁返回 True；上一轮真的还在跑则返回 False（本次直接跳过）。"""
    path = os.path.join(HERE, ONCE_LOCK)
    if os.path.isdir(path):
        old = None
        try:
            with open(os.path.join(path, "pid"), "r", encoding="utf-8") as f:
                old = int((f.read() or "0").strip() or 0)
        except Exception:
            old = None
        if old and _pid_alive(old):
            return False
        shutil.rmtree(path, ignore_errors=True)   # 进程已死的残留锁，立刻清掉
    try:
        os.makedirs(path)
        with open(os.path.join(path, "pid"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        return False
    return True


def release_once_lock():
    shutil.rmtree(os.path.join(HERE, ONCE_LOCK), ignore_errors=True)


# ---------------------------------------------------------------------------
# 后台守护
# ---------------------------------------------------------------------------
def daemonize():
    """Unix 下把自己变成后台进程；返回 False 表示不支持（就前台跑）。"""
    if os.name != "posix":
        log.error("后台模式只支持 Linux/NAS；Windows 请直接前台运行: python sync.py watch")
        return False
    log_path = os.path.join(HERE, DAEMON_LOG)
    pid_path = os.path.join(HERE, PID_FILE)
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    f = open(log_path, "a", encoding="utf-8")
    os.dup2(f.fileno(), sys.stdout.fileno())
    os.dup2(f.fileno(), sys.stderr.fileno())
    with open(pid_path, "w", encoding="utf-8") as pf:
        pf.write(str(os.getpid()))
    return True


def daemon_status():
    pid_path = os.path.join(HERE, PID_FILE)
    if not os.path.exists(pid_path):
        print("守护进程未运行（没有 %s）" % PID_FILE)
        return True
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        pid = 0
    if pid and _pid_alive(pid):
        print("守护进程运行中 (pid %d)，日志: %s" % (pid, os.path.join(HERE, DAEMON_LOG)))
    else:
        print("守护进程未运行（%s 里的 pid %s 已失效）" % (PID_FILE, pid))
    return True


def stop_daemon():
    pid_path = os.path.join(HERE, PID_FILE)
    if not os.path.exists(pid_path):
        print("守护进程未运行")
        return True
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or 0)
    except Exception:
        pid = 0
    if not pid or not _pid_alive(pid):
        print("守护进程未运行")
        try:
            os.remove(pid_path)
        except OSError:
            pass
        return True
    os.kill(pid, signal.SIGTERM)
    for _ in range(15):
        if not _pid_alive(pid):
            break
        time.sleep(1)
    if _pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
    try:
        os.remove(pid_path)
    except OSError:
        pass
    print("守护进程已停止 (pid %d)" % pid)
    return True


# ---------------------------------------------------------------------------
# Trakt 授权（一次性）
# ---------------------------------------------------------------------------
REDIRECT_URI = "http://localhost:5173/callback"
PORT = 5173
PKCE_STATE = ".pkce_state.json"


def make_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id, challenge):
    return ("https://trakt.tv/oauth/authorize"
            "?response_type=code&client_id=%s"
            "&redirect_uri=%s"
            "&code_challenge=%s&code_challenge_method=S256"
            % (client_id, quote(REDIRECT_URI, safe=''), challenge))


def exchange_code(client_id, code, verifier):
    return requests.post("https://api.trakt.tv/oauth/token", json={
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, timeout=REQUEST_TIMEOUT, verify=certifi.where())


class _CallbackHandler(BaseHTTPRequestHandler):
    captured = {"code": None}

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        code = (q.get("code") or [None])[0]
        if code:
            _CallbackHandler.captured["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>✅ 授权成功！可以关闭此页面，回到终端继续。</h2>".encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"missing code")

    def log_message(self, *a):  # 静默
        pass


def auth_serve(client_id, token_file):
    verifier, challenge = make_pkce()
    with open(os.path.join(HERE, PKCE_STATE), "w", encoding="utf-8") as f:
        json.dump({"verifier": verifier}, f)
    try:
        server = HTTPServer(("127.0.0.1", PORT), _CallbackHandler)
    except OSError as e:
        print("❌ 无法绑定 127.0.0.1:%d（%s）。请改用: python sync.py auth --mode paste" % (PORT, e))
        return False

    print("AUTH_URL:" + build_auth_url(client_id, challenge))
    print("等待你在浏览器中完成授权（10 分钟超时）...")
    sys.stdout.flush()

    server.timeout = 1
    deadline = time.time() + 600
    while time.time() < deadline:
        server.handle_request()
        if _CallbackHandler.captured["code"]:
            break
    server.server_close()

    code = _CallbackHandler.captured["code"]
    if not code:
        print("❌ 等待超时，未收到授权回调。请重新运行。")
        return False

    r = exchange_code(client_id, code, verifier)
    if r.status_code in (200, 201):
        save_token(r.json(), token_file)
        return True
    print("❌ 换取 token 失败 HTTP %d: %s" % (r.status_code, r.text[:300]))
    return False


def auth_paste(client_id, token_file):
    verifier, challenge = make_pkce()
    print("AUTH_URL:" + build_auth_url(client_id, challenge))
    print("")
    print("1. 浏览器打开上面链接并点 Allow")
    print("2. 页面打不开是正常的，复制浏览器地址栏的完整 URL")
    print("3. 把那个 URL 粘贴到这里后回车：")
    sys.stdout.flush()
    pasted = input().strip()
    code = (parse_qs(urlparse(pasted).query).get("code") or [None])[0]
    if not code:
        print("❌ 没解析到 code，请确认粘贴的是完整跳转 URL。")
        return False
    r = exchange_code(client_id, code, verifier)
    if r.status_code in (200, 201):
        save_token(r.json(), token_file)
        return True
    print("❌ 换取 token 失败 HTTP %d: %s" % (r.status_code, r.text[:300]))
    return False


def save_token(token, token_file):
    token = dict(token)
    token.setdefault("obtained_at", int(time.time()))
    out = _abs(token_file)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=4, ensure_ascii=False)
    print("✅ token 已保存到 %s" % out)


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def selftest():
    matched = [
        {"type": "movie", "id_value": "tt0111161", "watchlisted_at": to_iso_z("1682937600")},
        {"type": "show", "id_value": "tt0083658", "watchlisted_at": to_iso_z("2023-05-01 12:00:00")},
    ]
    payload = build_payload(matched)
    assert payload["movies"][0]["ids"]["imdb"] == "tt0111161"
    assert payload["shows"][0]["ids"]["imdb"] == "tt0083658"
    assert to_iso_z("2023-05-01 12:00:00") == "2023-05-01T12:00:00Z"
    assert _parse_iso("2023-05-01T12:00:00Z") is not None
    log.info("SELFTEST OK。payload=%s", json.dumps(payload, ensure_ascii=False))
    log.info("时间转换样例: 1682937600 -> %s", to_iso_z("1682937600"))
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
USAGE = """用法: sync.py <命令> [参数]

命令:
  auth      一次性授权 Trakt（PKCE，不需要 client_secret）
  run       跑一次全量同步（默认命令）
  once      探测一次，只在发现新增想看时才同步（给定时任务每分钟调用）
  watch     常驻探测，默认 30 秒一轮（加 --daemon 后台运行）
  status    查看后台守护进程是否在跑
  stop      停止后台守护进程
  selftest  离线自检

例子:
  python sync.py auth
  python sync.py run --dry-run
  python sync.py once
  python sync.py watch --daemon
"""


def main():
    ap = argparse.ArgumentParser(
        description="豆瓣想看 -> Trakt 待看 同步工具（单文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=USAGE)
    ap.add_argument("command", nargs="?", default="run",
                    choices=["auth", "run", "once", "watch", "status", "stop", "selftest"])
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="run 时忽略已同步清单，全量重推（慎用：会把看完的也推回待看）")
    ap.add_argument("--interval", type=int, default=None, help="watch 轮询间隔秒数（默认 30）")
    ap.add_argument("--probe-count", type=int, default=None, help="每轮探测条数（默认 5）")
    ap.add_argument("--full-sync-minutes", type=float, default=None,
                    help="静默期兜底全量间隔分钟（默认 30）")
    ap.add_argument("--daemon", action="store_true", help="watch 时后台运行")
    ap.add_argument("--mode", choices=["serve", "paste"], default="serve", help="auth 的回调方式")
    args = ap.parse_args()

    if args.command == "selftest":
        setup_logging(None)
        return selftest()

    if args.command == "status":
        return daemon_status()
    if args.command == "stop":
        return stop_daemon()

    cfg_path = _abs(args.config)
    if not os.path.exists(cfg_path):
        print("找不到配置文件 %s。请先复制 config.example.json 为 config.json 并填写。" % cfg_path)
        return False
    config = load_config(cfg_path)

    if args.command == "auth":
        tf = config["trakt"].get("token_file", "token.json")
        ok = auth_serve(config["trakt"]["client_id"], tf) if args.mode == "serve" \
            else auth_paste(config["trakt"]["client_id"], tf)
        if ok:
            try:
                os.remove(os.path.join(HERE, PKCE_STATE))
            except OSError:
                pass
        return ok

    setup_logging(config.get("sync", {}).get("log_file"))
    dry = args.dry_run or bool(config.get("sync", {}).get("dry_run"))

    if args.command == "run":
        return run(config, dry, force=args.force)

    if args.command in ("once", "watch"):
        w = config.get("watch", {}) or {}
        interval = args.interval if args.interval is not None else int(w.get("interval", DEFAULT_INTERVAL))
        probe_count = args.probe_count if args.probe_count is not None else int(w.get("probe_count", DEFAULT_PROBE_COUNT))
        full_sync_min = (args.full_sync_minutes if args.full_sync_minutes is not None
                         else float(w.get("full_sync_minutes", DEFAULT_FULL_SYNC_MIN)))

        if args.command == "once":
            if not acquire_once_lock():
                log.info("上一轮还没跑完，本次跳过（不会堆积）")
                return True
            try:
                return watch(config, dry, interval, probe_count, full_sync_min, once=True)
            finally:
                release_once_lock()

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
        if args.daemon:
            if not daemonize():
                return False
            # 后台化之后重新装一遍日志（文件句柄已被 dup2 改写）
            setup_logging(config.get("sync", {}).get("log_file"))
        return watch(config, dry, interval, probe_count, full_sync_min, once=False)

    return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
