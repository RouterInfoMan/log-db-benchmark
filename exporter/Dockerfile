FROM python:3.12-slim
COPY exporter.py /app/exporter.py
WORKDIR /app
EXPOSE 9101
ENTRYPOINT ["python3", "-u", "/app/exporter.py"]
