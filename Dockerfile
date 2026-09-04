# Candela — container image
#
# By default the image does NOT contain Playwright/Chromium, which keeps it around
# 250 MB. That browser is only needed by etl/fetch_trilux.py, which signs in to a
# manufacturer portal. If you want the container to fetch updates by itself:
#
#     WITH_BROWSER=1 docker compose build
#
# which adds roughly 400 MB.

FROM python:3.12-slim

ARG WITH_BROWSER=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# Dependencies first, so code changes do not invalidate the layer
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_BROWSER" = "1" ]; then \
        playwright install-deps chromium && playwright install chromium; \
    else \
        pip uninstall -y playwright >/dev/null 2>&1 || true; \
    fi

COPY etl/ ./etl/
COPY web/ ./web/
COPY examples/ ./examples/
COPY brands/ ./brands/
COPY docker/entrypoint.sh ./docker/entrypoint.sh
COPY run.sh README.md LICENSE ./

RUN chmod +x docker/entrypoint.sh \
 && mkdir -p data \
 && useradd --create-home --uid 10001 candela \
 && chown -R candela:candela /app \
 && if [ -d /opt/playwright ]; then chown -R candela:candela /opt/playwright; fi

USER candela
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["./docker/entrypoint.sh"]
