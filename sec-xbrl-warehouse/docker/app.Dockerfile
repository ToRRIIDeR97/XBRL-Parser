FROM python@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       ca-certificates \
       curl \
       git \
       libxml2-dev \
       libxslt1-dev \
       zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE /app/
COPY constraints-tested.txt /app/constraints-tested.txt
RUN python -c "import tomllib; from pathlib import Path; Path('/tmp/runtime-requirements.txt').write_text('\\n'.join(tomllib.loads(Path('pyproject.toml').read_text())['project']['dependencies']))"
# Arelle EFM's PythonMonkey dependency needs npm only while installing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends npm \
    && python -m pip install -c constraints-tested.txt -r /tmp/runtime-requirements.txt \
    && apt-get purge -y --auto-remove npm nodejs \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY src /app/src
RUN python -m pip install --no-deps .

COPY config /app/config
COPY sql /app/sql
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini
COPY docker/entrypoint.sh /usr/local/bin/xbrl-entrypoint
RUN chmod +x /usr/local/bin/xbrl-entrypoint

RUN groupadd --gid 10001 xbrl \
    && useradd --uid 10001 --gid xbrl --create-home xbrl \
    && mkdir -p /app/data \
    && chown xbrl:xbrl /app/data
USER 10001:10001

ENTRYPOINT ["xbrl-entrypoint"]
CMD ["doctor"]
