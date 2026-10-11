FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# Install dependencies first so this layer is cached when only the code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Don't run as root.
RUN useradd --create-home appuser && mkdir -p /data && chown -R appuser /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
