FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv
RUN uv sync --frozen --no-dev
COPY app ./app
RUN useradd --create-home moose && mkdir /app/data && chown moose:moose /app/data
USER moose
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
