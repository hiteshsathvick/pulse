FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY pulse ./pulse
COPY tests ./tests
COPY alembic ./alembic

RUN pip install --no-cache-dir .[dev]

EXPOSE 8000

CMD ["uvicorn", "pulse.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
