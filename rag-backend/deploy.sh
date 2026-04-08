#!/bin/bash
set -e  # Exit on error

echo "🚀 Memulai proses update dan deploy..."

# 1. Pull kode terbaru
echo "📥 Menarik kode terbaru dari repository..."
git pull origin main

# 2. Build dan restart container
echo "🏗️  Membangun ulang image Docker..."
docker-compose up --build -d --force-recreate

# 3. Tunggu aplikasi siap (dengan timeout)
echo "⏳ Menunggu aplikasi siap (maks 60 detik)..."
START_TIME=$(date +%s)
TIMEOUT=60
while true; do
  ELAPSED=$(( $(date +%s) - START_TIME ))
  if [ $ELAPSED -ge $TIMEOUT ]; then
    echo "❌ Timeout menunggu aplikasi siap ($TIMEOUT detik)"
    docker logs --tail 50 rag-service
    exit 1
  fi
  
  # Cek health endpoint
  if curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/health | grep -q "200"; then
    echo "✅ Aplikasi siap dalam $ELAPSED detik"
    break
  fi
  
  sleep 2
done

# 4. Final health check
echo "🔍 Memeriksa kesehatan akhir..."
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/health)

if [ "$RESPONSE" -eq 200 ]; then
  echo ""
  echo "✅✅✅ DEPLOY BERHASIL! ✅✅✅"
  echo ""
  docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
  echo ""
  echo "📊 Status aplikasi:"
  curl -s http://localhost:3000/health | python3 -m json.tool
else
  echo ""
  echo "❌❌❌ DEPLOY GAGAL! Status Code: $RESPONSE ❌❌❌"
  echo ""
  echo "📋 Log error terakhir:"
  docker logs --tail 100 rag-service
  exit 1
fi