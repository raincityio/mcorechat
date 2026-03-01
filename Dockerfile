# -------- Build stage --------
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /mcorechat/working

# System deps for building (safe even if pure Python)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only what is needed to build
COPY pyproject.toml .
COPY README.md .
COPY src ./src

# Install build tooling
RUN pip install --upgrade pip build

# install project
RUN python -mvenv /mcorechat
RUN /mcorechat/bin/pip install .


# -------- Runtime stage --------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY --from=builder /mcorechat /mcorechat

# Run your module
CMD ["/mcorechat/bin/mcorechat", "-c", "/config/mcorechat.yml"]

