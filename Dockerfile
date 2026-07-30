FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH="/app"

# Install standard requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Google Cloud Storage client library for dynamic SQLite database pulls
RUN pip install --no-cache-dir google-cloud-storage

# Copy codebase
COPY . .

# Expose server port 8080 (Cloud Run default)
EXPOSE 8080

# Run custom dashboard server
ENTRYPOINT ["python", "dashboard/dashboard.py"]
