// ws-chat backend — a single HTTP server that also hosts a WebSocket chat.
// No auth and no rooms: every connected client sees every message. Messages ARE
// persisted — they go into a SQLite database on a docker volume, so history survives a
// restart, a redeploy, and a rollback (freeholdy versions code and images, never volume
// data). The volume is what `fhcli volumes ws-chat` lists and can tar out and back in.
//
// Wire protocol (JSON text frames):
//   client -> server : { type: "join", user }            on connect
//                       { type: "chat", user, text }      to say something
//   server -> client : { type: "chat",   user, text, ts } broadcast of a message
//                       { type: "system", text, ts }      join/leave notices (not stored)
//                       { type: "welcome", users, history } sent once on connect
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.PORT) || 8080;
// Inside the container /data is the mount point of the `chat-data` volume declared in
// docker-compose.yml. Override for local dev: CHAT_DB=./chat.db npm start
const DB_PATH = process.env.CHAT_DB || "/data/chat.db";
// How much backlog a joining client receives.
const HISTORY_LIMIT = Number(process.env.CHAT_HISTORY) || 200;

fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
const db = new DatabaseSync(DB_PATH);
// Default (rollback) journalling on purpose: WAL would leave -wal/-shm files that a
// `fhcli volume-download` taken while the stack is running could copy mid-checkpoint.
db.exec(`
  CREATE TABLE IF NOT EXISTS messages (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT    NOT NULL,
    text TEXT    NOT NULL,
    ts   INTEGER NOT NULL
  );
`);
const insertMessage = db.prepare("INSERT INTO messages (user, text, ts) VALUES (?, ?, ?)");
const recentMessages = db.prepare(
  "SELECT user, text, ts FROM messages ORDER BY id DESC LIMIT ?"
);
const countMessages = db.prepare("SELECT COUNT(*) AS n FROM messages");

// Oldest-first, which is the order the UI renders.
const history = () =>
  recentMessages.all(HISTORY_LIMIT).reverse().map(r => ({ type: "chat", ...r }));

console.log(`ws-chat store: ${DB_PATH} (${countMessages.get().n} message(s) on disk)`);

const server = http.createServer((req, res) => {
  // Plain HTTP hits (health checks, nginx `/`) get a simple 200.
  res.writeHead(200, { "Content-Type": "text/plain" });
  res.end(`ws-chat backend ok — ${countMessages.get().n} message(s) stored\n`);
});

const wss = new WebSocketServer({ server });
const clients = new Map(); // ws -> username

function broadcast(obj) {
  const data = JSON.stringify(obj);
  for (const ws of clients.keys()) {
    if (ws.readyState === ws.OPEN) ws.send(data);
  }
}

wss.on("connection", (ws) => {
  clients.set(ws, null);

  ws.on("message", (raw) => {
    let msg;
    try { msg = JSON.parse(raw.toString()); } catch { return; }

    if (msg.type === "join") {
      const user = String(msg.user || "anon").slice(0, 40);
      clients.set(ws, user);
      ws.send(JSON.stringify({
        type: "welcome",
        users: [...new Set([...clients.values()].filter(Boolean))],
        history: history(),
      }));
      broadcast({ type: "system", text: `${user} joined`, ts: Date.now() });
    } else if (msg.type === "chat") {
      const user = clients.get(ws) || String(msg.user || "anon").slice(0, 40);
      const text = String(msg.text || "").slice(0, 2000);
      if (!text) return;
      const ts = Date.now();
      insertMessage.run(user, text, ts);   // store first, so what is broadcast is what is kept
      broadcast({ type: "chat", user, text, ts });
    }
  });

  ws.on("close", () => {
    const user = clients.get(ws);
    clients.delete(ws);
    if (user) broadcast({ type: "system", text: `${user} left`, ts: Date.now() });
  });
});

server.listen(PORT, () => console.log(`ws-chat backend listening on :${PORT}`));
