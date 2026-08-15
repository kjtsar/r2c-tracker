# Use an official lightweight Python image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (needed for some Python math/db libraries)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*
    
# Install the reviewed dependency resolution used by the pilot image. Keep
# requirements.txt as the human-maintained input and regenerate the lock
# deliberately so an unrelated deployment cannot float package versions.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock


# Copy the application code
COPY main.py .
COPY faa_proxy.py .
COPY control_plane.py .
COPY enrollment.py .
COPY platform_admin.py .
COPY platform_admin_identity.py .
COPY platform_admin_auth.py .
COPY turn_credentials.py .
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 8080

# Run the web service on container startup
# Cloud Run provides the PORT environment variable
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
