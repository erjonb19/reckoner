# Container Apps Job image. No credential is baked in and none is needed: the
# job authenticates to ADLS with a managed identity, which DefaultAzureCredential
# picks up from the environment at run time.
FROM python:3.11-slim

# pyarrow wheels are manylinux, so no build toolchain is required. Keeping the
# image free of gcc is worth a few lines of care: it is most of the difference
# between a ~200 MB image and a ~900 MB one, and the job pulls it on every run.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies before source, so a code change does not re-resolve the wheels.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Which commit this image was built from, baked in at build time and logged on
# every execution. Without it an execution cannot say what code it is running,
# and a job that ran a stale `latest` reports Succeeded having quietly done less
# than the current code would -- which is exactly what happened when a manual run
# started 43 seconds before its own image finished pushing.
ARG BUILD_SHA=unknown
ENV RECKONER_BUILD_SHA=$BUILD_SHA

# Unprivileged: the job reads ADLS and writes nothing to the filesystem it
# cannot afford to lose.
RUN useradd --create-home --uid 10001 reckoner
USER reckoner

ENTRYPOINT ["python", "-m", "reckoner_job"]
CMD ["--stage", "manifest", "--dry-run"]
