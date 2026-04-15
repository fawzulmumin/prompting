# Gunakan Python 3.11 (stable + performa baik untuk NLP)
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies untuk sentence-transformers
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first untuk optimalisasi cache Docker layer
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy seluruh kode aplikasi
COPY . .

# Buat folder logs dan cache
RUN mkdir -p logs .cache && chmod -R 777 logs .cache

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV TRANSFORMERS_CACHE=/app/.cache
ENV PYTHON_ENV=production

# Expose port aplikasi
EXPOSE 3000

# Jalankan aplikasi dengan Uvicorn (ASGI server untuk FastAPI)
# --workers 4: optimal untuk 8-core server (half of cores)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000", "--workers", "4"]