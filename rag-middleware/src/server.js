// server.js (Express Bridge)
import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import helmet from "helmet";
import rateLimit from "express-rate-limit";
import morgan from "morgan";
import { randomUUID } from "crypto";

dotenv.config();

const app = express(); 

const PYTHON_BASE_URL = process.env.PYTHON_API_URL || "http://localhost:3000";

// ── Security headers ──
app.use(helmet());

// ── Request ID (untuk tracing log) ──
app.use((req, _res, next) => {
  req.id = req.headers["x-request-id"] || randomUUID();
  next();
});

// ── Logging ──
morgan.token("id", (req) => req.id);
app.use(morgan(":id :method :url :status :response-time ms"));

// ── CORS & Body Parser ──
app.use(cors({ origin: process.env.FRONTEND_URL || "http://localhost:5173" }));
app.use(express.json());

// ── Rate limiting ──
const limiter = rateLimit({
  windowMs: 60 * 1000,
  max: 30,
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: "Terlalu banyak request, coba lagi dalam 1 menit." },
});
app.use("/api/ask", limiter);

// // ── Optional API KEY Auth ──
// const API_KEY = process.env.API_KEY;
// if (API_KEY) {
//   app.use("/api", (req, res, next) => {
//     if (req.headers["x-api-key"] !== API_KEY) {
//       return res.status(401).json({ error: "Unauthorized" });
//     }
//     next();
//   });
// }

// ── Proxy: POST /api/ask → Python ──
app.post("/api/ask", async (req, res) => {
  const { question, user_id, level, lat, lon, radius_km } = req.body;

  // ── Validasi input ──
  if (!question || typeof question !== "string" || question.trim().length === 0) {
    return res.status(400).json({ error: "Field 'question' wajib diisi." });
  }
  if (question.length > 1000) {
    return res.status(400).json({ error: "Question terlalu panjang (max 1000 karakter)." });
  }
  if (lat !== undefined && (typeof lat !== "number" || lat < -90 || lat > 90)) {
    return res.status(400).json({ error: "Nilai 'lat' tidak valid." });
  }
  if (lon !== undefined && (typeof lon !== "number" || lon < -180 || lon > 180)) {
    return res.status(400).json({ error: "Nilai 'lon' tidak valid." });
  }

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60_000);  // 30s → 60s

    const response = await fetch(`${PYTHON_BASE_URL}/ask`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Request-Id": req.id,
      },
      body: JSON.stringify({
        question: question.trim(),
        user_id,
        level,
        lat,
        lon,
        radius_km,
      }),
      signal: controller.signal,
    });

    clearTimeout(timeout);

    if (!response.ok) {
      const err = await response.text();
      return res.status(response.status).json({ error: err });
    }

    const data = await response.json();
    res.json(data);
  } catch (err) {
    if (err.name === "AbortError") {
      return res.status(504).json({ error: "Python backend timeout setelah 60 detik." });
    }
    res.status(502).json({
      error: "Python backend tidak dapat dijangkau",
      detail: err.message,
    });
  }
});

// ── Proxy: GET /api/health ──
app.get("/api/health", async (_req, res) => {
  try {
    const response = await fetch(`${PYTHON_BASE_URL}/health`);
    const data = await response.json();
    res.json(data);
  } catch {
    res.status(502).json({ status: "unreachable" });
  }
});

// ── Start server ──
const PORT = process.env.EXPRESS_PORT || 4000;

const server = app.listen(PORT,'0.0.0.0', () =>
  console.log(`Express bridge running on :${PORT}`)
);

// ── Graceful shutdown ──
const shutdown = (signal) => {
  console.log(`${signal} received, shutting down gracefully...`);
  server.close(() => {
    console.log("Server closed.");
    process.exit(0);
  });
  setTimeout(() => process.exit(1), 10_000);
};

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));