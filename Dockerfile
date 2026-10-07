# Two images from one build. `runtime`, the last stage and so the default, is the API, the
# worker and the migration run; the command chooses which. `sim` is the provider simulator,
# for the local stack only.
#
# Base images are named by tag here. CI resolves each tag to a digest and builds from that
# (FROM python:3.14-slim@sha256:...), so that a release is built from bytes that were
# looked at, not from whatever the tag points to that day.
#
# The provider simulator is part of the same wheel (pyproject.toml packages src/corridor and
# src/corridor_sim together), so installing the project installs it. It is taken out of
# what `runtime` is given: an image that is deployed carries no code that plays a bank.
# Corridor never imports it, which a contract in the import checks holds it to.

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


# The same environment without the simulator package, for the image that is deployed. The
# check is that the directory was there to remove and is gone: a layout that moved it
# fails the build instead of shipping it.
FROM build AS build-runtime

RUN site_packages="$(/app/.venv/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')" \
    && test -d "${site_packages}/corridor_sim" \
    && rm -r "${site_packages}/corridor_sim" \
    && ! /app/.venv/bin/python -c 'import corridor_sim' 2>/dev/null \
    && /app/.venv/bin/python -c 'import corridor'


# What both images share: the user and where the environment is found.
FROM python:3.14-slim AS base

# A fixed numeric id, so that an orchestrator can require a non-root user without reading
# the image's passwd file.
RUN groupadd --system --gid 10001 corridor \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home corridor

WORKDIR /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1


# The provider simulator: the whole environment, simulator included. Built only when it is
# asked for by name (`--target sim`), as compose.yaml does for the local stack. It refuses
# to start in a production environment, and it is never pushed to the registry.
FROM base AS sim

COPY --from=build /app/.venv /app/.venv

USER 10001:10001

EXPOSE 8100

# The simulator has no route that answers without a credential, so the check is that it
# accepts a connection.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8100), 2).close()"]

CMD ["python", "-m", "corridor_sim"]


FROM base AS runtime

COPY --from=build-runtime /app/.venv /app/.venv
# `corridor db migrate` reads these from the working directory.
COPY alembic.ini ./
COPY migrations ./migrations

USER 10001:10001

EXPOSE 8000

# Liveness of the API, with nothing but the standard library. A container that runs
# another command (the worker, a migration) is given its own check or none where it is
# started.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

# No secret is built in: every setting arrives in the environment when a container starts.
CMD ["corridor", "serve", "--host", "0.0.0.0", "--port", "8000"]
