FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements*.txt ./
# CPU inference avoids shipping CUDA libraries for the local retrieval model.
RUN python -m pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir -r requirements-local.txt

COPY *.py ./
COPY web ./web
COPY examples ./examples
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/.data /app/.models \
    && chown -R app:app /app
USER app

EXPOSE 8767
CMD ["python", "-m", "uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8767"]
