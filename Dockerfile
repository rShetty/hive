# Agent Marketplace - Main Application
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt asyncpg

# Copy application
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY docker/ ./docker/

# Set working directory to backend
WORKDIR /app/backend

# Create directory for SQLite database
RUN mkdir -p /data

# Expose port
ENV PORT=8080
EXPOSE 8080

# Run the application
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
