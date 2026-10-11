FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 AS builder

WORKDIR /build
RUN python -m pip install --no-cache-dir pip==26.2.1
COPY pyproject.toml README.md LICENSE NOTICE .env.example talos.example.yaml ./
COPY packages ./packages
COPY config ./config
COPY skills ./skills
ARG TALOS_RUNTIME_EXTRAS=service
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels ".[${TALOS_RUNTIME_EXTRAS}]"

FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apk upgrade --no-cache \
    && addgroup -g 1000 -S talos && adduser -u 1000 -S -G talos talos
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir pip==26.2.1 \
    && python -m pip install --no-cache-dir --no-compile /wheels/*.whl \
    && python -m pip uninstall -y pip \
    && rm -rf /wheels
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY skills ./skills
RUN mkdir -p /app/tools /app/.talos/model-credentials \
    && chown -R talos:talos /app/config /app/skills /app/tools /app/.talos \
    && chmod 700 /app/.talos/model-credentials
COPY talos.example.yaml ./talos.yaml
COPY .env.example ./
USER talos

CMD ["uvicorn", "talos.api.server:app", "--host", "127.0.0.1", "--port", "8000"]
