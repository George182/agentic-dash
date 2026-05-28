# Multi-stage build: build deps install into a venv in stage 1, runtime stage
# carries only the populated venv + Python — no pip toolchain, no build files,
# no source tree. Drops the image roughly in half vs. a single-stage build.

# ---------- Stage 1: builder ----------
FROM python:3.12-slim AS builder

WORKDIR /build

# Copy only what's needed to install. Excluded from the runtime stage entirely.
COPY pyproject.toml ./
COPY app/ ./app/
COPY agent/ ./agent/

# Install into an isolated venv that we copy whole into runtime. Pinning the
# venv path makes the COPY target deterministic.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir .

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

# Bring over only the populated venv. `pyproject.toml` package-data already
# bundled `app/assets/*` + `app/assets/fonts/*.woff2` into site-packages, so
# Dash will serve them from there — no separate COPY needed.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Cloud Run injects PORT; default to 8080 for local `docker run` parity.
ENV PORT=8080
EXPOSE 8080

# Single container: FastAPI shell serves /agui (SSE) + the Dash app at /.
CMD ["sh", "-c", "uvicorn app.server:app --host 0.0.0.0 --port ${PORT}"]
