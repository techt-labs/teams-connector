# Standalone connector image.
#
# Build context is the `server/` directory, because the connector is a
# package under it (`connector/`) and imports itself absolutely
# (`from connector import …`). Build from there so the package resolves:
#
#     docker build -t teams-connector .
#     docker run -p 8000:8000 --env-file connector/.env teams-connector
#
# The same image runs unchanged on Azure App Service (Web App for
# Containers), Azure Container Apps, or any Kubernetes ingress — the
# platform supplies the public HTTPS URL that replaces ngrok in local
# testing. Point the Azure Bot messaging endpoint at
# https://<your-host>/api/messages.

FROM python:3.11-slim

# No .pyc writes, unbuffered stdout so container logs stream in real time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Then the package itself.
COPY connector/ ./connector/

# Run as an unprivileged user — the process only needs to bind a port
# and reach Postgres/Teams, never to write inside the image.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# One worker is correct here: the store's reference cache is per-process
# and backed by the database, so horizontal scaling is done by running
# more containers (each stateless), not more workers sharing memory.
CMD ["uvicorn", "connector.app:app", "--host", "0.0.0.0", "--port", "8000"]
