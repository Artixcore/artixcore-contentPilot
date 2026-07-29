# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install --requirement requirements.txt

RUN groupadd --gid 10001 contentpilot \
    && useradd --uid 10001 --gid contentpilot --create-home --shell /usr/sbin/nologin contentpilot \
    && mkdir -p /app/data /app/logs \
    && chown -R contentpilot:contentpilot /app

COPY --chown=contentpilot:contentpilot . .

USER 10001:10001

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8501/_stcore/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
