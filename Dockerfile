FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
# Install the exact versions pinned in uv.lock; dependencies first so code changes reuse this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra production --no-install-project
COPY capstone ./capstone
RUN uv sync --frozen --no-dev --extra production --no-editable
RUN useradd --system --uid 10001 --create-home appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "capstone.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
