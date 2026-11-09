# syntax=docker/dockerfile:1
# WorkplaceOps AI API (FastAPI). Build: docker build -t workplaceops-api .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY data/ ./data/
COPY scripts/ ./scripts/

# Writable runtime stores (git-ignored on the host and excluded from the build context).
RUN mkdir -p data/policy_store data/vendor_quote_store && chown -R app:app /app/data

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
