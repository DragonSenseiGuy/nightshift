FROM ubuntu:24.04

# curl is handy for in-container egress checks; git/ca-certificates support ad-hoc
# worktree tasks via the orchestrator's run_task path. Python itself is provided by
# uv (the system Python on 24.04 is 3.12, but the project needs >=3.13).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git curl \
    && rm -rf /var/lib/apt/lists/*

# uv: fast, reproducible dependency install from the committed lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Bake the project's dependencies into a fixed venv at build time. Egress is locked
# down at runtime, so nothing can be fetched from PyPI then — it must be here already.
# uv downloads a managed CPython matching pyproject's requires-python.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Application code, used by the broker container (the sandbox runs the mounted worktree
# at /workspace instead). Only the modules the broker/summariser need.
COPY *.py ./

# Sane default; the orchestrator overrides command + working dir per container.
WORKDIR /workspace
CMD ["bash"]
