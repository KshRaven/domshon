FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DS_HOST=0.0.0.0 \
    DS_PORT=8787 \
    DS_CONFIG_DIR=/data/config \
    DS_STORAGE_ROOT=/data/files

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 --home-dir /data domshon \
    && mkdir -p /data/config /data/files && chown -R domshon:domshon /data

USER domshon
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('DS_PORT','8787'), timeout=4)" || exit 1

CMD ["domshon", "server", "serve"]
