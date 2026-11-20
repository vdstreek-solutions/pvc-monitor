# Base images are pinned by tag and multi-arch index digest; bump both together.

### Stage 1: build React frontend
FROM node:26.13.1-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/vite.config.js ./
COPY frontend/src ./src
RUN npm run build

### Stage 2: Python backend, frontend dist served by FastAPI
FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY backend/requirements.txt .
# Hash-checked, wheel-only install. pip/setuptools are build tooling only;
# removing them drops their (bundled-dependency) CVEs from the image.
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt \
 && pip uninstall -y pip setuptools wheel
COPY backend/*.py ./
# main.py resolves ../frontend/dist relative to /app → /frontend/dist
COPY --from=frontend-builder /frontend/dist /frontend/dist

RUN useradd -r -u 1000 appuser
USER 1000

EXPOSE 8000
# Proxy headers are only trusted from the addresses in FORWARDED_ALLOW_IPS
# (uvicorn default: 127.0.0.1). Set it to the reverse proxy / gateway addresses.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
