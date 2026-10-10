FROM python:3.12-slim

# pdftotext is what turns the vonk PDFs into text before extraction
RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# python:3.12-slim has no user on UID 1000 (checked with getent), unlike node:*-slim
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin vonk \
    && mkdir /data \
    && chown vonk:vonk /data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV VONK_DB_PATH=/data/vonk.db \
    PYTHONUNBUFFERED=1

USER vonk
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

ENTRYPOINT ["vonk"]
CMD ["serve"]
