FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 脚本进镜像；config.json / token.json 通过 volume 挂载，绝不进镜像
COPY wishlist_sync.py watch_sync.py auth_trakt.py config.example.json ./

# 探测模式常驻：每 30 秒探一次，发现新增想看才完整同步。
# 想改频率就改 config.json 的 watch.interval。
CMD ["python", "watch_sync.py", "--config", "/app/config.json"]
