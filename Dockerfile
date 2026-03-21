FROM python:3.14-slim

LABEL org.opencontainers.image.title="cutco-vault"
LABEL org.opencontainers.image.description="Cutco Collection Tracker"

WORKDIR /app

ARG APP_VERSION=dev
LABEL org.opencontainers.image.version="${APP_VERSION}"
ENV APP_VERSION=${APP_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV FLASK_ENV=production
ENV LOG_LEVEL=INFO

EXPOSE 8095

HEALTHCHECK --interval=5m --timeout=15s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8095/health')" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8095", "--workers", "2", "--timeout", "60", "app:app"]
