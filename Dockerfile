FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FOXTUNES_DATA_DIR=/data FOXTUNES_OAUTH_BIND=0.0.0.0
RUN groupadd --gid 10001 foxtunes && useradd --uid 10001 --gid 10001 --no-create-home foxtunes && mkdir /data && chown foxtunes:foxtunes /data
COPY foxtunes.py web_server.py ./
COPY web/ ./web/
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8863
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8863/healthz', timeout=3)"
CMD ["python", "web_server.py", "--bind", "0.0.0.0"]
