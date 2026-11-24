# Development checks only. Built without network; ./check supplies the pinned tool files.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
RUN --network=none --mount=type=bind,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels --root-user-action=ignore \
    --disable-pip-version-check ruff==0.17.5 mypy==2.4.1
