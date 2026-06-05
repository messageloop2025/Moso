# 毛竹 运行镜像：应用打入镜像；持久化由 compose 挂载 data/data、data/fs、data/logs

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt \
    && python -c "from markitdown import MarkItDown; print('markitdown OK')"

COPY . /app

ARG EDGEOPS_VERSION=dev

ENV EDGEOPS_VERSION=${EDGEOPS_VERSION}

RUN mkdir -p /app/data /app/web/fs /app/logs

ENV EDGEOPS_HOST=0.0.0.0 \
    EDGEOPS_PORT=8010 \
    EDGEOPS_DB=/app/data/edgeops.db

EXPOSE 8010

VOLUME ["/app/data", "/app/web/fs", "/app/logs"]

CMD ["sh", "-c", "mkdir -p /app/logs && uvicorn app:app --host ${EDGEOPS_HOST:-0.0.0.0} --port ${EDGEOPS_PORT:-8010} --forwarded-allow-ips='*' 2>&1 | tee -a /app/logs/edgeops.log"]

