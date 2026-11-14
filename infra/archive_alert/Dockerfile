FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir Flask==3.1.3 gunicorn==26.0.0
COPY relay.py ./
USER 65532
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "25", "relay:app"]
