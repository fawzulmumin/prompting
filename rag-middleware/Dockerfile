# Gunakan Node.js 20 LTS (slim untuk image lebih kecil)
FROM node:20-slim
 
# Set working directory
WORKDIR /app
 
# Copy package files lebih dulu untuk optimalisasi cache Docker layer
# Jika package.json tidak berubah, layer ini tidak akan di-rebuild
COPY package*.json ./
 
# Install hanya production dependencies
RUN npm ci --omit=dev
 
# Copy source code
COPY src/ ./src/
 
# Buat user non-root untuk keamanan (jangan jalankan sebagai root)
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 expressuser \
    && chown -R expressuser:nodejs /app
USER expressuser
 
# Set environment variables
ENV NODE_ENV=production
 
# Expose port middleware
EXPOSE 4000
 
# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://localhost:4000/api/health').then(r => r.ok ? process.exit(0) : process.exit(1)).catch(() => process.exit(1))"
 
# Jalankan aplikasi
CMD ["node", "src/server.js"]