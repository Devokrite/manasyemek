# Use official Python image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Copy everything
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port for Flask (required by Northflank)
EXPOSE 8080

# Run your bot script
CMD ["python", "bot.py"]
