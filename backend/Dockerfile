FROM python:3.12-slim

WORKDIR /app

# Dependencies get their own layer, keyed only on pyproject.toml. Source used
# to be copied first, so *every* code change invalidated the layer and
# re-installed every dependency -- 12+ minutes per build locally, and the same
# cost on every CI run. Now an ordinary source edit reuses this layer.
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; print('\n'.join(p['dependencies'] + p['optional-dependencies']['dev']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY alembic.ini ./
COPY pulse ./pulse
COPY tests ./tests
COPY alembic ./alembic

# --no-deps: everything was installed above. setuptools' build step leaves
# build/ and *.egg-info in the cwd; harmless at runtime, but they duplicate the
# whole source tree, which confuses `ruff`/`mypy` when run against `.` inside
# this image.
RUN pip install --no-cache-dir --no-deps . && rm -rf build *.egg-info

EXPOSE 8000

CMD ["uvicorn", "pulse.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
