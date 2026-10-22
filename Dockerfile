FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY leadgen ./leadgen
RUN pip install --no-cache-dir .

VOLUME /app/data
EXPOSE 8000

# One process only: the scheduler runs inside the web server.
CMD ["python", "-m", "leadgen", "serve", "--port", "8000"]
