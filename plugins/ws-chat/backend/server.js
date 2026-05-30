// ws-chat backend — a single HTTP server that also hosts a WebSocket chat.
// No auth, no rooms, no persistence: every connected client sees every message.
//
// Wire protocol (JSON text frames):
//   client -> server : { type: "join", user }            on connect
//                       { type: "chat", user, text }      to say something
//   server -> client : { type: "chat",   user, text, ts } broadcast of a message
//                       { type: "system", text, ts }      join/leave notices
//                       { type: "welcome", users }         sent once on connect
import http from "node:http";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.PORT) || 8080;

const server = http.createServer((req, res) => {
  // Plain HTTP hits (health checks, nginx `/`) get a simple 200.
  res.writeHead(200, { "Content-Type": "text/plain" });
  res.end("ws-chat backend ok\n");
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
      ws.send(JSON.stringify({ type: "welcome", users: [...new Set([...clients.values()].filter(Boolean))] }));
      broadcast({ type: "system", text: `${user} joined`, ts: Date.now() });
    } else if (msg.type === "chat") {
      const user = clients.get(ws) || String(msg.user || "anon").slice(0, 40);
      const text = String(msg.text || "").slice(0, 2000);
      if (text) broadcast({ type: "chat", user, text, ts: Date.now() });
    }
  });

  ws.on("close", () => {
    const user = clients.get(ws);
    clients.delete(ws);
    if (user) broadcast({ type: "system", text: `${user} left`, ts: Date.now() });
  });
});

server.listen(PORT, () => console.log(`ws-chat backend listening on :${PORT}`));
