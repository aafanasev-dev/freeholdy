import React, { useState, useRef, useEffect, useCallback } from "react";

// Derive the backend WebSocket URL from our own location.
// freeholdy exposes this app at  web.{project}.{domain}  and the backend at
// websocket.{project}.{domain} — so swap the leading "web." label. Falls back to
// localhost:8080 for local dev (vite on a non-"web." host).
function wsUrl() {
  const loc = window.location;
  const proto = loc.protocol === "https:" ? "wss" : "ws";
  let host = loc.host.replace(/^web\./, "websocket.");
  if (host === loc.host) host = "localhost:8080"; // dev fallback
  return `${proto}://${host}`;
}

// Chat name chosen during the interactive install (plugin install.sh pre phase →
// .env CHAT_NAME → compose build arg → Vite env), baked in at build time.
const CHAT_NAME = import.meta.env.VITE_CHAT_NAME || "ws-chat";

const S = {
  page: { fontFamily: "system-ui, sans-serif", maxWidth: 640, margin: "0 auto", padding: 16, height: "100vh", boxSizing: "border-box", display: "flex", flexDirection: "column" },
  title: { fontSize: 20, fontWeight: 700, marginBottom: 12 },
  log: { flex: 1, overflowY: "auto", border: "1px solid #ddd", borderRadius: 8, padding: 12, background: "#fafafa" },
  row: { display: "flex", gap: 8, marginTop: 12 },
  input: { flex: 1, padding: "10px 12px", border: "1px solid #ccc", borderRadius: 8, fontSize: 14 },
  btn: { padding: "10px 16px", border: "none", borderRadius: 8, background: "#3b82f6", color: "#fff", fontSize: 14, cursor: "pointer" },
  msg: { marginBottom: 6, lineHeight: 1.4, fontSize: 14 },
  sys: { color: "#999", fontStyle: "italic" },
  user: { fontWeight: 600, marginRight: 6 },
};

function Join({ onJoin }) {
  const [name, setName] = useState("");
  return (
    <div style={S.page}>
      <div style={S.title}>💬 {CHAT_NAME}</div>
      <p style={{ color: "#666" }}>Pick a username to join the chat.</p>
      <div style={S.row}>
        <input style={S.input} autoFocus placeholder="your name" value={name}
          onChange={e => setName(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && name.trim()) onJoin(name.trim()); }} />
        <button style={S.btn} disabled={!name.trim()} onClick={() => onJoin(name.trim())}>join</button>
      </div>
    </div>
  );
}

function Chat({ user }) {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [connected, setConnected] = useState(false);
  const wsRef = useRef(null);
  const logRef = useRef(null);

  useEffect(() => {
    const ws = new WebSocket(wsUrl());
    wsRef.current = ws;
    ws.onopen = () => { setConnected(true); ws.send(JSON.stringify({ type: "join", user })); };
    ws.onclose = () => setConnected(false);
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "chat" || m.type === "system") setMessages(prev => [...prev, m]);
    };
    return () => ws.close();
  }, [user]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [messages]);

  const send = useCallback(() => {
    const t = text.trim();
    if (!t || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: "chat", user, text: t }));
    setText("");
  }, [text, user]);

  return (
    <div style={S.page}>
      <div style={S.title}>
        💬 {CHAT_NAME} <span style={{ fontSize: 13, fontWeight: 400, color: connected ? "#16a34a" : "#dc2626" }}>
          {connected ? `· ${user}` : "· connecting…"}
        </span>
      </div>
      <div style={S.log} ref={logRef}>
        {messages.map((m, i) => m.type === "system" ? (
          <div key={i} style={{ ...S.msg, ...S.sys }}>{m.text}</div>
        ) : (
          <div key={i} style={S.msg}><span style={S.user}>{m.user}:</span>{m.text}</div>
        ))}
      </div>
      <div style={S.row}>
        <input style={S.input} autoFocus placeholder="type a message…" value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") send(); }} />
        <button style={S.btn} onClick={send}>send</button>
      </div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState("");
  return user ? <Chat user={user} /> : <Join onJoin={setUser} />;
}
