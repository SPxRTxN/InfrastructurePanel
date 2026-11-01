# Образ Homelab Dashboard: linux/amd64 (мини-ПК) и linux/arm64 (Raspberry Pi).
# Минорная версия Python и Debian зафиксированы; патчи безопасности приходят при пересборке.
# Для полностью воспроизводимой сборки замените тег на digest: python:3.12-slim-trixie@sha256:…
ARG PYTHON_IMAGE=python:3.12-slim-trixie
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app \
    DATABASE_URL=sqlite:////data/homelab.db \
    HOST=0.0.0.0 \
    PORT=8000

# tzdata — чтобы работал TZ=Europe/Moscow: события календаря хранятся в местном времени
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь; ~/.ssh — точка монтирования ключей и known_hosts
RUN groupadd --gid 1000 app \
 && useradd --uid 1000 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data /home/app/.ssh \
 && chown app:app /data /home/app/.ssh \
 && chmod 700 /data /home/app/.ssh

WORKDIR /app
COPY requirements.txt requirements.lock ./
# Только готовые сборки (wheels): в slim-образе нет компилятора, а сборка из исходников на arm64
# в CI уже ломалась (sqlcipher3 0.6.0). Если у пакета нет wheel под архитектуру — сборка честно падает здесь.
RUN pip install --only-binary=:all: -r requirements.txt -c requirements.lock

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
# Агент той же версии, что ушла с этой панелью: при старте регистрируется в каталоге версий агентов
COPY handoff/homelab-agent ./agent-bundle

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT', '8000')}/healthz\", timeout=4)"]

# Один процесс: планировщик работает внутри приложения и не должен дублироваться
CMD ["python", "-m", "app.cli", "serve"]
