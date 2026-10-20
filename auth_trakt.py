#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trakt 一次性授权（PKCE 授权码流程，无需 client_secret）。

用法
----
  python auth_trakt.py --config config.json            # 本地回调模式（推荐）
  python auth_trakt.py --config config.json --mode paste  # 手动粘贴模式（备用）

流程
----
1. 脚本打印一个 Trakt 授权链接。
2. 你在浏览器打开 → 登录 → 点 Allow。
3. 浏览器跳到 http://localhost:5173/callback?code=...
   - 本地回调模式：脚本自动截获 code，自动换取 token，无需任何手动操作。
   - 粘贴模式：页面会打不开（正常！），把浏览器地址栏完整 URL 复制回来粘贴即可。
4. token.json 保存成功后，wishlist_sync.py 即可使用（token 7 天有效，脚本会自动用 refresh_token 续期）。
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote, urlparse, parse_qs
from urllib.request import urlopen

import requests
import certifi

HERE = os.path.dirname(os.path.abspath(__file__))
REDIRECT_URI = "http://localhost:5173/callback"
PORT = 5173
STATE_FILE = os.path.join(HERE, ".pkce_state.json")
REQUEST_TIMEOUT = 30


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def make_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id, challenge):
    return (
        "https://trakt.tv/oauth/authorize"
        f"?response_type=code&client_id={client_id}"
        f"&redirect_uri={quote(REDIRECT_URI, safe='')}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
    )


def exchange_code(client_id, code, verifier):
    r = requests.post("https://api.trakt.tv/oauth/token", json={
        "code": code,
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, timeout=REQUEST_TIMEOUT, verify=certifi.where())
    return r


def save_token_file(token, token_file):
    token = dict(token)
    token.setdefault("obtained_at", int(time.time()))
    out = os.path.join(HERE, token_file) if not os.path.isabs(token_file) else token_file
    with open(out, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=4, ensure_ascii=False)
    print(f"✅ token 已保存到 {out}")


# ---------------- 本地回调服务器 ----------------
class CallbackHandler(BaseHTTPRequestHandler):
    captured = {"code": None}

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        code = (q.get("code") or [None])[0]
        if code:
            CallbackHandler.captured["code"] = code
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


def serve_mode(client_id, token_file):
    verifier, challenge = make_pkce()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"verifier": verifier}, f)

    try:
        server = HTTPServer(("127.0.0.1", PORT), CallbackHandler)
    except OSError as e:
        print(f"❌ 无法绑定 127.0.0.1:{PORT}（{e}）。请改用: python auth_trakt.py --mode paste")
        return False

    url = build_auth_url(client_id, challenge)
    print("AUTH_URL:" + url)
    print("等待你在浏览器中完成授权（10 分钟超时）...")
    sys.stdout.flush()

    server.timeout = 1
    deadline = time.time() + 600
    while time.time() < deadline:
        server.handle_request()
        if CallbackHandler.captured["code"]:
            break
    server.server_close()

    code = CallbackHandler.captured["code"]
    if not code:
        print("❌ 等待超时，未收到授权回调。请重新运行。")
        return False

    r = exchange_code(client_id, code, verifier)
    if r.status_code in (200, 201):
        save_token_file(r.json(), token_file)
        return True
    print(f"❌ 换取 token 失败 HTTP {r.status_code}: {r.text[:300]}")
    return False


def paste_mode(client_id, token_file):
    verifier, challenge = make_pkce()
    url = build_auth_url(client_id, challenge)
    print("AUTH_URL:" + url)
    print("\n1. 浏览器打开上面链接并点 Allow")
    print("2. 页面打不开是正常的，复制浏览器地址栏的完整 URL")
    print("3. 把那个 URL 粘贴到这里后回车：")
    sys.stdout.flush()
    pasted = input().strip()
    q = parse_qs(urlparse(pasted).query)
    code = (q.get("code") or [None])[0]
    if not code:
        print("❌ 没解析到 code，请确认粘贴的是完整跳转 URL。")
        return False
    r = exchange_code(client_id, code, verifier)
    if r.status_code in (200, 201):
        save_token_file(r.json(), token_file)
        return True
    print(f"❌ 换取 token 失败 HTTP {r.status_code}: {r.text[:300]}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", choices=["serve", "paste"], default="serve")
    args = ap.parse_args()
    cfg_path = os.path.join(HERE, args.config) if not os.path.isabs(args.config) else args.config
    config = load_config(cfg_path)
    client_id = config["trakt"]["client_id"]
    token_file = config["trakt"].get("token_file", "token.json")

    if args.mode == "serve":
        ok = serve_mode(client_id, token_file)
    else:
        ok = paste_mode(client_id, token_file)
    if ok:
        try:
            os.remove(STATE_FILE)
        except OSError:
            pass
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
