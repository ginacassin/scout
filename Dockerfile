# Same base and conventions as the Cinematic Concierge bot (multi-arch, runs on the Pi 5)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Madrid

WORKDIR /usr/src/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Dokploy restarts the container if no poll has succeeded for 6 hours
HEALTHCHECK --interval=10m --timeout=10s --start-period=2m \
    CMD python -m scout.healthcheck || exit 1

CMD ["python", "app.py"]
