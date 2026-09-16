FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml alembic.ini ./
COPY pulse ./pulse
COPY tests ./tests
COPY alembic ./alembic

# setuptools' build step leaves build/ and *.egg-info in the cwd; harmless at
# runtime, but they duplicate the whole source tree, which confuses
# `ruff`/`mypy` when run against `.` inside this image.
RUN pip install --no-cache-dir .[dev] && rm -rf build *.egg-info

EXPOSE 8000

CMD ["uvicorn", "pulse.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
