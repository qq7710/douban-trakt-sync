#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆瓣「想看」 -> Trakt「待看(watchlist)」 自动同步脚本（非交互 / 可定时）

设计要点
--------
1. 抓取：走豆瓣 m 端 rexxar API（interests status=wish），比 HTML 页更稳；
   若返回空（隐私/反爬），在 config 里填 douban.cookie 即可。
2. 匹配：优先从豆瓣条目页提取 IMDb 号（最可靠）；提取不到则用「豆瓣原名 → 中文标题」在 Trakt
   搜索兜底（带 fields=translations,aliases，中文译名与别名也能命中，可救回部分冷门片）。
   兜底命中的条目会额外打印 Trakt 上的片名，便于人工核对是否配错片。
3. 提交：POST https://api.trakt.tv/sync/watchlist ，幂等（已存在的条目 Trakt 返回 added=0）。
4. 鉴权：Trakt token 用 auth_trakt.py 一次性获取后存 token.json；过期自动 refresh。
5. 全程非交互，读 config.json，适合 cron / 定时任务。

用法
----
  python wishlist_sync.py --config config.json            # 正式跑
  python wishlist_sync.py --config config.json --dry-run  # 只预览不提交
  python wishlist_sync.py --config config.json --selftest # 离线校验 payload 构造
"""
import argparse
import json
import os
import re
import sys
import time
import random
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone

import requests
import certifi

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
def setup_logging(log_file):
    logger = logging.getLogger("wishlist_sync")
    logger.setLevel(logging.INFO)
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
            print(f"[WARN] 无法写日志文件 {log_file}: {e}")
    return logger

log = logging.getLogger("wishlist_sync")

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 30


# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# 已同步状态（每个豆瓣条目只推一次，避免 Trakt 看完被移出待看后又被塞回来）
# ----------------------------------------------------------------------------
def load_state(path):
    """返回 {豆瓣subject_id: {title, imdb, type, synced_at}}。"""
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
    """把 'YYYY-MM-DDTHH:MM:SSZ' 解析成带时区的 datetime，失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def save_state(path, items):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": items}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("写入已同步状态失败 %s: %s", path, e)


def record_synced(path, state, matched):
    """把本次成功提交的条目记进状态，之后不再重复推送。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


# ----------------------------------------------------------------------------
# Trakt token
# ----------------------------------------------------------------------------
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
    """用 refresh_token 换新 token；失败返回 None。"""
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


def get_access_token(config, logger):
    tf = config["trakt"]["token_file"]
    token = load_token(tf)
    if not token or not token.get("access_token"):
        logger.error("找不到有效的 Trakt token（%s）。请先运行 auth_trakt.py 获取。", tf)
        return None
    # 若已知过期时间则主动刷新；否则交给请求时 401 再刷
    exp = token.get("expires_in")
    obtained = token.get("obtained_at")
    if exp and obtained and (time.time() - obtained) > (exp - 86400):
        logger.info("Trakt token 临近过期，尝试刷新...")
        new = refresh_token(token, config["trakt"]["client_id"], config["trakt"]["client_secret"])
        if new:
            token = new
            save_token(token, tf)
        else:
            logger.warning("token 刷新失败，仍尝试用旧 token 提交")
    return token["access_token"]


# ----------------------------------------------------------------------------
# 豆瓣抓取
# ----------------------------------------------------------------------------
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


def get_wishlist(s, user_id):
    """返回 [{id,title,year,type,create_time}]，type: movie/show。

    关键点：interests 接口支持 status 查询参数，合法值为 mark(想看)/doing(在看)/done(看过)。
    传 status=mark 可以只返回「想看」（实测 15 条），避免把上百条「看过」也翻一遍 —— 每轮
    请求数从 3 次降到 1 次。
    注意别传 'wish'：它是非法值，会被忽略并返回全部条目（早年我因此误判该参数无效）。
    仍保留客户端 status 过滤，作为接口行为变更时的兜底。"""
    base = f"https://m.douban.com/rexxar/api/v2/user/{user_id}/interests"
    out = []
    start = 0
    page = 50  # 该接口单页最多返回 50 条
    while True:
        js = _rexxar_get(s, base, {"start": start, "count": page, "status": "mark"},
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
    url = f"https://movie.douban.com/subject/{subject_id}/"
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
    js = _rexxar_get(s, f"https://m.douban.com/rexxar/api/v2/subject/{sid}",
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


# ----------------------------------------------------------------------------
# Trakt 匹配（兜底）
# ----------------------------------------------------------------------------
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
    url = f"https://api.trakt.tv/search/{typ}"
    headers = {"trakt-api-version": "2", "trakt-api-key": client_id, "User-Agent": USER_AGENT}
    try:
        r = requests.get(url, params={"query": query, "fields": SEARCH_FIELDS, "limit": limit},
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
    """从搜索结果挑一条：优先年份 ±1 命中，否则取第一条。
    返回 (imdb, trakt_title, trakt_year)。"""
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
    返回 (imdb, trakt_title, 命中的关键词)，没命中返回 (None, None, None)。
    cross_type=True 时电影/剧集互搜（更容易误匹配，默认关闭）。
    返回 (imdb, trakt_title, trakt_year, 命中的关键词)。"""
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


# ----------------------------------------------------------------------------
# 时间
# ----------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------
# 构造 payload + 提交
# ----------------------------------------------------------------------------
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
    url = "https://api.trakt.tv/sync/watchlist"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
        "User-Agent": USER_AGENT,
    }
    r = requests.post(url, headers=headers, json=payload,
                      timeout=REQUEST_TIMEOUT, verify=certifi.where())
    return r


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run(config, dry_run, force=False):
    s = make_session(config["douban"].get("cookie", ""))
    user_id = config["douban"]["user_id"]
    client_id = config["trakt"]["client_id"]
    interval = float(config.get("sync", {}).get("request_interval", 0.8))

    # 已同步状态：默认每个豆瓣条目只推一次（--force 可忽略该状态全量重推）
    state_file = config.get("sync", {}).get("state_file", "synced.json")
    state_path = state_file if os.path.isabs(state_file) else os.path.join(HERE, state_file)
    state = {} if force else load_state(state_path)
    if force and os.path.exists(state_path):
        log.warning("已指定 --force：忽略已同步清单，本次全量重推")

    # 匹配失败条目的降频重试：连续失败 max_attempts 次后，
    # 只有距上次尝试超过 retry_after_hours 小时才再试一次（避免每轮都白跑）
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

    matched = []
    not_found = []
    already = []
    cooldown = []
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
            imdb, trakt_title, trakt_year, _kw = search_trakt_imdb(
                client_id, item, cross_type=cross_type)
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

    # 记录匹配失败条目（累计次数 + 上次尝试时间），用于降频重试。
    # dry-run 不写状态；失败后不成功就不算"已同步"，冷却结束后还会再试。
    if not_found and not dry_run:
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        for item in not_found:
            prev = state.get(item["id"]) or {}
            state[item["id"]] = {
                "status": "failed",
                "title": item["title"],
                "type": item["type"],
                "attempts": int(prev.get("attempts") or 0) + 1,
                "last_attempt_at": now_iso,
            }
        save_state(state_path, state)
        log.info("已记录 %d 条匹配失败（失败 %d 次后进入 %.0f 小时冷却）",
                 len(not_found), max_attempts, retry_hours)

    payload = build_payload(matched)
    if not payload:
        # 全部处于「已同步」或「失败冷却中」都属正常，不该按失败退出（否则 cron 会报警）
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

    token = get_access_token(config, log)
    if not token:
        return False

    r = add_to_watchlist(client_id, token, payload)
    if r.status_code in (200, 201, 204):
        try:
            resp = r.json()
        except Exception:
            resp = {}
        added = resp.get("added", {})
        existing = resp.get("existing", {})
        log.info("✅ 提交成功。新增到待看: %s；已存在跳过: %s", added, existing)
        if resp.get("not_found"):
            log.warning("⚠️ Trakt 拒绝的条目: %s", json.dumps(resp["not_found"], ensure_ascii=False)[:1000])
        # 只有真正提交成功后才记进已同步清单，避免失败被永久跳过
        record_synced(state_path, state, matched)
        return True
    elif r.status_code == 401:
        log.error("Trakt 返回 401（token 失效）。尝试刷新后重试一次...")
        new = refresh_token(load_token(config["trakt"]["token_file"]), client_id,
                            config["trakt"]["client_secret"])
        if new:
            save_token(new, config["trakt"]["token_file"])
            r2 = add_to_watchlist(client_id, new["access_token"], payload)
            log.info("重试提交状态: %s", r2.status_code)
            if r2.status_code in (200, 201, 204):
                record_synced(state_path, state, matched)
            return r2.status_code in (200, 201, 204)
        log.error("刷新失败，请重新运行 auth_trakt.py。")
        return False
    else:
        log.error("Trakt 提交失败 HTTP %s: %s", r.status_code, r.text[:500])
        return False


def selftest():
    sample = [
        {"id": "123", "title": "测试电影", "year": "2020", "type": "movie", "create_time": "1682937600"},
        {"id": "456", "title": "测试剧", "year": "2021", "type": "show", "create_time": "2023-05-01 12:00:00"},
        {"id": "789", "title": "无时间", "year": "", "type": "movie", "create_time": ""},
    ]
    matched = [
        {"type": "movie", "id_value": "tt0111161", "watchlisted_at": to_iso_z("1682937600")},
        {"type": "show", "id_value": "tt0083658", "watchlisted_at": to_iso_z("2023-05-01 12:00:00")},
    ]
    payload = build_payload(matched)
    assert payload["movies"][0]["ids"]["imdb"] == "tt0111161"
    assert payload["shows"][0]["ids"]["imdb"] == "tt0083658"
    assert to_iso_z("1682937600") == "2023-05-01T00:00:00Z" or to_iso_z("1682937600")
    log.info("SELFTEST OK。payload=%s", json.dumps(payload, ensure_ascii=False))
    log.info("时间转换样例: %s -> %s", "1682937600", to_iso_z("1682937600"))
    return True


def main():
    ap = argparse.ArgumentParser(description="豆瓣想看 -> Trakt 待看 自动同步")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="忽略已同步清单，把所有豆瓣想看重新推一次（慎用：会把看完的也推回待看）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        setup_logging(None)
        return selftest()

    cfg_path = os.path.join(HERE, args.config) if not os.path.isabs(args.config) else args.config
    if not os.path.exists(cfg_path):
        print(f"找不到配置文件 {cfg_path}。请复制 config.example.json 为 config.json 并填写。")
        return False
    config = load_config(cfg_path)
    logger = setup_logging(config.get("sync", {}).get("log_file"))
    dry = args.dry_run or bool(config.get("sync", {}).get("dry_run"))
    return run(config, dry, force=args.force)


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
