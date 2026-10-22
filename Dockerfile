FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The add-on runs as its own user, not as root. Profiles, the session key and cached images live in /data.
ENV DATA_DIR=/data
RUN useradd --system --uid 10001 --no-create-home nuvio && mkdir -p /data && chown nuvio /data
USER nuvio
VOLUME /data
EXPOSE 7000

# Healthy when the editor page answers.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:' + os.environ.get('PORT', '7000') + '/configure', timeout=5)"]

# One worker on purpose: each profile keeps its catalog in memory and refreshes it in a
# background thread, so several worker processes would each do that work again. Threads
# give the concurrency. The control socket is switched off: it needs a home folder, which the add-on's user doesn't have.
CMD ["sh", "-c", "exec gunicorn -b 0.0.0.0:${PORT:-7000} --workers 1 --threads 16 --timeout 120 --no-control-socket 'app.web:create_app()'"]
