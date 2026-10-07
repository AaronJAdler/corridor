# One image for the API, the worker and the migration run; the command chooses which.
#
# Base images are named by tag here. CI resolves each tag to a digest and builds from that
# (FROM python:3.14-slim@sha256:...), so that a release is built from bytes that were
# looked at, not from whatever the tag points to that day.
#
# The provider simulator is part of the same wheel (pyproject.toml packages src/corridor and
# src/corridor_sim together), so this image carries it too. It is inert unless it is started
# with `python -m corridor_sim`, and it refuses to start in a production environment.

FROM python:3.14-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

RUN pip install --no-cache-dir uv==0.11.32

# The dependencies first, from the lockfile alone: this layer is rebuilt only when the
# lockfile changes, not on every change to the source.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the project itself, installed into the environment and not linked to the source
# tree, which is left behind in this stage.
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.14-slim AS runtime

# A fixed numeric id, so that an orchestrator can require a non-root user without reading
# the image's passwd file.
RUN groupadd --system --gid 10001 corridor \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home corridor

WORKDIR /app

COPY --from=build /app/.venv /app/.venv
# `corridor db migrate` reads these from the working directory.
COPY alembic.ini ./
COPY migrations ./migrations

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001

EXPOSE 8000

# Liveness of the API, with nothing but the standard library. A container that runs
# another command (the worker, a migration) is given its own check or none where it is
# started.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# No secret is built in: every setting arrives in the environment when a container starts.
CMD ["corridor", "serve", "--host", "0.0.0.0", "--port", "8000"]
