FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests

RUN pip install --no-cache-dir -e ".[dev]"

# Runs as a non-root user: many sandboxed/restricted container runtimes reject or
# constrain root processes. Nothing here needs root -- installing into site-packages
# is the only root-requiring step, done above before the user switch. /app is
# chown'd so pytest can still write its cache dir there (harmless without this --
# tests still pass -- but a permission-denied warning on every run isn't clean).
RUN useradd --create-home --uid 1000 --shell /bin/bash interleave \
    && chown -R interleave:interleave /app
USER interleave

CMD ["pytest", "-q"]
