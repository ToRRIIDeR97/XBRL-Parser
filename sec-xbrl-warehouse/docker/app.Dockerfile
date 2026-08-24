FROM python:3.12-slim

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
COPY src /app/src
RUN python -m pip install --upgrade pip \
    && python -m pip install .

COPY config /app/config
COPY sql /app/sql
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini
COPY docker/entrypoint.sh /usr/local/bin/xbrl-entrypoint
RUN chmod +x /usr/local/bin/xbrl-entrypoint

ENTRYPOINT ["xbrl-entrypoint"]
CMD ["doctor"]
