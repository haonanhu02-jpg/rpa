# 使用官方Python镜像作为基础镜像
FROM python:3.13-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install the runtime snapshot exported from uv.lock, with verified file hashes.
# Package indexes apply to this image build only, never the host pip config.
ARG PYPI_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
ARG PYPI_FALLBACK_INDEX_URL=https://pypi.org/simple
COPY backend/ai-service/requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes --timeout 60 --retries 3 \
        --index-url "$PYPI_INDEX_URL" -r requirements.lock \
    || python -m pip install --no-cache-dir --require-hashes --timeout 60 --retries 3 \
        --index-url "$PYPI_FALLBACK_INDEX_URL" -r requirements.lock

# 复制项目代码
COPY backend/ai-service/app/ ./app/

# 复制配置文件（如果存在）
# COPY docker/.env ./.env

# 暴露端口
EXPOSE 8010


# 启动应用
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--proxy-headers", "--workers", "4"]
