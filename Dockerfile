### Stage 1: build React frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

### Stage 2: Python backend, frontend dist served by FastAPI
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
# pip/setuptools are build tooling only; removing them drops their (bundled-dependency) CVEs from the image.
RUN pip install --no-cache-dir -r requirements.txt && pip uninstall -y pip setuptools wheel
COPY backend/ .
# main.py resolves ../frontend/dist relative to /app → /frontend/dist
COPY --from=frontend-builder /frontend/dist /frontend/dist

RUN useradd -r -u 1000 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
