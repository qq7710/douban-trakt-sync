#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""豆瓣想看 -> Trakt 待看：准实时守护进程（轻量轮询 + 事件触发）。

背景
----
豆瓣没有官方 Webhook（开放 API 多年以前就关闭了，也没有「想看变更」回调），
所以做不到真正的"事件推送监听"。能做到的最接近的方案是：

  利用 interests 接口「按 create_time 倒序返回」+ 支持 count 参数的特性，
  每 N 秒只拉最新 5 条（一次请求，几十字节），一旦发现没见过的条目 id，
  立刻触发一次完整同步。

效果：在豆瓣点下「想看」后，最多一个轮询周期（默认 30 秒）就同步到 Trakt。
开销：静默期每轮只有 1 次请求（约 2880 次/天，30 秒一轮），
      比之前每 10 分钟跑一次全量（每次 20~40 次请求）更省。

用法
----
    python3 watch_sync.py                 # 前台运行（看日志）
    python3 watch_sync.py --interval 60   # 60 秒一轮（更保守）
    python3 watch_sync.py --once          # 只探测一次，有新增就同步，然后退出（适合 cron 每分钟）

推荐用 watch_sync.sh 后台托管：
    ./watch_sync.sh start | stop | restart | status | log
"""

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import wishlist_sync as ws

HERE = os.path.dirname(os.path.abspath(__file__))

# 默认参数（config.json 的 watch 段可覆盖）
DEFAULT_INTERVAL = 30          # 轮询间隔（秒）
DEFAULT_PROBE_COUNT = 5        # 每轮探测条数（接口倒序返回，只看最新的这几条）
DEFAULT_FULL_SYNC_MIN = 30     # 静默期兜底全量同步间隔（分钟）
DEFAULT_BACKOFF_MAX = 600      # 连续出错时的最大退避（秒）
WATCH_STATE_FILE = "watch_state.json"

_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    ws.log.info("收到信号 %s，准备退出...", signum)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 探测状态：known = "已经见过的豆瓣条目 id"，用来判断有没有新东西
# ---------------------------------------------------------------------------
def load_watch_state(path):
    if not os.path.exists(path):
        return {"known": [], "last_full_sync": None, "last_probe": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        ws.log.warning("守护状态读取失败（%s），按空处理: %s", path, e)
        return {"known": [], "last_full_sync": None, "last_probe": None}
    if not isinstance(d, dict):
        return {"known": [], "last_full_sync": None, "last_probe": None}
    d.setdefault("known", [])
    d.setdefault("last_full_sync", None)
    d.setdefault("last_probe", None)
    return d


def save_watch_state(path, st):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        ws.log.error("写入守护状态失败 %s: %s", path, e)


# ---------------------------------------------------------------------------
# 轻量探测：只拉最新 N 条想看
# ---------------------------------------------------------------------------
def probe(s, user_id, n):
    """返回 [(id, title, create_time)]，接口异常返回 None。"""
    base = "https://m.douban.com/rexxar/api/v2/user/%s/interests" % user_id
    js = ws._rexxar_get(s, base, {"start": 0, "count": n, "status": "mark"},
                        "https://m.douban.com/mine/movie")
    if js is None:
        return None
    arr = js.get("interests") or []
    out = []
    for it in arr:
        if (it.get("status") or "") not in ("wish", "mark"):
            continue
        subj = it.get("subject") or {}
        sid = str(subj.get("id") or "").strip()
        if not sid:
            continue
        out.append((sid, subj.get("title") or "", it.get("create_time") or ""))
    return out


def do_full_sync(config, dry):
    """跑一次完整同步（wishlist_sync.run）。"""
    ws.log.info("===== 触发完整同步 =====")
    try:
        ok = ws.run(config, dry, force=False)
    except Exception as e:
        ws.log.error("完整同步异常: %s", e, exc_info=True)
        return False
    ws.log.info("===== 完整同步结束（%s）=====", "成功" if ok else "失败")
    return bool(ok)


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def watch(config, dry, interval, probe_count, full_sync_min, once=False):
    user_id = config["douban"]["user_id"]
    cookie = config["douban"].get("cookie", "")

    watch_state_file = config.get("watch", {}).get("state_file", WATCH_STATE_FILE)
    wpath = watch_state_file if os.path.isabs(watch_state_file) else os.path.join(HERE, watch_state_file)
    st = load_watch_state(wpath)

    # 已同步清单里的条目也算"见过"，避免把早年的老条目当成新增
    state_file = config.get("sync", {}).get("state_file", "synced.json")
    spath = state_file if os.path.isabs(state_file) else os.path.join(HERE, state_file)
    known = set(str(k) for k in ws.load_state(spath).keys())
    known |= set(str(x) for x in (st.get("known") or []))
    # known 只做增量保留，避免无限膨胀
    if len(known) > 2000:
        known = set(list(known)[-2000:])

    s = ws.make_session(cookie)

    def absorb_all():
        """把当前全部想看 id 吸收进 known（一次请求），同步后调用。"""
        try:
            allw = ws.get_wishlist(s, user_id)
        except Exception as e:
            ws.log.warning("拉取全量想看失败: %s", e)
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
        """一轮探测：返回 True 表示正常，False 表示探测失败（需要退避）。"""
        try:
            items = probe(s, user_id, probe_count)
        except Exception as e:
            ws.log.error("探测异常: %s", e)
            items = None
        if items is None:
            return False

        st["last_probe"] = _now_iso()
        fresh = [it for it in items if str(it[0]) not in known]
        if fresh:
            for sid, title, _ct in fresh:
                ws.log.info("🆕 发现新增想看：《%s》(豆瓣 %s)", title, sid)
            do_full_sync(config, dry)
            after_sync()
            return True

        last = _parse_iso(st.get("last_full_sync"))
        due = True if last is None else (
            datetime.now(timezone.utc) - last).total_seconds() >= full_sync_min * 60
        if due:
            ws.log.info("静默 %s 分钟，执行一次兜底全量同步（也会重试冷却结束的失败条目）",
                        full_sync_min)
            do_full_sync(config, dry)
            after_sync()
        return True

    if once:
        # 单次模式（给 cron 每分钟调用用）：只探测一次，不启动时全量，开销最小
        ws.log.info("单次模式：探测一次，豆瓣用户 %s", user_id)
        ok = tick()
        save_watch_state(wpath, st)
        return ok

    # 守护模式：启动先跑一次全量，把停机期间漏掉的补上，同时给 known 打底
    ws.log.info("守护进程启动：豆瓣用户 %s，轮询间隔 %ss，探测 %s 条，兜底全量 %s 分钟",
                user_id, interval, probe_count, full_sync_min)
    do_full_sync(config, dry)
    after_sync()
    ws.log.info("启动基线完成：已知条目 %d 条", len(known))

    err_streak = 0
    while not _stop:
        if tick():
            err_streak = 0
        else:
            err_streak += 1
            backoff = min(interval * (2 ** min(err_streak, 5)), DEFAULT_BACKOFF_MAX)
            ws.log.warning("探测失败（连续第 %d 次），退避 %.0f 秒后重试", err_streak, backoff)
            _sleep(backoff)
            continue
        _sleep(interval)

    ws.log.info("守护进程已退出。")
    save_watch_state(wpath, st)
    return True


def _sleep(sec):
    """可中断的 sleep：收到 SIGTERM/SIGINT 立刻返回。"""
    end = time.time() + sec + random.random() * 2  # 加一点抖动，避免节奏过于机械
    while not _stop and time.time() < end:
        time.sleep(min(1.0, max(0.0, end - time.time())))


def main():
    ap = argparse.ArgumentParser(description="豆瓣想看 -> Trakt 待看 准实时守护进程")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--interval", type=int, default=None,
                    help="轮询间隔秒数（默认 30，config.json 的 watch.interval 可覆盖）")
    ap.add_argument("--probe-count", type=int, default=None, help="每轮探测条数（默认 5）")
    ap.add_argument("--full-sync-minutes", type=float, default=None,
                    help="静默期兜底全量同步间隔分钟（默认 30）")
    ap.add_argument("--once", action="store_true",
                    help="只跑一轮（启动全量 + 探测一次），适合 cron 每分钟调用")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    cfg_path = os.path.join(HERE, args.config) if not os.path.isabs(args.config) else args.config
    if not os.path.exists(cfg_path):
        print("找不到配置文件 %s。请先复制 config.example.json 为 config.json 并填写。" % cfg_path)
        return False
    config = ws.load_config(cfg_path)
    ws.setup_logging(config.get("sync", {}).get("log_file"))

    w = config.get("watch", {}) or {}
    interval = args.interval if args.interval is not None else int(w.get("interval", DEFAULT_INTERVAL))
    probe_count = args.probe_count if args.probe_count is not None else int(w.get("probe_count", DEFAULT_PROBE_COUNT))
    full_sync_min = (args.full_sync_minutes if args.full_sync_minutes is not None
                     else float(w.get("full_sync_minutes", DEFAULT_FULL_SYNC_MIN)))
    dry = args.dry_run or bool(config.get("sync", {}).get("dry_run"))

    return watch(config, dry, interval, probe_count, full_sync_min, once=args.once)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
