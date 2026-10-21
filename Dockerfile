FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py gemeenten.json ./
COPY static/ static/

# Cloud Run sets $PORT; shell form so it gets expanded at container start.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 main:app
