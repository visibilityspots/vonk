FROM python:3.12-slim

# pdftotext turns the vonk PDFs into text before extraction; image-only PDFs go
# through tesseract (Dutch data) instead
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-nld \
    && rm -rf /var/lib/apt/lists/*

# python:3.12-slim has no user on UID 1000 (checked with getent), unlike node:*-slim
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin vonk \
    && mkdir /data \
    && chown vonk:vonk /data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# pip is only needed for this install; removing it keeps the base image's pip CVEs out
RUN pip install --no-cache-dir . \
    && pip uninstall -y pip

ENV VONK_DB_PATH=/data/vonk.db \
    PYTHONUNBUFFERED=1

USER vonk
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

ENTRYPOINT ["vonk"]
CMD ["serve"]
