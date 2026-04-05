FROM python:3.14-slim

LABEL org.opencontainers.image.title="cutco-vault"
LABEL org.opencontainers.image.description="Cutco Collection Tracker"

WORKDIR /app

ARG APP_VERSION=dev
LABEL org.opencontainers.image.version="${APP_VERSION}"
ENV APP_VERSION=${APP_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 gosu tzdata && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV FLASK_ENV=production
ENV LOG_LEVEL=INFO
ENV TZ=UTC

# PUID/PGID let the container run as the host user so /data volume files
# are owned correctly.  Defaults to root (0) if not set.
ENV PUID=0
ENV PGID=0

EXPOSE 8095

HEALTHCHECK --interval=5m --timeout=15s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8095/health')" || exit 1

# Create the app user with the requested UID/GID at container start, then
# hand off to gunicorn running as that user.
CMD ["sh", "-c", \
  "if [ \"$PUID\" != \"0\" ]; then \
     groupadd -g $PGID appgroup 2>/dev/null || true; \
     useradd -u $PUID -g $PGID -M -d /data -s /sbin/nologin appuser 2>/dev/null || true; \
     chown -R $PUID:$PGID /data /app; \
     exec gosu appuser env HOME=/data gunicorn --bind 0.0.0.0:8095 --workers 4 --timeout 120 app:app; \
   else \
     exec gunicorn --bind 0.0.0.0:8095 --workers 4 --timeout 120 app:app; \
   fi"]
