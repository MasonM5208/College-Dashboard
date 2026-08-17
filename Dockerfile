# One image, one process. There is no separate worker container: the VPS has 1 vCPU
# and 1-2GB of RAM (CLAUDE.md), and SPEC §4 rules out Redis, Celery and message
# queues for a workload of a few jobs an hour.
FROM python:3.12-slim-bookworm

# PYTHONUNBUFFERED matters more than it looks: without it, Python buffers stdout
# when it is a pipe, so `docker compose logs` would show nothing during a crash —
# exactly when the logs are needed.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before source, so editing a Python file does not reinstall them.
# Layer rebuilds are slow on one shared vCPU.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY migrations/ ./migrations/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# uid 1000 matches the `mason` account on the VPS, so files the container writes
# into the bind-mounted /data are owned by Mason on the host and readable by the
# nightly backup without any permission juggling.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin dashboard \
 && chmod 0755 /usr/local/bin/docker-entrypoint.sh

USER dashboard

# The database lives on a bind mount from the host, never inside the image: an
# image is rebuilt and replaced, and anything inside one is disposable.
VOLUME ["/data"]

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
