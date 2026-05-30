# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install uv for faster dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY data ./data
COPY .env.example .env.example

# Install dependencies. Include OpenBB so configured investment watchlists work
# in the container instead of being skipped as an optional source.
RUN uv sync --frozen --extra openbb --no-dev

# Create volume mount points
VOLUME ["/app/data"]

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run commands through uv. The default command is the long-running daemon;
# one-shot runs can still use: docker compose run --rm horizon horizon --hours 24
ENTRYPOINT ["uv", "run"]
CMD ["python", "-m", "src.services.daemon"]
