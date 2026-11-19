# Standalone backend image (used by docker-compose). Base image pinned by tag and index digest.
FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.txt \
 && pip uninstall -y pip setuptools wheel
COPY *.py ./

RUN useradd -r -u 1000 appuser
USER 1000

EXPOSE 8000
# Proxy headers are only trusted from the addresses in FORWARDED_ALLOW_IPS (uvicorn default: 127.0.0.1).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
