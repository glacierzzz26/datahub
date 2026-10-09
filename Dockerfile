FROM python:3.12-slim

# 部署标识（版本 + 短 hash，生产核验用）
ARG APP_VERSION=v0.0.0
ARG GIT_SHORT=unknown
ENV APP_VERSION=${APP_VERSION} \
    GIT_SHORT=${GIT_SHORT} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码
COPY app ./app

EXPOSE 8100

# 入口：装请求层超时补丁并起 uvicorn（app.server:main）
CMD ["python", "-m", "app.server"]
