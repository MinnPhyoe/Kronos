# Feed, strategy and API services (no torch). Build from the Kronos repo root:
#   docker build -f forex-app/Dockerfile -t forex-app .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/forex-app
COPY forex-app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY forex-app/ .
EXPOSE 8000
CMD ["python", "-m", "forexapp.run", "api"]
