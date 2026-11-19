FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    HEADLESS=true \
    NO_BROWSER=1 \
    GDRIVE_FOLDER_ID=1nU0cJhO6IHaTBgoA80EzZsLxPMWB4ulx

RUN pip install --no-cache-dir python-dotenv>=1.0.0 requests>=2.32.0 rich>=13.9.0

COPY . .

RUN mkdir -p /app/data /app/downloads

EXPOSE 3000 8080

HEALTHCHECK --interval=20s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health')" || exit 1

CMD ["python3", "app.py"]
