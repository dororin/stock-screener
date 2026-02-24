# Use the official Python image
FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY main.py .
RUN pip install fastapi uvicorn

# Copy static files
COPY static/ ./static/

# Expose the port
EXPOSE 8080

# Run the application
CMD ["uvicorn", "main.py:app", "--host", "0.0.0.0", "--port", "8080"]
