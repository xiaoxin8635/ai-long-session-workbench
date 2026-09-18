# ============================================================
# EchoDesk memory-service - 容器镜像
# 基础镜像 python:3.11-slim；非 root 运行；含 alembic（容器内执行迁移）
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ---- 非 root 运行用户 ----
RUN groupadd -r app && useradd -r -g app -d /app app

WORKDIR /app

# ---- 先装依赖（利用层缓存：pyproject 变化才重装）----
# PyPI 源可经 build arg 覆写（国内环境传清华源加速）
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY pyproject.toml ./
# 预置空 app 包占位，使依赖安装层与代码层分离（源码变更不触发重装依赖）
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir --index-url ${PIP_INDEX_URL} . \
    && rm -rf app

# ---- 应用代码 ----
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

USER app

EXPOSE 8100

# 存活探针：进程内 urllib 请求，无需 curl（slim 镜像未带）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8100/healthz', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
