FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 脚本进镜像；config.json / token.json 通过 volume 挂载，绝不进镜像
COPY wishlist_sync.py auth_trakt.py config.example.json ./

# 默认每 10 分钟一轮（幂等，只补新增）。改下面的 sleep 即可调频率。
CMD ["sh", "-c", "while true; do python wishlist_sync.py --config /app/config.json; sleep 600; done"]
