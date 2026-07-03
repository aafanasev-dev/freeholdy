// imgstore storage service — the API + public image server behind
// storage.{project}.{domain}.
//
//   GET    /images/:file      public   serve an image by its sha256 filename
//   GET    /api/images        token    list uploaded images
//   POST   /api/images        token    upload an image (multipart field "image")
//   DELETE /api/images/:file  token    delete an image
//   GET    /health            public   liveness
//
// Access tokens are never stored in plaintext: gen-token.sh writes the SHA-256 hash
// of each token to /data/tokens.txt (one hex digest per line) and prints the token
// once. Here we hash the presented Bearer token and check membership — mirroring
// freeholdy's own app/auth.py::hash_token model. Image serving is intentionally
// unauthenticated so the links are shareable; the sha256 path is the secret.

const express = require("express");
const multer = require("multer");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const DATA_DIR = "/data";
const IMAGES_DIR = path.join(DATA_DIR, "images");
const TOKENS_FILE = path.join(DATA_DIR, "tokens.txt");
const PORT = 3000;
const MAX_BYTES = 50 * 1024 * 1024; // keep in step with nginx-extra.conf client_max_body_size

fs.mkdirSync(IMAGES_DIR, { recursive: true });

const EXT_BY_MIME = {
  "image/jpeg": "jpeg",
  "image/png": "png",
  "image/gif": "gif",
  "image/webp": "webp",
};
const CONTENT_TYPE_BY_EXT = {
  jpeg: "image/jpeg",
  png: "image/png",
  gif: "image/gif",
  webp: "image/webp",
};
const NAME_RE = /^[0-9a-f]{64}\.(jpeg|png|gif|webp)$/;

const sha256 = (buf) => crypto.createHash("sha256").update(buf).digest("hex");

// Valid-token hash set, re-read from disk whenever tokens.txt changes (so a token
// minted by gen-token.sh works immediately, with no restart).
let tokenCache = { mtimeMs: -1, set: new Set() };
function validTokenHashes() {
  let stat;
  try {
    stat = fs.statSync(TOKENS_FILE);
  } catch {
    tokenCache = { mtimeMs: -1, set: new Set() };
    return tokenCache.set;
  }
  if (stat.mtimeMs !== tokenCache.mtimeMs) {
    const lines = fs
      .readFileSync(TOKENS_FILE, "utf8")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    tokenCache = { mtimeMs: stat.mtimeMs, set: new Set(lines) };
  }
  return tokenCache.set;
}

function requireToken(req, res, next) {
  const m = (req.headers.authorization || "").match(/^Bearer\s+(.+)$/i);
  const token = m ? m[1].trim() : "";
  if (!token) return res.status(401).json({ error: "missing token" });
  if (!validTokenHashes().has(sha256(Buffer.from(token, "utf8")))) {
    return res.status(401).json({ error: "invalid token" });
  }
  next();
}

const app = express();
app.disable("x-powered-by");

// CORS — the web UI lives on a sibling subdomain (web.*), so its fetch() calls are
// cross-origin. We use a Bearer header (no cookies), so reflecting the origin is safe.
app.use((req, res, next) => {
  const origin = req.headers.origin;
  if (origin) res.setHeader("Access-Control-Allow-Origin", origin);
  res.setHeader("Vary", "Origin");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Authorization,Content-Type");
  if (req.method === "OPTIONS") return res.sendStatus(204);
  next();
});

app.get("/health", (_req, res) => res.json({ status: "ok" }));

// Public: serve an image by filename. No auth — the sha256 path is the capability.
app.get("/images/:file", (req, res) => {
  const file = req.params.file;
  if (!NAME_RE.test(file)) return res.status(400).json({ error: "bad filename" });
  const fp = path.join(IMAGES_DIR, file);
  if (!fs.existsSync(fp)) return res.status(404).json({ error: "not found" });
  res.setHeader("Content-Type", CONTENT_TYPE_BY_EXT[file.split(".").pop()]);
  res.setHeader("Cache-Control", "public, max-age=31536000, immutable");
  fs.createReadStream(fp).pipe(res);
});

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: MAX_BYTES },
});

app.get("/api/images", requireToken, (_req, res) => {
  const images = fs
    .readdirSync(IMAGES_DIR)
    .filter((f) => NAME_RE.test(f))
    .map((name) => {
      const st = fs.statSync(path.join(IMAGES_DIR, name));
      return { name, url: `/images/${name}`, size: st.size, uploaded: st.mtimeMs };
    })
    .sort((a, b) => b.uploaded - a.uploaded);
  res.json({ images });
});

app.post("/api/images", requireToken, upload.single("image"), (req, res) => {
  if (!req.file) return res.status(400).json({ error: "no file (field name must be 'image')" });
  const ext = EXT_BY_MIME[req.file.mimetype];
  if (!ext) return res.status(415).json({ error: "unsupported type — jpeg, png, gif or webp only" });
  // Content-addressed: same bytes -> same name (idempotent re-upload).
  const name = `${sha256(req.file.buffer)}.${ext}`;
  const fp = path.join(IMAGES_DIR, name);
  if (!fs.existsSync(fp)) fs.writeFileSync(fp, req.file.buffer);
  res.status(201).json({ name, url: `/images/${name}` });
});

app.delete("/api/images/:file", requireToken, (req, res) => {
  const file = req.params.file;
  if (!NAME_RE.test(file)) return res.status(400).json({ error: "bad filename" });
  try {
    fs.unlinkSync(path.join(IMAGES_DIR, file));
  } catch (e) {
    if (e.code === "ENOENT") return res.status(404).json({ error: "not found" });
    throw e;
  }
  res.json({ status: "ok" });
});

// eslint-disable-next-line no-unused-vars
app.use((err, _req, res, _next) => {
  if (err && err.code === "LIMIT_FILE_SIZE") {
    return res.status(413).json({ error: "file too large (max 50 MB)" });
  }
  console.error(err);
  res.status(500).json({ error: "server error" });
});

app.listen(PORT, () => console.log(`imgstore storage listening on ${PORT}`));
