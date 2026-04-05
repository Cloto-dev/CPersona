FROM python:3.13-slim

WORKDIR /app

# Copy all files
COPY . .

# Install dependencies
RUN pip install --no-cache-dir .

# Default environment
ENV CPERSONA_DB_PATH=/data/cpersona.db

# Expose Streamable HTTP port (optional)
EXPOSE 8400

CMD ["python", "server.py"]
