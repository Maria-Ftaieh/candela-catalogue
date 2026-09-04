# Candela — container image
#
# Deliberately does NOT install Playwright/Chromium (~400 MB). That is only needed
# by etl/fetch_trilux.py, which downloads from a manufacturer portal; inside the
# container you bring your own data (or use the generated demo data).

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so code changes do not invalidate the layer
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y playwright 2>/dev/null || true

COPY etl/ ./etl/
COPY web/ ./web/
COPY examples/ ./examples/
COPY brands/ ./brands/
COPY docker/entrypoint.sh ./docker/entrypoint.sh
COPY run.sh README.md LICENSE ./

RUN chmod +x docker/entrypoint.sh \
 && mkdir -p data \
 && useradd --create-home --uid 10001 candela \
 && chown -R candela:candela /app

USER candela
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import urllib.request,sys; \
      sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["./docker/entrypoint.sh"]
