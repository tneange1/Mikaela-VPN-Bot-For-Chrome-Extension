FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 APP_DIR=/app DATA_DIR=/data MIKAELA_DOCKER=1 BRANCH=main \
    REPO_URL=https://github.com/tneange1/Mikaela-VPN-Bot-For-Chrome-Extension.git
# зависимости запекаются в образ, чтобы старт был быстрым; новый код подтягивается из git при запуске
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
WORKDIR /app
ENTRYPOINT ["/entrypoint.sh"]
