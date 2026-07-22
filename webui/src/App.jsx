import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { zip as fflateZip } from "fflate";
import "@xterm/xterm/css/xterm.css";

const BASE = import.meta.env.VITE_API_URL || "https://api.your_domain.com";
// The server's base domain, derived from the API URL (strip the scheme + the `api.`
// host prefix): e.g. https://api.acme.com → acme.com. Projects are served under it.
const DOMAIN = BASE.replace(/^https?:\/\/api\./, "").replace(/^https?:\/\//, "");
const POLL_MS = 1000;
const CHUNK_SIZE = 1024 * 1024;   // 1 MiB pieces — stays under nginx's 1 MB default body limit

// ── Deploy history (localStorage) ─────────────────────────────────────────────
// Remember each project's last deploy source so redeploys pre-fill. Git URLs are fully
// reusable (URL + branch); local files/folder can only be remembered as a hint (browsers
// never expose a file path and can't re-read files without a fresh user pick).
// Shape: { projects: { [name]: { srcKind, gitUrl, branch, label, ts } },
//          recentGitUrls: [ { gitUrl, branch, name, ts }, … ] }   // srcKind ∈ git|files|folder
const DEPLOY_HISTORY_KEY = "freeholdy_deploy_history";
const RECENT_GIT_CAP = 8;

const loadDeployHistory = () => {
  try {
    const h = JSON.parse(localStorage.getItem(DEPLOY_HISTORY_KEY) || "{}");
    return { projects: h.projects || {}, recentGitUrls: h.recentGitUrls || [] };
  } catch { return { projects: {}, recentGitUrls: [] }; }
};

const getProjectDeploy = (name) => loadDeployHistory().projects[name] || null;
const getRecentGitUrls = () => loadDeployHistory().recentGitUrls;

const saveProjectDeploy = (name, entry) => {
  try {
    const h = loadDeployHistory();
    const full = { ...entry, ts: Date.now() };
    h.projects[name] = full;
    if (entry.srcKind === "git" && entry.gitUrl) {
      h.recentGitUrls = [
        { gitUrl: entry.gitUrl, branch: entry.branch || "", name, ts: full.ts },
        ...h.recentGitUrls.filter(r => r.gitUrl !== entry.gitUrl),
      ].slice(0, RECENT_GIT_CAP);
    }
    localStorage.setItem(DEPLOY_HISTORY_KEY, JSON.stringify(h));
  } catch { /* localStorage full / disabled — remembering is best-effort */ }
};

// Human-readable label for a files/folder selection, e.g. "folder 'myapp' · 42 files".
const describeSelection = (srcKind, entries, rootName) =>
  srcKind === "folder" && rootName
    ? `folder '${rootName}' · ${entries.length} file${entries.length !== 1 ? "s" : ""}`
    : `${entries.length} file${entries.length !== 1 ? "s" : ""}`;

// ── API factory ───────────────────────────────────────────────────────────────
const mkApi = (token) => {
  const h = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const hf = { Authorization: `Bearer ${token}` };
  const unwrap = async (r) => {
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.detail || `HTTP ${r.status}`);
    }
    return r.json();
  };
  const hr = { Authorization: `Bearer ${token}`, "Content-Type": "application/octet-stream" };
  return {
    get:  (p)    => fetch(`${BASE}${p}`, { headers: h }).then(unwrap),
    post: (p, b) => fetch(`${BASE}${p}`, { method: "POST", headers: h, body: b != null ? JSON.stringify(b) : undefined }).then(unwrap),
    put:  (p, b) => fetch(`${BASE}${p}`, { method: "PUT", headers: h, body: b != null ? JSON.stringify(b) : undefined }).then(unwrap),
    form: (p, fd) => fetch(`${BASE}${p}`, { method: "POST", headers: hf, body: fd }).then(unwrap),
    raw:  (p, body) => fetch(`${BASE}${p}`, { method: "POST", headers: hr, body }).then(unwrap),
    del:  (p)    => fetch(`${BASE}${p}`, { method: "DELETE", headers: h }).then(unwrap),
  };
};

// ── Theme ─────────────────────────────────────────────────────────────────────
// Grafana-style dark palette: near-black chrome, flat bordered panels, a
// Grafana-orange primary accent (C.brand), and translucent tint tokens so button
// variants / chips / badges read correctly on the dark surfaces. Green/amber/red
// stay reserved for status semantics; blue for links/info.
const C = {
  bg:     "#111217",   // app background
  s1:     "#181b1f",   // primary panel / card surface
  s2:     "#1f2329",   // nav rail / header / secondary surface
  s3:     "#22252b",   // tertiary surface (default buttons, fills, code bg)
  bd:     "#2c3235",   // subtle border
  bdB:    "#3d444b",   // stronger border
  brand:  "#F55F3E",   // Grafana orange — brand / primary accent
  brandH: "#ff780a",   // brighter orange (hover)
  green:  "#6ccf8e",   // success / running
  amber:  "#ff9830",   // warning / exited
  red:    "#e5564f",   // error / danger
  blue:   "#6e9fff",   // info / links
  txt:    "#ccccdc",   // primary text
  muted:  "#8e8e98",   // secondary text
  dim:    "#5b5e63",   // tertiary text / placeholders
  shadow: "0 1px 2px rgba(0,0,0,.4), 0 4px 14px rgba(0,0,0,.35)",
  ff:     "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  mono:   "'Roboto Mono', 'JetBrains Mono', monospace",
  // Translucent tint tokens (fill + border) for variant buttons, chips, badges.
  purple: "#a970ff",   // versions button accent (distinct from the orange brand)
  brandFill: "rgba(245,95,62,.14)",   brandBd: "rgba(245,95,62,.42)",
  purpleFill:"rgba(169,112,255,.15)", purpleBd:"rgba(169,112,255,.42)",
  blueFill:  "rgba(110,159,255,.14)", blueBd:  "rgba(110,159,255,.42)",
  greenFill: "rgba(108,207,142,.14)", greenBd: "rgba(108,207,142,.40)",
  amberFill: "rgba(255,152,48,.14)",  amberBd:  "rgba(255,152,48,.40)",
  redFill:   "rgba(229,86,79,.16)",   redBd:    "rgba(229,86,79,.42)",
};

const SC = {
  running: C.green, done: C.green,
  exited: C.amber, aborted: C.amber,
  error: C.red,
  no_image: C.muted, not_found: C.muted, no_job: C.muted,
  waiting_interactive: C.blue, connecting: C.muted,
};
const SI = {
  running: "▶", done: "✓", exited: "■", aborted: "⚠",
  error: "✗", no_image: "○", not_found: "○", no_job: "—",
  waiting_interactive: "⌨", connecting: "…",
};

// ── Tiny shared components ────────────────────────────────────────────────────
const Btn = ({ children, onClick, v = "default", sm, disabled, busy, title, style: st = {} }) => {
  const vs = {
    default: { bg: C.s3,           color: C.txt,     bd: C.bdB },
    primary: { bg: C.brandFill,    color: C.brand,   bd: C.brandBd },
    danger:  { bg: C.redFill,      color: C.red,     bd: C.redBd },
    amber:   { bg: C.amberFill,    color: C.amber,   bd: C.amberBd },
    ghost:   { bg: "transparent",  color: C.muted,   bd: "transparent" },
    blue:    { bg: C.blueFill,     color: C.blue,    bd: C.blueBd },
    purple:  { bg: C.purpleFill,   color: C.purple,  bd: C.purpleBd },
    green:   { bg: C.green,        color: "#0b0c0e", bd: C.green },
  };
  const vv = vs[v] || vs.default;
  return (
    <button onClick={onClick} disabled={disabled || busy} title={title} style={{
      background: vv.bg, color: (disabled || busy) ? C.dim : vv.color,
      border: `1px solid ${(disabled || busy) ? C.bd : vv.bd}`,
      fontFamily: C.ff, fontSize: sm ? "11px" : "12px", fontWeight: 500,
      padding: sm ? "3px 9px" : "7px 15px", cursor: (disabled || busy) ? "not-allowed" : "pointer",
      borderRadius: "8px", opacity: (disabled || busy) ? 0.55 : 1,
      whiteSpace: "nowrap", letterSpacing: "0.01em",
      ...st,
    }}>
      {busy ? "…" : children}
    </button>
  );
};

const Tag = ({ status }) => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: "5px", fontFamily: C.ff, fontSize: "11px", color: SC[status] || C.muted }}>
    <span style={{ fontSize: "7px" }}>●</span>{SI[status]} {status}
  </span>
);

const Field = ({ label, children, style: st = {} }) => (
  <div style={st}>
    {label && <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.1em", marginBottom: "5px" }}>{label}</div>}
    {children}
  </div>
);

const TextIn = ({ value, onChange, placeholder, type = "text", style: st = {} }) => (
  <input
    type={type} value={value} placeholder={placeholder}
    onChange={e => onChange(e.target.value)}
    style={{
      background: C.s2, border: `1px solid ${C.bdB}`, color: C.txt,
      fontFamily: C.ff, fontSize: "12px", padding: "8px 11px",
      borderRadius: "8px", outline: "none", width: "100%", boxSizing: "border-box", ...st,
    }}
  />
);

const Err = ({ msg }) => msg ? (
  <div style={{ color: C.red, fontFamily: C.ff, fontSize: "11px", padding: "6px 10px", background: C.redFill, border: `1px solid ${C.redBd}`, borderRadius: "8px" }}>
    ✗ {msg}
  </div>
) : null;

const Ok = ({ msg }) => msg ? (
  <div style={{ color: C.green, fontFamily: C.ff, fontSize: "11px", padding: "6px 10px", background: C.greenFill, border: `1px solid ${C.greenBd}`, borderRadius: "8px" }}>
    ✓ {msg}
  </div>
) : null;

// ── Minimal Markdown renderer ───────────────────────────────────────────────────
// Just enough for plugin ABOUT.md detail panes: headings, paragraphs, bullet lists,
// fenced code, and inline **bold** / `code` / [links](url). Not a full CommonMark parser.
const mdInline = (text, kp) => {
  const out = [];
  const re = /(\*\*([^*]+)\*\*)|(`([^`]+)`)|(\[([^\]]+)\]\(([^)]+)\))/g;
  let last = 0, m, i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[2] !== undefined) out.push(<strong key={`${kp}b${i}`} style={{ color: C.txt, fontWeight: 600 }}>{m[2]}</strong>);
    else if (m[4] !== undefined) out.push(<code key={`${kp}c${i}`} style={{ fontFamily: C.mono, fontSize: "11px", background: C.s3, border: `1px solid ${C.bd}`, borderRadius: "5px", padding: "1px 5px", color: C.txt }}>{m[4]}</code>);
    else if (m[6] !== undefined) out.push(<a key={`${kp}a${i}`} href={m[7]} target="_blank" rel="noreferrer" style={{ color: C.blue, textDecoration: "none" }}>{m[6]}</a>);
    last = re.lastIndex; i++;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
};

const Markdown = ({ text }) => {
  if (!text) return null;
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      const buf = []; i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) { buf.push(lines[i]); i++; }
      i++; blocks.push({ type: "code", text: buf.join("\n") }); continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { blocks.push({ type: "h", level: h[1].length, text: h[2] }); i++; continue; }
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*]\s+/, "")); i++; }
      blocks.push({ type: "ul", items }); continue;
    }
    if (line.trim() === "") { i++; continue; }
    const buf = [line]; i++;
    while (i < lines.length && lines[i].trim() !== "" && !/^#{1,4}\s+/.test(lines[i]) && !/^\s*[-*]\s+/.test(lines[i]) && !lines[i].trim().startsWith("```")) { buf.push(lines[i]); i++; }
    blocks.push({ type: "p", text: buf.join(" ") });
  }
  const hSize = { 1: "20px", 2: "15px", 3: "12px", 4: "11px" };
  return (
    <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "13px", lineHeight: "1.7" }}>
      {blocks.map((b, k) => {
        if (b.type === "h") return (
          <div key={k} style={{ color: b.level === 1 ? C.txt : C.brand, fontWeight: b.level === 1 ? 700 : 600, fontSize: hSize[b.level] || "12px", letterSpacing: b.level >= 3 ? "0.06em" : 0, textTransform: b.level >= 3 ? "uppercase" : "none", margin: k === 0 ? "0 0 10px" : "18px 0 8px" }}>{mdInline(b.text, `h${k}`)}</div>
        );
        if (b.type === "ul") return (
          <ul key={k} style={{ margin: "0 0 12px", paddingLeft: "18px" }}>
            {b.items.map((it, j) => <li key={j} style={{ margin: "4px 0" }}>{mdInline(it, `l${k}-${j}-`)}</li>)}
          </ul>
        );
        if (b.type === "code") return (
          <pre key={k} style={{ background: C.s3, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "10px 12px", fontFamily: C.mono, fontSize: "11px", color: C.txt, overflow: "auto", margin: "0 0 12px", lineHeight: "1.6" }}>{b.text}</pre>
        );
        return <p key={k} style={{ margin: "0 0 12px" }}>{mdInline(b.text, `p${k}`)}</p>;
      })}
    </div>
  );
};

// ── Log pane ──────────────────────────────────────────────────────────────────
const LogPane = ({ log, onClose, onAbort }) => {
  const ref = useRef();
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [log?.logs]);
  if (!log) return null;
  return (
    <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", display: "flex", flexDirection: "column", height: "280px", marginTop: "10px", boxShadow: C.shadow }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "5px 12px", borderBottom: `1px solid ${C.bd}`, background: C.s2 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.12em" }}>OP LOG</span>
          <span style={{ color: C.blue, fontFamily: C.ff, fontSize: "10px" }}>{log.project} → {log.operation}</span>
          <Tag status={log.status} />
        </div>
        <div style={{ display: "flex", gap: "6px" }}>
          {log.status === "running" && <Btn v="amber" sm onClick={onAbort}>abort</Btn>}
          <Btn v="ghost" sm onClick={onClose}>✕</Btn>
        </div>
      </div>
      <div ref={ref} style={{ flex: 1, overflow: "auto", padding: "10px 14px", fontFamily: C.mono, fontSize: "11px", color: C.txt, lineHeight: "1.65", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
        {log.logs || <span style={{ color: C.dim }}>waiting for output…</span>}
      </div>
    </div>
  );
};

// ── Install pane ──────────────────────────────────────────────────────────────
// Watches a plugin install over a WebSocket (/plugins/{plugin}/install/{project}). The
// build always streams live here (no status polling). For interactive plugins the input
// row drives install.sh's prompts (echo is disabled server-side, so sent lines are echoed
// locally); for non-interactive plugins it is read-only and just shows the build. The
// exit frame reports the build result; reconnecting re-attaches (interactive re-runs
// install.sh if the project isn't built yet, otherwise re-streams the running build).
const InstallPane = ({ token, wsPath, project, interactive, onExit, onClose }) => {
  const [lines, setLines] = useState("");
  const [status, setStatus] = useState("connecting");
  const [input, setInput] = useState("");
  const [attempt, setAttempt] = useState(0);   // bump to reconnect
  const wsRef = useRef(null);
  const scrollRef = useRef();
  const exitedRef = useRef(false);
  const retriedBusyRef = useRef(false);
  const onExitRef = useRef(onExit);
  onExitRef.current = onExit;

  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [lines]);

  useEffect(() => {
    exitedRef.current = false;
    setStatus("connecting");
    const ws = new WebSocket(BASE.replace(/^http/, "ws") + wsPath);
    wsRef.current = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "ready") setStatus("running");
      else if (msg.type === "stdout") setLines(l => l + msg.data);
      else if (msg.type === "error") setLines(l => l + `\n✗ ${msg.message}\n`);
      else if (msg.type === "exit") {
        exitedRef.current = true;
        setStatus(msg.code === 0 ? "done" : "error");
        if (msg.code !== 0) setLines(l => l + `\n✗ install exited with code ${msg.code} — reconnect to retry\n`);
        onExitRef.current(msg.code);
      }
    };
    ws.onclose = (ev) => {
      if (exitedRef.current) return;
      // 4409 = session slot busy. In dev, React StrictMode mounts twice and the first
      // socket's cleanup frees the slot just after the second is rejected — retry once.
      if (ev.code === 4409 && !retriedBusyRef.current) {
        retriedBusyRef.current = true;
        setTimeout(() => setAttempt(a => a + 1), 600);
        return;
      }
      setStatus("aborted");
      setLines(l => l + "\n⚠ session closed — reconnect to resume\n");
    };
    return () => { ws.close(); };
  }, [wsPath, token, attempt]);

  const send = () => {
    if (status !== "running" || wsRef.current?.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: "stdin", data: input + "\n" }));
    setLines(l => l + input + "\n");
    setInput("");
  };

  return (
    <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", display: "flex", flexDirection: "column", height: "280px", marginTop: "10px", boxShadow: C.shadow }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "5px 12px", borderBottom: `1px solid ${C.bd}`, background: C.s2 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.12em" }}>INSTALL</span>
          <span style={{ color: C.blue, fontFamily: C.ff, fontSize: "10px" }}>{project} → {interactive ? "install.sh" : "build"}</span>
          <Tag status={status} />
        </div>
        <div style={{ display: "flex", gap: "6px" }}>
          {(status === "aborted" || status === "error") && (
            <Btn v="blue" sm onClick={() => { retriedBusyRef.current = false; setAttempt(a => a + 1); }}>↻ reconnect</Btn>
          )}
          <Btn v="ghost" sm onClick={onClose}>✕</Btn>
        </div>
      </div>
      <div ref={scrollRef} style={{ flex: 1, overflow: "auto", padding: "10px 14px", fontFamily: C.mono, fontSize: "11px", color: C.txt, lineHeight: "1.65", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
        {lines || <span style={{ color: C.dim }}>connecting to install session…</span>}
      </div>
      {interactive && (
        <div style={{ display: "flex", gap: "8px", padding: "8px 12px", borderTop: `1px solid ${C.bd}`, background: C.s2 }}>
          <input
            value={input} placeholder={status === "running" ? "type your answer, Enter to send" : "…"}
            disabled={status !== "running"}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") send(); }}
            style={{
              flex: 1, background: C.s1, border: `1px solid ${C.bdB}`, color: C.txt,
              fontFamily: C.mono, fontSize: "11px", padding: "6px 10px",
              borderRadius: "8px", outline: "none",
            }}
          />
          <Btn v="primary" sm onClick={send} disabled={status !== "running"}>send</Btn>
        </div>
      )}
    </div>
  );
};

// ── Modal wrapper ─────────────────────────────────────────────────────────────
const Modal = ({ onClose, width = 460, children }) => (
  <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", backdropFilter: "blur(2px)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999 }} onClick={onClose}>
    <div style={{ background: C.s1, border: `1px solid ${C.bdB}`, borderRadius: "8px", padding: "22px", width, maxWidth: "95vw", boxShadow: "0 24px 64px rgba(0,0,0,.6)" }} onClick={e => e.stopPropagation()}>
      {children}
    </div>
  </div>
);

const ModalHeader = ({ title, color = C.blue, onClose }) => (
  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
    <span style={{ color, fontFamily: C.ff, fontSize: "11px", letterSpacing: "0.1em" }}>{title}</span>
    <Btn v="ghost" sm onClick={onClose}>✕</Btn>
  </div>
);

// ── Git SSH key modal ─────────────────────────────────────────────────────────
// Fetches the server's GitHub public key (GET /git/key, creating it server-side on
// first use) so the user can add it to GitHub and clone private repos over SSH.
const GitKeyModal = ({ token, onClose }) => {
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    mkApi(token).get("/git/key")
      .then(setData)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [token]);

  const copy = () => {
    navigator.clipboard.writeText(data.public_key).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <Modal onClose={onClose} width={560}>
      <ModalHeader title="GIT SSH KEY" color={C.blue} onClose={onClose} />
      {loading ? (
        <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px", padding: "20px", textAlign: "center" }}>loading…</div>
      ) : error ? (
        <Err msg={error} />
      ) : (
        <>
          <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px", marginBottom: "10px" }}>
            {data.created
              ? "Generated a new GitHub SSH key on the server."
              : "The server already has a GitHub SSH key."}
          </div>
          <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px", letterSpacing: "0.08em", marginBottom: "6px" }}>PUBLIC KEY</div>
          <div style={{ background: C.s3, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "10px 12px", fontFamily: C.mono, fontSize: "11px", color: C.txt, lineHeight: "1.6", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: "120px", overflow: "auto", marginBottom: "10px" }}>
            {data.public_key}
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "14px" }}>
            <Btn v="blue" sm onClick={copy}>{copied ? "✓ copied" : "📋 copy"}</Btn>
          </div>
          <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px", lineHeight: "1.7", whiteSpace: "pre-wrap", background: C.s2, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "10px 12px" }}>
            {data.instructions}
          </div>
        </>
      )}
    </Modal>
  );
};

// ── Exec terminal ─────────────────────────────────────────────────────────────
// A full interactive shell over a WebSocket: an xterm.js terminal bridged to
// `docker exec -it` on the server (/projects/{name}/exec, or .../services/{svc}/exec for
// compose). auth frame first, then raw stdin/stdout frames + resize on fit. Closing the
// modal closes the socket, which kills the exec server-side.
const ExecTerminal = ({ token, project, service, label, onClose }) => {
  const mountRef = useRef(null);
  const [status, setStatus] = useState("connecting");
  const [attempt, setAttempt] = useState(0);
  const retriedBusyRef = useRef(false);

  useEffect(() => {
    const term = new Terminal({
      fontFamily: C.mono, fontSize: 12, cursorBlink: true, convertEol: false,
      theme: { background: "#0d0e12", foreground: "#ccccdc", cursor: "#F55F3E" },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(mountRef.current);
    fit.fit();

    const wsPath = service
      ? `/projects/${project}/services/${service}/exec`
      : `/projects/${project}/exec`;
    const ws = new WebSocket(BASE.replace(/^http/, "ws") + wsPath);
    const sendResize = () => {
      if (ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
    };

    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "ready") { setStatus("running"); fit.fit(); sendResize(); term.focus(); }
      else if (msg.type === "stdout") term.write(msg.data);
      else if (msg.type === "error") { term.write(`\r\n\x1b[31m${msg.message}\x1b[0m\r\n`); setStatus("error"); }
      else if (msg.type === "exit") { setStatus(msg.code === 0 ? "done" : "error"); term.write(`\r\n\x1b[2m[exited ${msg.code}]\x1b[0m\r\n`); }
    };
    ws.onclose = (ev) => {
      if (ev.code === 4409 && !retriedBusyRef.current) {
        retriedBusyRef.current = true;
        setTimeout(() => setAttempt(a => a + 1), 400);
        return;
      }
      setStatus(s => (s === "running" || s === "connecting") ? "aborted" : s);
    };

    const dataSub = term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stdin", data: d }));
    });
    const onWinResize = () => { fit.fit(); sendResize(); };
    window.addEventListener("resize", onWinResize);

    return () => {
      window.removeEventListener("resize", onWinResize);
      dataSub.dispose();
      ws.close();
      term.dispose();
    };
  }, [token, project, service, attempt]);

  return (
    <Modal onClose={onClose} width={780}>
      <ModalHeader title="EXEC SHELL" color={C.amber} onClose={onClose} />
      <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "10px" }}>
        <span style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px" }}>{label || project}</span>
        <Tag status={status} />
      </div>
      <div ref={mountRef} style={{ height: "420px", background: "#0d0e12", borderRadius: "8px", padding: "8px 10px", overflow: "hidden" }} />
    </Modal>
  );
};

// ── Install plugin modal ──────────────────────────────────────────────────────
const InstallPluginModal = ({ token, plugin, onClose, onInstalled }) => {
  const [name, setName] = useState(plugin.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    if (!name.trim()) return setError("Project name is required");
    setError(""); setBusy(true);
    try {
      const data = await mkApi(token).post(`/plugins/${plugin.name}/add`, { project_name: name.trim() });
      onInstalled(data);
      onClose();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  return (
    <Modal onClose={onClose}>
      <ModalHeader title="INSTALL PLUGIN" color={C.brand} onClose={onClose} />
      <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginBottom: "14px" }}>
        {plugin.name} · {plugin.deploy_mode === "compose" ? "compose" : `port ${plugin.container_port}`}
      </div>
      <Field label="PROJECT NAME" style={{ marginBottom: "12px" }}>
        <TextIn value={name} onChange={setName} placeholder="myapp" />
      </Field>
      {error && <Err msg={error} />}
      <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "12px" }}>
        <Btn v="ghost" onClick={onClose} disabled={busy}>cancel</Btn>
        <Btn v="primary" onClick={submit} busy={busy}>install</Btn>
      </div>
    </Modal>
  );
};

// ── Confirm modal ─────────────────────────────────────────────────────────────
const ConfirmModal = ({ message, onConfirm, onCancel, loading }) => (
  <Modal onClose={onCancel}>
    <div style={{ color: C.red, fontFamily: C.ff, fontSize: "11px", marginBottom: "18px", lineHeight: "1.6" }}>⚠ {message}</div>
    <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
      <Btn v="ghost" onClick={onCancel} disabled={loading}>cancel</Btn>
      <Btn v="danger" onClick={onConfirm} busy={loading}>confirm delete</Btn>
    </div>
  </Modal>
);

// ── Upload modal (unified: file or folder → autodetect + provision) ─────────────
// Strip the leading folder segment from a webkitRelativePath ("pkg/src/a.js" → "src/a.js")
// so files land at the project root, matching the CLI's relpath-from-LOCAL_DIR semantics.
const stripRoot = (p) => p.split("/").slice(1).join("/") || p;

// Zip `entries` ([{ file, rel }]), stream the archive to the server in 1 MiB chunks
// (reporting progress via `onProgress({ sent, total })`), then ask it to reassemble +
// unzip + provision. Returns the `/upload/complete` response. The project is created
// server-side if it doesn't exist yet (deploy auto-creates). Shared by UploadModal (per-card
// redeploy) and DeployForm (new project). Mirrors `fhcli deploy` with a local path.
const chunkedDeploy = async (api, project, entries, onProgress) => {
  const fileMap = {};
  for (const { file, rel } of entries) fileMap[rel] = new Uint8Array(await file.arrayBuffer());
  const zipped = await new Promise((resolve, reject) =>
    fflateZip(fileMap, (err, data) => (err ? reject(err) : resolve(data))));

  const total = zipped.length;
  const uploadId = crypto.randomUUID().replace(/-/g, "");
  onProgress && onProgress({ sent: 0, total });
  for (let offset = 0; offset < total; offset += CHUNK_SIZE) {
    const end = Math.min(offset + CHUNK_SIZE, total);
    await api.raw(`/projects/${project}/upload/chunk?upload_id=${uploadId}&offset=${offset}`,
                  zipped.subarray(offset, end));
    onProgress && onProgress({ sent: end, total });
  }
  return api.post(`/projects/${project}/upload/complete`, { upload_id: uploadId, total_size: total });
};

const UploadModal = ({ token, project, onClose, onUploaded, onDeploy }) => {
  const prev = useMemo(() => getProjectDeploy(project), [project]);
  const [mode, setMode] = useState(prev?.srcKind === "git" ? "git" : "files");   // "files" | "git"
  const [entries, setEntries] = useState([]);   // [{ file, rel }]
  const [srcKind, setSrcKind] = useState("files");   // "files" | "folder" — which picker fired
  const [rootName, setRootName] = useState("");      // top folder name of a folder pick
  const [gitUrl, setGitUrl] = useState(prev?.srcKind === "git" ? (prev.gitUrl || "") : "");
  const [branch, setBranch] = useState(prev?.srcKind === "git" ? (prev.branch || "") : "");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(null);   // { sent, total } in bytes
  const dirRef = useRef();

  // React drops the non-standard directory attributes, so set them on mount / mode switch.
  useEffect(() => {
    if (dirRef.current) {
      dirRef.current.setAttribute("webkitdirectory", "");
      dirRef.current.setAttribute("directory", "");
    }
  }, [mode]);

  const pickFiles = (fileList) => {
    setError(""); setResult(null); setSrcKind("files"); setRootName("");
    setEntries([...fileList].map(f => ({ file: f, rel: f.name })));
  };
  const pickFolder = (fileList) => {
    setError(""); setResult(null); setSrcKind("folder");
    setRootName((fileList[0]?.webkitRelativePath || "").split("/")[0] || "");
    setEntries([...fileList].map(f => ({ file: f, rel: stripRoot(f.webkitRelativePath || f.name) })));
  };

  const submit = async () => {
    setError(""); setResult(null); setBusy(true); setProgress(null);
    try {
      if (mode === "git") {
        if (!gitUrl.trim()) { setBusy(false); return setError("Git URL is required"); }
        // Same idempotent get-or-create-then-redeploy path as DeployForm's git tab.
        const data = await mkApi(token).post("/git/add", { name: project, git_url: gitUrl.trim(), branch: branch.trim() || null });
        saveProjectDeploy(project, { srcKind: "git", gitUrl: gitUrl.trim(), branch: branch.trim(), label: gitUrl.trim() });
        onDeploy && onDeploy(data);   // git always provisions → stream the build in InstallPane
        onClose();
        return;
      }
      // Files: zip the selection, stream it to the server in 1 MiB chunks (progress bar),
      // then reassemble + unzip + provision. Mirrors `fhcli deploy` (local path).
      if (!entries.length) { setBusy(false); return setError("Select a file or folder first"); }
      const data = await chunkedDeploy(mkApi(token), project, entries, setProgress);
      saveProjectDeploy(project, { srcKind, gitUrl: "", branch: "", label: describeSelection(srcKind, entries, rootName) });
      // When a manifest is provisioned the server auto-launches build + run and returns a
      // ws_path; hand off to the InstallPane to stream the deploy live (same as git/plugin
      // installs). A plain file sync (no manifest) just shows its result here.
      if (data.provisioned && data.ws_path && onDeploy) {
        onDeploy(data);
        onClose();
        return;
      }
      setResult(data);
      onUploaded && onUploaded();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const dropStyle = { flex: 1, display: "block", border: `2px dashed ${C.bdB}`, borderRadius: "8px", padding: "16px", textAlign: "center", background: C.s1, cursor: "pointer" };
  const tab = (m, label) => (
    <button onClick={() => { setMode(m); setError(""); setResult(null); }} style={{
      flex: 1, padding: "8px", cursor: "pointer", fontFamily: C.ff, fontSize: "11px", fontWeight: 600,
      background: mode === m ? C.brandFill : C.s3, color: mode === m ? C.brand : C.muted,
      border: `1px solid ${mode === m ? C.brandBd : C.bd}`, borderRadius: "8px",
    }}>{label}</button>
  );

  return (
    <Modal onClose={onClose} width={540}>
      <ModalHeader title="DEPLOY" color={C.brand} onClose={onClose} />
      <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginBottom: "14px", lineHeight: "1.6" }}>
        {project} · a <span style={{ color: C.txt }}>Dockerfile</span> or <span style={{ color: C.txt }}>docker-compose.yml</span> in the
        deployed root sets the deploy mode and provisions automatically (compose wins).
      </div>

      <div style={{ display: "flex", gap: "8px", marginBottom: "12px" }}>
        {tab("files", "files / folder")}
        {tab("git", "git URL")}
      </div>

      {mode === "files" ? (
        <>
          {prev && prev.srcKind !== "git" && !entries.length && (
            <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginBottom: "10px" }}>
              last deploy: <span style={{ color: C.txt }}>{prev.label}</span> — reselect to redeploy
            </div>
          )}
          <div style={{ display: "flex", gap: "8px", marginBottom: "12px" }}>
            <label htmlFor="file-up" style={dropStyle}>
              <input id="file-up" type="file" multiple onChange={e => pickFiles(e.target.files)} style={{ display: "none" }} />
              <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px" }}>select file(s)</div>
            </label>
            <label htmlFor="folder-up" style={dropStyle}>
              <input id="folder-up" ref={dirRef} type="file" multiple onChange={e => pickFolder(e.target.files)} style={{ display: "none" }} />
              <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px" }}>select a folder</div>
            </label>
          </div>

          {entries.length > 0 && !progress && (
            <div style={{ color: C.green, fontFamily: C.ff, fontSize: "11px", marginBottom: "12px" }}>
              ✓ {entries.length} file(s) selected
            </div>
          )}

          {progress && (
            <div style={{ marginBottom: "12px" }}>
              <div style={{ height: "8px", background: C.s3, borderRadius: "4px", overflow: "hidden" }}>
                <div style={{ height: "100%", width: `${progress.total ? Math.round(progress.sent / progress.total * 100) : 0}%`,
                              background: C.green, transition: "width 0.15s ease" }} />
              </div>
              <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginTop: "5px" }}>
                sending {(progress.sent / 1048576).toFixed(1)} / {(progress.total / 1048576).toFixed(1)} MB
              </div>
            </div>
          )}
        </>
      ) : (
        <div style={{ marginBottom: "12px" }}>
          <Field label="GIT URL">
            <TextIn value={gitUrl} onChange={setGitUrl} placeholder="https://github.com/owner/repo.git" />
          </Field>
          <div style={{ height: "10px" }} />
          <Field label="BRANCH (optional)">
            <TextIn value={branch} onChange={setBranch} placeholder="default branch" />
          </Field>
        </div>
      )}

      {result && (
        <>
          <Ok msg={result.message} />
          {result.provisioned && (
            <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginTop: "8px" }}>
              detected deploy mode: <span style={{ color: C.green }}>{result.deploy_mode}</span>
            </div>
          )}
        </>
      )}
      {error && <Err msg={error} />}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "12px" }}>
        <Btn v="ghost" onClick={onClose}>close</Btn>
        <Btn v="primary" onClick={submit} busy={busy} disabled={mode === "files" && !entries.length}>deploy</Btn>
      </div>
    </Modal>
  );
};

// ── Status modal ──────────────────────────────────────────────────────────────
const StatusModal = ({ data, project, onClose }) => (
  <Modal onClose={onClose} width={540}>
    <ModalHeader title={`STATUS — ${project}`} color={C.muted} onClose={onClose} />
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "10px", marginBottom: "14px" }}>
      {[["operation", data.operation || "—"], ["status", data.status], ["exit code", data.exit_code ?? "—"]].map(([k, v]) => (
        <div key={k} style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "8px 12px" }}>
          <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.1em", marginBottom: "4px" }}>{k.toUpperCase()}</div>
          <div style={{ color: k === "status" ? (SC[v] || C.txt) : C.txt, fontFamily: C.ff, fontSize: "12px" }}>{String(v)}</div>
        </div>
      ))}
    </div>
    <Field label="LOGS">
      <div style={{ background: C.s3, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "10px 12px", fontFamily: C.mono, fontSize: "11px", color: C.txt, lineHeight: "1.65", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: "220px", overflow: "auto" }}>
        {data.logs || <span style={{ color: C.dim }}>(no logs)</span>}
      </div>
    </Field>
    <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "12px" }}>
      <Btn v="ghost" onClick={onClose}>close</Btn>
    </div>
  </Modal>
);

// ── SSL modal ─────────────────────────────────────────────────────────────────
const SslModal = ({ data, project, onClose }) => (
  <Modal onClose={onClose}>
    <ModalHeader title={`SSL — ${project}`} color={C.green} onClose={onClose} />
    <div style={{ display: "flex", alignItems: "center", gap: "10px", marginBottom: "12px" }}>
      <span style={{ color: data.ssl_enabled ? C.green : C.amber, fontFamily: C.ff, fontSize: "13px" }}>
        {data.ssl_enabled ? "✓ enabled" : "⚠ not yet enabled"}
      </span>
    </div>
    {data.message && (
      <Field label="CERTBOT OUTPUT">
        <div style={{ background: C.s3, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "10px 12px", fontFamily: C.mono, fontSize: "11px", color: C.txt, lineHeight: "1.6", whiteSpace: "pre-wrap", maxHeight: "200px", overflow: "auto" }}>
          {data.message}
        </div>
      </Field>
    )}
    <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "12px" }}>
      <Btn v="ghost" onClick={onClose}>close</Btn>
    </div>
  </Modal>
);

// ── Domain modal (set / clear a component's custom domain) ──────────────────────
const DomainModal = ({ token, project, service = null, info, onClose, onDone }) => {
  const [domain, setDomain] = useState(info.custom_domain || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const title = service ? `${project} → ${service}` : project;

  const path = service
    ? `/projects/${project}/services/${service}/domain`
    : `/projects/${project}/domain`;

  const submit = async (clear) => {
    const value = clear ? null : domain.trim();
    if (!clear && (!value || !value.includes("."))) return setError("Enter a valid domain (e.g. app.acme.com)");
    setError(""); setBusy(true);
    try {
      const data = await mkApi(token).post(path, { custom_domain: value });
      setResult(data);
      onDone && onDone();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  // Pull the affected component out of the returned project to report its new state.
  const after = result && (service ? (result.services || []).find(s => s.name === service) : result.container);

  return (
    <Modal onClose={onClose}>
      <ModalHeader title={`CUSTOM DOMAIN — ${title}`} color={C.blue} onClose={onClose} />
      <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginBottom: "12px", lineHeight: "1.6" }}>
        currently served at <span style={{ color: C.blue }}>{info.subdomain || "—"}</span>
        {info.custom_domain && <span style={{ color: C.dim }}> (custom)</span>}
      </div>
      <Field label="CUSTOM DOMAIN (FQDN)" style={{ marginBottom: "8px" }}>
        <TextIn value={domain} onChange={setDomain} placeholder="app.acme.com" />
      </Field>
      <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.6", marginBottom: "12px" }}>
        Point the domain's A record at this server first. SSL is issued on save; if DNS hasn't
        propagated yet the component stays HTTP-only and you can save again later to retry.
      </div>

      {after && (
        <div style={{ marginBottom: "12px" }}>
          <Ok msg={`now serving ${after.subdomain}`} />
          <div style={{ color: after.ssl_enabled ? C.green : C.amber, fontFamily: C.ff, fontSize: "11px", marginTop: "8px" }}>
            {after.ssl_enabled ? "✓ SSL enabled" : "⚠ SSL not yet enabled (retry once DNS points here)"}
          </div>
        </div>
      )}
      {error && <Err msg={error} />}

      <div style={{ display: "flex", justifyContent: "space-between", gap: "8px", marginTop: "12px" }}>
        <div>
          {info.custom_domain && <Btn v="amber" onClick={() => submit(true)} busy={busy}>clear</Btn>}
        </div>
        <div style={{ display: "flex", gap: "8px" }}>
          <Btn v="ghost" onClick={onClose} disabled={busy}>close</Btn>
          <Btn v="primary" onClick={() => submit(false)} busy={busy}>set domain</Btn>
        </div>
      </div>
    </Modal>
  );
};

// ── Versions modal (blue/green backups) ─────────────────────────────────────────
// Lists a project's deployed versions (dockerfile: active / inactive / archived; compose:
// active / archived — a compose switchover downs the previous stack), lets the user set the
// backup limit (how many archived versions to keep), and roll back to an earlier one. A
// rollback streams over WS /projects/{name}/deploy via the shared InstallPane (handed up
// through onStream), exactly like a deploy.
const VC = { active: C.green, inactive: C.amber, archived: C.muted };
const VI = { active: "▶", inactive: "■", archived: "○" };
const VersionBadge = ({ status }) => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: "5px", fontFamily: C.ff, fontSize: "11px", color: VC[status] || C.muted }}>
    <span style={{ fontSize: "7px" }}>●</span>{VI[status] || "•"} {status}
  </span>
);

const VersionsModal = ({ token, project, onClose, onStream, onRefresh }) => {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [limit, setLimit] = useState("");
  const [busy, setBusy] = useState({});   // { limit, "rollback:N" }
  const client = mkApi(token);

  const load = async () => {
    try { const d = await client.get(`/projects/${project}/versions`); setData(d); setLimit(String(d.backup_limit)); }
    catch (e) { setError(e.message); }
  };
  useEffect(() => { load(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const saveLimit = async () => {
    const n = parseInt(limit, 10);
    if (!Number.isInteger(n) || n < 1) return setError("limit must be a whole number ≥ 1");
    setError(""); setBusy(b => ({ ...b, limit: true }));
    try {
      const d = await client.put(`/projects/${project}/backup-limit`, { limit: n });
      setData(d); setLimit(String(d.backup_limit)); onRefresh && onRefresh();
    } catch (e) { setError(e.message); }
    finally { setBusy(b => ({ ...b, limit: false })); }
  };

  const rollback = async (version) => {
    setError(""); setBusy(b => ({ ...b, [`rollback:${version}`]: true }));
    try {
      const d = await client.post(`/projects/${project}/rollback`, { version });
      onStream({ project, wsPath: d.ws_path });   // stream the rollback log in the InstallPane
      onClose();
    } catch (e) { setError(e.message); setBusy(b => ({ ...b, [`rollback:${version}`]: false })); }
  };

  const counts = data?.counts || {};
  const TH = ({ children, right, center }) => (
    <th style={{ padding: "4px 10px", textAlign: right ? "right" : center ? "center" : "left", color: C.dim, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.1em", fontWeight: 400, whiteSpace: "nowrap" }}>{children}</th>
  );
  const td = { padding: "7px 10px", fontFamily: C.ff, fontSize: "11px", color: C.txt };

  return (
    <Modal onClose={onClose} width={660}>
      <ModalHeader title={`VERSIONS — ${project}`} color={C.brand} onClose={onClose} />

      {/* Backup limit control + counts */}
      <div style={{ display: "flex", alignItems: "flex-end", justifyContent: "space-between", gap: "12px", marginBottom: "14px", flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "flex-end", gap: "8px" }}>
          <Field label="BACKUP LIMIT (ARCHIVED KEPT)">
            <TextIn value={limit} onChange={setLimit} type="number" style={{ width: "90px" }} />
          </Field>
          <Btn v="primary" onClick={saveLimit} busy={busy.limit}>save</Btn>
        </div>
        <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.7" }}>
          <span style={{ color: C.green }}>{counts.active || 0} active</span>
          {" · "}<span style={{ color: C.amber }}>{counts.inactive || 0} inactive</span>
          {" · "}<span style={{ color: C.muted }}>{counts.archived || 0} archived</span>
        </div>
      </div>

      {error && <div style={{ marginBottom: "10px" }}><Err msg={error} /></div>}

      {!data ? (
        <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "11px", padding: "16px 0" }}>loading versions…</div>
      ) : (data.versions || []).length === 0 ? (
        <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "11px", padding: "16px 0" }}>
          no versions yet — deploy this project first.
        </div>
      ) : (
        <div style={{ overflowX: "auto", border: `1px solid ${C.bd}`, borderRadius: "8px" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${C.bd}`, background: C.s2 }}>
                <TH right>VER</TH><TH>STATE</TH><TH right>PORT</TH><TH>RUNTIME</TH><TH>CREATED</TH><TH right>ACTION</TH>
              </tr>
            </thead>
            <tbody>
              {data.versions.map(v => (
                <tr key={v.version} style={{ borderBottom: `1px solid ${C.bd}` }}>
                  <td style={{ ...td, textAlign: "right", color: C.brand, fontWeight: 600 }}>v{v.version}</td>
                  <td style={td}><VersionBadge status={v.status} /></td>
                  <td style={{ ...td, textAlign: "right" }}>{v.local_port ?? "—"}</td>
                  <td style={td}><Tag status={v.container_status} /></td>
                  <td style={{ ...td, color: C.dim }}>{v.created_at ? new Date(v.created_at).toLocaleString() : "—"}</td>
                  <td style={{ ...td, textAlign: "right" }}>
                    {v.status === "active"
                      ? <span style={{ color: C.dim, fontSize: "10px" }}>current</span>
                      : <Btn sm v="blue" onClick={() => rollback(v.version)} busy={busy[`rollback:${v.version}`]} title={`Roll back to v${v.version}`}>rollback</Btn>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "14px" }}>
        <Btn v="ghost" onClick={onClose}>close</Btn>
      </div>
    </Modal>
  );
};

// ── Row cells (shared layout) ───────────────────────────────────────────────────
const Cells = ({ label, info }) => (
  <>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "11px", color: C.blue, minWidth: "90px" }}>{label}</td>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "10px" }}>
      {info.subdomain
        ? <a href={`https://${info.subdomain}`} target="_blank" rel="noreferrer" style={{ color: C.muted, textDecoration: "none" }}>{info.subdomain}</a>
        : <span style={{ color: C.dim }}>—</span>}
      {info.custom_domain && (
        <span style={{ color: C.blue, background: C.blueFill, border: `1px solid ${C.blueBd}`, fontSize: "8px", letterSpacing: "0.08em", padding: "1px 5px", borderRadius: "8px", marginLeft: "7px" }}>custom</span>
      )}
      {info.exposed === false && (
        <span style={{ color: C.dim, background: C.s3, border: `1px solid ${C.bd}`, fontSize: "8px", letterSpacing: "0.08em", padding: "1px 5px", borderRadius: "8px", marginLeft: "7px" }} title="No published TCP port — runs outside nginx (host networking, UDP-only, or internal-only)">unproxied</span>
      )}
    </td>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "11px", color: C.txt, textAlign: "right", minWidth: "55px" }}>{info.local_port ?? "—"}</td>
    <td style={{ padding: "7px 10px", textAlign: "center", minWidth: "38px" }}>
      {info.exposed === false
        ? <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>—</span>
        : <span style={{ color: info.ssl_enabled ? C.green : C.dim, fontFamily: C.ff, fontSize: "11px" }}>{info.ssl_enabled ? "✓" : "✗"}</span>}
    </td>
    <td style={{ padding: "7px 10px", minWidth: "130px" }}><Tag status={info.container_status} /></td>
  </>
);

// ── Container row (dockerfile mode: one container per project, project-level ops) ──
const ContainerRow = ({ project, info, token, onOperation, onRefresh, onStream }) => {
  const [busy, setBusy] = useState({});
  const [modal, setModal] = useState(null); // null | {type, data?}

  const act = async (action, body) => {
    setBusy(b => ({ ...b, [action]: true }));
    try {
      const data = action === "status"
        ? await mkApi(token).get(`/projects/${project}/status`)
        : await mkApi(token).post(`/projects/${project}/${action}`, body);
      if (action === "status") setModal({ type: "status", data });
      else if (action === "ssl") setModal({ type: "ssl", data });
      else onOperation({ project, operation: data.operation || action, status: data.status, logs: data.logs || data.message || "" });
    } catch (e) {
      onOperation({ project, operation: action, status: "error", logs: e.message });
    } finally {
      setBusy(b => ({ ...b, [action]: false }));
    }
  };

  const isRunning = info.container_status === "running";

  return (
    <>
      <tr style={{ borderBottom: `1px solid ${C.bd}` }}>
        <Cells label={project} info={info} />
        <td style={{ padding: "6px 8px" }}>
          <div style={{ display: "flex", gap: "4px", flexWrap: "wrap" }}>
            <Btn sm v="danger" onClick={() => act("stop")} busy={busy.stop} disabled={!isRunning} title="Stop container">stop</Btn>
            <Btn sm v="amber" onClick={() => setModal({ type: "exec" })} disabled={!isRunning} title="Exec command in container">exec</Btn>
            <Btn sm onClick={() => act("ssl")} busy={busy.ssl} title="Issue/renew SSL cert">ssl</Btn>
            <Btn sm v="blue" onClick={() => setModal({ type: "domain" })} title="Set or clear a custom domain">domain</Btn>
            <Btn sm v="purple" onClick={() => setModal({ type: "versions" })} title="View versions, roll back, set backup limit">versions</Btn>
            <Btn sm v="ghost" onClick={() => act("status")} busy={busy.status} title="View job status & logs">status</Btn>
          </div>
        </td>
      </tr>

      {modal?.type === "exec"       && <ExecTerminal token={token} project={project} label={project} onClose={() => setModal(null)} />}
      {modal?.type === "status"     && <StatusModal data={modal.data} project={project} onClose={() => setModal(null)} />}
      {modal?.type === "ssl"        && <SslModal data={modal.data} project={project} onClose={() => setModal(null)} />}
      {modal?.type === "domain"     && <DomainModal token={token} project={project} info={info} onClose={() => setModal(null)} onDone={onRefresh} />}
      {modal?.type === "versions"   && <VersionsModal token={token} project={project} onClose={() => setModal(null)} onStream={onStream} onRefresh={onRefresh} />}
    </>
  );
};

// ── Service row (compose mode: per-service exec + custom domain; stack lifecycle is on the card) ──
const ServiceRow = ({ project, info, token, onOperation, onRefresh }) => {
  const [modal, setModal] = useState(null);
  const isRunning = info.container_status === "running";

  return (
    <tr style={{ borderBottom: `1px solid ${C.bd}` }}>
      <Cells label={info.name} info={info} />
      <td style={{ padding: "6px 8px" }}>
        <div style={{ display: "flex", gap: "4px", flexWrap: "wrap" }}>
          <Btn sm v="amber" onClick={() => setModal("exec")} disabled={!isRunning} title="Exec command in this service's container">exec</Btn>
          {info.exposed !== false && (
            <Btn sm v="blue" onClick={() => setModal("domain")} title="Set or clear a custom domain">domain</Btn>
          )}
        </div>
        {modal === "exec"   && <ExecTerminal token={token} project={project} service={info.name} label={`${project} → ${info.name}`} onClose={() => setModal(null)} />}
        {modal === "domain" && <DomainModal token={token} project={project} service={info.name} info={info} onClose={() => setModal(null)} onDone={onRefresh} />}
      </td>
    </tr>
  );
};

// ── Project card ──────────────────────────────────────────────────────────────
const ProjectCard = ({ project, token, onOperation, onRemoved, onRefresh, onDeploy, onStream }) => {
  const [confirm, setConfirm] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [busy, setBusy] = useState({});
  const [uploadModal, setUploadModal] = useState(false);
  const [versionsModal, setVersionsModal] = useState(false);
  const isCompose = project.deploy_mode === "compose";
  const isPending = project.deploy_mode !== "compose" && project.deploy_mode !== "dockerfile";
  const isPlugin = project.type === "plugin";

  const remove = async () => {
    setRemoving(true);
    try {
      await mkApi(token).del(`/projects/${project.name}`);
      onRemoved(project.name);
    } catch (e) { alert(`Remove failed: ${e.message}`); }
    finally { setRemoving(false); setConfirm(false); }
  };

  // Project-level docker compose down — streamed via the bottom LogPane. (build + up are
  // launched automatically by the upload/deploy flow.)
  const composeAct = async (action) => {
    setBusy(b => ({ ...b, [action]: true }));
    try {
      const data = await mkApi(token).post(`/projects/${project.name}/compose/${action}`);
      onOperation({ project: project.name, kind: "compose", operation: data.operation || `compose_${action}`, status: data.status, logs: data.logs || data.message || "" });
    } catch (e) {
      onOperation({ project: project.name, kind: "compose", operation: `compose_${action}`, status: "error", logs: e.message });
    } finally {
      setBusy(b => ({ ...b, [action]: false }));
    }
  };

  const TH = ({ children, right, center }) => (
    <th style={{ padding: "4px 10px", textAlign: right ? "right" : center ? "center" : "left", color: C.dim, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.1em", fontWeight: 400, whiteSpace: "nowrap" }}>
      {children}
    </th>
  );

  return (
    <>
      <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", marginBottom: "10px", overflow: "hidden", boxShadow: C.shadow }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 14px", background: C.s2, borderBottom: `1px solid ${C.bd}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span style={{ color: C.brand, fontFamily: C.ff, fontSize: "14px", fontWeight: 600 }}>{project.name}</span>
            {isCompose && (
              <span style={{ color: C.brand, background: C.brandFill, border: `1px solid ${C.brandBd}`, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.08em", padding: "1px 7px", borderRadius: "8px" }}>compose</span>
            )}
            {isPending && (
              <span style={{ color: C.amber, background: C.amberFill, border: `1px solid ${C.amberBd}`, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.08em", padding: "1px 7px", borderRadius: "8px" }}>pending</span>
            )}
            {isPlugin && (
              <span style={{ color: C.muted, background: C.s3, border: `1px solid ${C.bd}`, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.08em", padding: "1px 7px", borderRadius: "8px" }}>plugin</span>
            )}
            <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px" }}>
              {isCompose ? `${project.services?.length ?? 0} service${project.services?.length !== 1 ? "s" : ""}` : isPending ? "awaiting upload" : "container"}
            </span>
            {project.created_at && (
              <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px" }}>created {new Date(project.created_at).toLocaleDateString()}</span>
            )}
          </div>
          <div style={{ display: "flex", gap: "6px" }}>
            <Btn sm v="green" onClick={() => setUploadModal(true)} title="Upload files or a folder — auto-detects Dockerfile / docker-compose.yml, provisions, and deploys">{isPending ? "upload" : "deploy"}</Btn>
            {isCompose && (
              <Btn sm v="purple" onClick={() => setVersionsModal(true)} title="View versions, roll back, set backup limit">versions</Btn>
            )}
            {isCompose && (
              <Btn sm v="danger" onClick={() => composeAct("down")} busy={busy.down} title="docker compose down">down</Btn>
            )}
            <Btn v="danger" sm onClick={() => setConfirm(true)} busy={removing}>remove</Btn>
          </div>
        </div>

        {isPending ? (
          <div style={{ padding: "18px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
            awaiting upload — use <span style={{ color: C.brand }}>upload</span> to add a Dockerfile or docker-compose.yml
          </div>
        ) : isCompose && (project.services || []).length === 0 ? (
          <div style={{ padding: "18px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
            no services recorded — <span style={{ color: C.brand }}>deploy</span> again to register this project's containers
          </div>
        ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${C.bd}` }}>
                <TH>{isCompose ? "SERVICE" : "CONTAINER"}</TH><TH>SUBDOMAIN</TH><TH right>PORT</TH><TH center>SSL</TH><TH>STATUS</TH><TH>ACTIONS</TH>
              </tr>
            </thead>
            <tbody>
              {isCompose
                ? (project.services || []).map(s => <ServiceRow key={s.name} project={project.name} info={s} token={token} onOperation={onOperation} onRefresh={onRefresh} />)
                : (project.container
                    ? <ContainerRow project={project.name} info={project.container} token={token} onOperation={onOperation} onRefresh={onRefresh} onStream={onStream} />
                    : null)}
            </tbody>
          </table>
        </div>
        )}
      </div>

      {uploadModal && <UploadModal token={token} project={project.name} onClose={() => setUploadModal(false)} onUploaded={() => onRefresh && onRefresh()} onDeploy={onDeploy} />}
      {versionsModal && <VersionsModal token={token} project={project.name} onClose={() => setVersionsModal(false)} onStream={onStream} onRefresh={onRefresh} />}
      {confirm && <ConfirmModal message={`Delete "${project.name}"? This stops containers, removes images and nginx config.`} onConfirm={remove} onCancel={() => setConfirm(false)} loading={removing} />}
    </>
  );
};

// ── Deploy project form (files/folder or git URL) ─────────────────────────────
// One entry point to create + deploy a project. "Files" mode chunk-uploads the selection
// (chunkedDeploy); "Git" mode POSTs /git/add. Either way the server auto-creates the
// project (no separate create step), auto-detects a Dockerfile/docker-compose.yml (compose
// wins), wires nginx + SSL, and builds + runs it — the provisioned response streams over
// the InstallPane (onDeployed → handleInstalled). Mirrors `fhcli deploy NAME PATH-OR-URL`.
const DeployForm = ({ token, onDeployed, onSynced, onCancel }) => {
  const [name, setName] = useState("");
  const [mode, setMode] = useState("files");   // "files" | "git"
  const [entries, setEntries] = useState([]);   // [{ file, rel }]
  const [srcKind, setSrcKind] = useState("files");   // "files" | "folder" — which picker fired
  const [rootName, setRootName] = useState("");      // top folder name of a folder pick
  const [gitUrl, setGitUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(null);
  const [result, setResult] = useState(null);
  const recentGit = useMemo(() => getRecentGitUrls(), []);
  const dirRef = useRef();

  // React drops the non-standard directory attributes, so set them on mount / mode switch.
  useEffect(() => {
    if (dirRef.current) {
      dirRef.current.setAttribute("webkitdirectory", "");
      dirRef.current.setAttribute("directory", "");
    }
  }, [mode]);

  const pickFiles = (fileList) => { setError(""); setResult(null); setSrcKind("files"); setRootName(""); setEntries([...fileList].map(f => ({ file: f, rel: f.name }))); };
  const pickFolder = (fileList) => { setError(""); setResult(null); setSrcKind("folder"); setRootName((fileList[0]?.webkitRelativePath || "").split("/")[0] || ""); setEntries([...fileList].map(f => ({ file: f, rel: stripRoot(f.webkitRelativePath || f.name) }))); };

  // Fill the git fields (and the name, if still empty) from a remembered recent deploy.
  const useRecent = (r) => { setGitUrl(r.gitUrl); setBranch(r.branch || ""); if (!name.trim() && r.name) setName(r.name); setError(""); };

  const submit = async () => {
    if (!name.trim()) return setError("Project name is required");
    setError(""); setResult(null); setProgress(null); setBusy(true);
    try {
      const api = mkApi(token);
      if (mode === "git") {
        if (!gitUrl.trim()) { setBusy(false); return setError("Git URL is required"); }
        const data = await api.post("/git/add", { name: name.trim(), git_url: gitUrl.trim(), branch: branch.trim() || null });
        saveProjectDeploy(name.trim(), { srcKind: "git", gitUrl: gitUrl.trim(), branch: branch.trim(), label: gitUrl.trim() });
        onDeployed(data);   // git always provisions (or 400s) → stream the build
      } else {
        if (!entries.length) { setBusy(false); return setError("Select a file or folder first"); }
        const data = await chunkedDeploy(api, name.trim(), entries, setProgress);
        saveProjectDeploy(name.trim(), { srcKind, gitUrl: "", branch: "", label: describeSelection(srcKind, entries, rootName) });
        if (data.provisioned && data.ws_path) { onDeployed(data); return; }
        setResult(data); onSynced && onSynced();   // no manifest — plain file sync, nothing built
      }
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const slug = name.trim().toLowerCase();
  const dropStyle = { flex: 1, display: "block", border: `2px dashed ${C.bdB}`, borderRadius: "8px", padding: "14px", textAlign: "center", background: C.s1, cursor: "pointer" };
  const tab = (m, label) => (
    <button onClick={() => { setMode(m); setError(""); setResult(null); }} style={{
      flex: 1, padding: "8px", cursor: "pointer", fontFamily: C.ff, fontSize: "11px", fontWeight: 600,
      background: mode === m ? C.brandFill : C.s3, color: mode === m ? C.brand : C.muted,
      border: `1px solid ${mode === m ? C.brandBd : C.bd}`, borderRadius: "8px",
    }}>{label}</button>
  );

  return (
    <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "18px", marginBottom: "12px", boxShadow: C.shadow }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "14px" }}>
        <span style={{ color: C.brand, fontFamily: C.ff, fontSize: "11px", letterSpacing: "0.1em", fontWeight: 600 }}>DEPLOY PROJECT</span>
        <Btn v="ghost" sm onClick={onCancel}>✕</Btn>
      </div>

      <div style={{ display: "grid", gap: "12px" }}>
        <Field label="PROJECT NAME (used as the subdomain)">
          <TextIn value={name} onChange={setName} placeholder="myapp" />
          <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px", marginTop: "5px" }}>
            → served at <span style={{ color: C.blue }}>https://{slug || "myapp"}.{DOMAIN}</span>
            <span style={{ color: C.dim }}> · created automatically if new</span>
          </div>
        </Field>

        <div style={{ display: "flex", gap: "8px" }}>
          {tab("files", "files / folder")}
          {tab("git", "git URL")}
        </div>

        {mode === "files" ? (
          <>
            <div style={{ display: "flex", gap: "8px" }}>
              <label htmlFor="dep-file" style={dropStyle}>
                <input id="dep-file" type="file" multiple onChange={e => pickFiles(e.target.files)} style={{ display: "none" }} />
                <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px" }}>select file(s)</div>
              </label>
              <label htmlFor="dep-folder" style={dropStyle}>
                <input id="dep-folder" ref={dirRef} type="file" multiple onChange={e => pickFolder(e.target.files)} style={{ display: "none" }} />
                <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px" }}>select a folder</div>
              </label>
            </div>
            {entries.length > 0 && !progress && (
              <div style={{ color: C.green, fontFamily: C.ff, fontSize: "11px" }}>✓ {entries.length} file(s) selected</div>
            )}
          </>
        ) : (
          <>
            <Field label="GIT URL">
              <TextIn value={gitUrl} onChange={setGitUrl} placeholder="https://github.com/owner/repo.git" />
            </Field>
            {recentGit.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", alignItems: "center" }}>
                <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px" }}>recent:</span>
                {recentGit.map(r => (
                  <button key={r.gitUrl} onClick={() => useRecent(r)} title={r.branch ? `${r.gitUrl} (${r.branch})` : r.gitUrl} style={{
                    cursor: "pointer", fontFamily: C.ff, fontSize: "10px", color: C.blue,
                    background: C.s3, border: `1px solid ${C.bd}`, borderRadius: "6px", padding: "3px 8px",
                    maxWidth: "220px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  }}>{r.gitUrl.replace(/^https?:\/\//, "").replace(/\.git$/, "")}</button>
                ))}
              </div>
            )}
            <Field label="BRANCH (optional)">
              <TextIn value={branch} onChange={setBranch} placeholder="default branch" />
            </Field>
          </>
        )}

        <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.6", background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "10px 12px" }}>
          The server scans the {mode === "git" ? "cloned repo" : "uploaded"} root for a
          <span style={{ color: C.txt }}> Dockerfile</span> or <span style={{ color: C.txt }}>docker-compose.yml</span> (compose
          wins), wires up nginx + SSL, then builds and runs it — streaming the build log below. A
          Dockerfile must <span style={{ color: C.txt }}>EXPOSE</span> its port.
          {mode === "files" && " Files with no manifest are just synced (nothing is built)."}
        </div>

        {progress && (
          <div>
            <div style={{ height: "8px", background: C.s3, borderRadius: "4px", overflow: "hidden" }}>
              <div style={{ height: "100%", width: `${progress.total ? Math.round(progress.sent / progress.total * 100) : 0}%`, background: C.green, transition: "width 0.15s ease" }} />
            </div>
            <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginTop: "5px" }}>
              sending {(progress.sent / 1048576).toFixed(1)} / {(progress.total / 1048576).toFixed(1)} MB
            </div>
          </div>
        )}

        {result && <Ok msg={result.message} />}
        <Err msg={error} />

        <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
          <Btn v="ghost" onClick={onCancel} disabled={busy}>cancel</Btn>
          <Btn v="primary" onClick={submit} busy={busy} disabled={mode === "files" && !entries.length}>deploy</Btn>
        </div>
      </div>
    </div>
  );
};

// ── Plugin panel (master-detail: name list ←25% │ 75%→ selected plugin detail) ──
const PluginPanel = ({ token, onInstalled, onCancel }) => {
  const [plugins, setPlugins] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);   // plugin name
  const [installing, setInstalling] = useState(null);

  useEffect(() => {
    mkApi(token).get("/plugins")
      // `system` plugins create hidden projects and are not offered in the UI
      .then(ps => {
        const list = ps.filter(p => p.type !== "system");
        setPlugins(list);
        if (list.length) setSelected(list[0].name);
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [token]);

  const chip = (text, accent) => (
    <span style={{ background: accent ? C.brandFill : C.s3, color: accent ? C.brand : C.muted, border: `1px solid ${accent ? C.brandBd : C.bd}`, borderRadius: "8px", padding: "2px 9px", fontFamily: C.ff, fontSize: "10px" }}>{text}</span>
  );

  const active = plugins.find(p => p.name === selected) || null;

  return (
    <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", marginBottom: "12px", boxShadow: C.shadow, overflow: "hidden" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", borderBottom: `1px solid ${C.bd}`, background: C.s2 }}>
        <span style={{ color: C.brand, fontFamily: C.ff, fontSize: "11px", letterSpacing: "0.1em", fontWeight: 600 }}>AVAILABLE PLUGINS</span>
        <Btn v="ghost" sm onClick={onCancel}>✕</Btn>
      </div>

      {loading ? (
        <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px", padding: "20px" }}>loading plugins…</div>
      ) : error ? (
        <div style={{ padding: "16px" }}><Err msg={error} /></div>
      ) : plugins.length === 0 ? (
        <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "11px", padding: "20px" }}>no plugins available</div>
      ) : (
        <div style={{ display: "flex", minHeight: "360px" }}>
          {/* Left: plugin name list (~25%) */}
          <div style={{ width: "25%", minWidth: "170px", maxWidth: "260px", flexShrink: 0, borderRight: `1px solid ${C.bd}`, background: C.s2, padding: "8px" }}>
            {plugins.map(p => {
              const sel = p.name === selected;
              return (
                <button key={p.name} onClick={() => setSelected(p.name)} style={{
                  display: "block", width: "100%", textAlign: "left",
                  background: sel ? C.brandFill : "transparent",
                  color: sel ? C.brand : C.txt,
                  border: `1px solid ${sel ? C.brandBd : "transparent"}`,
                  borderRadius: "8px", padding: "9px 11px", marginBottom: "2px", cursor: "pointer",
                  fontFamily: C.ff, fontSize: "13px", fontWeight: sel ? 600 : 500,
                }}>
                  {p.name}
                  <div style={{ color: sel ? C.brand : C.dim, fontSize: "10px", fontWeight: 400, marginTop: "2px", opacity: sel ? 0.8 : 1 }}>
                    {p.deploy_mode === "compose" ? "compose" : "dockerfile"}
                  </div>
                </button>
              );
            })}
          </div>

          {/* Right: selected plugin detail (~75%) */}
          <div style={{ flex: 1, minWidth: 0, padding: "20px 22px", position: "relative" }}>
            {active && (
              <>
                <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "16px", marginBottom: "16px" }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ color: C.txt, fontFamily: C.ff, fontSize: "20px", fontWeight: 700, marginBottom: "8px" }}>{active.name}</div>
                    <div style={{ display: "flex", alignItems: "center", gap: "7px", flexWrap: "wrap" }}>
                      {chip(active.deploy_mode === "compose" ? "compose" : `port ${active.container_port}`)}
                      {active.has_install && chip("install.sh", true)}
                      {active.interactive && chip("interactive", true)}
                    </div>
                  </div>
                  <Btn v="green" onClick={() => setInstalling(active)} style={{ flexShrink: 0 }}>install</Btn>
                </div>
                <div style={{ maxHeight: "65vh", overflowY: "auto" }}>
                  <Markdown text={active.about || active.description} />
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {installing && (
        <InstallPluginModal
          token={token}
          plugin={installing}
          onClose={() => setInstalling(null)}
          onInstalled={onInstalled}
        />
      )}
    </div>
  );
};

// ── Login screen ──────────────────────────────────────────────────────────────
const LoginScreen = ({ onAuth }) => {
  const [token, setToken] = useState(() => localStorage.getItem("freeholdy_token") || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    if (!token.trim()) return setError("Enter a token");
    setBusy(true); setError("");
    try {
      await mkApi(token.trim()).get("/health");
      onAuth(token.trim());
    } catch (e) { setError(`Authentication failed: ${e.message}`); }
    finally { setBusy(false); }
  };

  return (
    <div style={{ minHeight: "100vh", background: C.bg, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ width: 380, background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "36px", boxShadow: "0 12px 48px rgba(0,0,0,.5)" }}>
        <div style={{ marginBottom: "30px" }}>
          <div style={{ color: C.brand, fontFamily: C.ff, fontSize: "22px", fontWeight: 700, marginBottom: "6px" }}>freeholdy</div>
          <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", letterSpacing: "0.12em" }}>CLOUDOPEN.SPACE CONTROL PANEL</div>
        </div>

        <Field label="API TOKEN" style={{ marginBottom: "10px" }}>
          <TextIn type="password" value={token} onChange={setToken} placeholder="paste your token…" />
        </Field>

        <Err msg={error} />

        <Btn v="primary" onClick={submit} busy={busy} style={{ width: "100%", marginTop: error ? "10px" : "14px", padding: "7px 12px" }}>
          connect →
        </Btn>

        <div style={{ marginTop: "22px", color: C.muted, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.7", borderTop: `1px solid ${C.bd}`, paddingTop: "14px" }}>
          generate token:
          <div style={{ marginTop: "6px", color: C.txt, fontFamily: C.mono, fontSize: "10px", background: C.s3, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "7px 9px", wordBreak: "break-all" }}>
            python scripts/generate_token.py generate --name web_ui
          </div>
        </div>
      </div>
    </div>
  );
};

// ── Nav rail item (Grafana-style left rail) ─────────────────────────────────────
const NavItem = ({ icon, label, active, onClick, title }) => (
  <button onClick={onClick} title={title} style={{
    display: "flex", alignItems: "center", gap: "10px", width: "100%", textAlign: "left",
    background: active ? C.brandFill : "transparent",
    color: active ? C.brand : C.txt,
    border: "1px solid transparent",
    borderLeft: `3px solid ${active ? C.brand : "transparent"}`,
    borderRadius: "6px", padding: "9px 12px", marginBottom: "2px", cursor: "pointer",
    fontFamily: C.ff, fontSize: "13px", fontWeight: active ? 600 : 500,
  }}>
    <span style={{ fontSize: "14px", width: "18px", textAlign: "center" }}>{icon}</span>
    <span>{label}</span>
  </button>
);

// ── Dashboard ─────────────────────────────────────────────────────────────────
const Dashboard = ({ token, onLogout }) => {
  const [projects, setProjects] = useState([]);
  const [health, setHealth] = useState(null);
  const [version, setVersion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showDeploy, setShowDeploy] = useState(false);
  const [showPlugins, setShowPlugins] = useState(false);
  const [showGitKey, setShowGitKey] = useState(false);
  const [railOpen, setRailOpen] = useState(false);   // mobile: off-canvas nav rail
  const [activeLog, setActiveLog] = useState(null);
  const [interactiveLog, setInteractiveLog] = useState(null);  // { project, wsPath, kind }
  const pollRef = useRef(null);

  const client = useMemo(() => mkApi(token), [token]);

  const fetchProjects = useCallback(async () => {
    // `system` projects (e.g. infrastructure) are intentionally hidden from the UI
    try { setProjects((await client.get("/projects")).filter(p => p.type !== "system")); }
    catch (e) { console.error("fetch projects:", e); }
    finally { setLoading(false); }
  }, [client]);

  const checkHealth = useCallback(async () => {
    try { const d = await client.get("/health"); setHealth(d.status); }
    catch { setHealth("unreachable"); }
  }, [client]);

  const fetchVersion = useCallback(async () => {
    try { setVersion(await client.get("/version")); } catch {}
  }, [client]);

  useEffect(() => { checkHealth(); fetchProjects(); fetchVersion(); }, []);

  // Stop/ssl poll the project-level status endpoint; compose down has its own path.
  // (Deploys, exec, and installs stream over WebSockets, not polled.)
  const statusPath = (log) => log.kind === "compose"
    ? `/projects/${log.project}/compose/status`
    : `/projects/${log.project}/status`;

  const startPolling = useCallback((log) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const d = await client.get(statusPath(log));
        setActiveLog(l => l ? { ...l, status: d.status, logs: d.logs, operation: d.operation || l.operation } : null);
        if (d.status !== "running") {
          clearInterval(pollRef.current); pollRef.current = null;
          fetchProjects();
        }
      } catch {}
    }, POLL_MS);
  }, [client, fetchProjects]);

  const handleOperation = useCallback((log) => {
    setActiveLog(log);
    if (log.status === "running") startPolling(log);
  }, [startPolling]);

  const handleInstalled = useCallback((data) => {
    setProjects(p => [data.project, ...p.filter(x => x.name !== data.project.name)]);
    // Every install streams over its WebSocket now (interactive drives install.sh first,
    // non-interactive just watches the build). Hand off to the InstallPane.
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    setActiveLog(null);
    setInteractiveLog({
      project: data.project.name,
      wsPath: data.ws_path,
      interactive: data.job?.status === "waiting_interactive",
    });
    setShowPlugins(false);
  }, []);

  // Stream an already-launched deploy/rollback job (WS /projects/{name}/deploy) in the
  // InstallPane — used by a rollback from the VersionsModal (no new project object).
  const handleDeployStream = useCallback(({ project, wsPath }) => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    setActiveLog(null);
    setInteractiveLog({ project, wsPath, interactive: false });
  }, []);

  // The build streamed to completion over the install WebSocket — refresh the project
  // list so its status (running / failed) updates. The pane stays open showing the log.
  const handleInstallExit = useCallback((code) => {
    fetchProjects();
  }, [fetchProjects]);

  const handleAbort = async () => {
    if (!activeLog) return;
    const abortPath = activeLog.kind === "compose"
      ? `/projects/${activeLog.project}/compose/abort`
      : `/projects/${activeLog.project}/abort`;
    try {
      const d = await client.post(abortPath);
      setActiveLog(l => l ? { ...l, status: "aborted", logs: d.logs || d.message || l.logs } : null);
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    } catch (e) {
      setActiveLog(l => l ? { ...l, logs: (l.logs || "") + `\n✗ abort failed: ${e.message}` } : null);
    }
  };

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  const healthColor = health === "ok" ? C.green : health === "unreachable" ? C.red : C.amber;

  const sectionTitle = showDeploy ? "Deploy project"
    : showPlugins ? "Plugins"
    : `Projects (${projects.length})`;

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.txt, fontFamily: C.ff }}>
      {/* Left nav rail (Grafana-style) */}
      <nav className={`fh-rail${railOpen ? " open" : ""}`} style={{
        position: "fixed", top: 0, left: 0, bottom: 0, width: "220px", zIndex: 60,
        background: C.s2, borderRight: `1px solid ${C.bd}`,
        display: "flex", flexDirection: "column", padding: "14px 12px", overflowY: "auto",
      }}>
        {/* Brand */}
        <div style={{ display: "flex", alignItems: "center", gap: "9px", padding: "4px 8px 12px" }}>
          <span style={{ color: C.brand, fontSize: "17px", fontWeight: 700, letterSpacing: "-0.3px" }}>freeholdy</span>
        </div>
        {version && (
          <div style={{ margin: "0 8px 14px", alignSelf: "flex-start", color: C.muted, fontSize: "10px", fontWeight: 600, padding: "3px 8px", border: `1px solid ${C.bd}`, borderRadius: "6px" }}>
            v{version.version} · {version.type}
          </div>
        )}
        {/* Nav */}
        <div style={{ flex: 1 }}>
          <NavItem icon="▣" label="Projects" active={!showDeploy && !showPlugins}
            onClick={() => { setShowDeploy(false); setShowPlugins(false); setRailOpen(false); }} />
          <NavItem icon="＋" label="Deploy" active={showDeploy}
            onClick={() => { setShowPlugins(false); setShowDeploy(true); setRailOpen(false); }} />
          <NavItem icon="⧉" label="Plugins" active={showPlugins}
            onClick={() => { setShowDeploy(false); setShowPlugins(true); setRailOpen(false); }} />
          <NavItem icon="🔑" label="Git key"
            title="Get the server's GitHub SSH public key to add to GitHub for cloning private repos"
            onClick={() => { setShowGitKey(true); setRailOpen(false); }} />
        </div>
        {/* Footer */}
        <div style={{ borderTop: `1px solid ${C.bd}`, paddingTop: "8px" }}>
          <NavItem icon="↻" label="Refresh" onClick={() => { checkHealth(); fetchProjects(); }} />
          <NavItem icon="⎋" label="Logout" onClick={onLogout} />
        </div>
      </nav>

      {railOpen && (
        <div onClick={() => setRailOpen(false)} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", zIndex: 55 }} />
      )}

      {/* Main column (shifts right of the rail) */}
      <div className="fh-main" style={{ marginLeft: "220px", minHeight: "100vh" }}>
        {/* Slim top bar */}
        <div style={{ background: C.s1, borderBottom: `1px solid ${C.bd}`, padding: "0 20px", display: "flex", alignItems: "center", justifyContent: "space-between", height: "48px", position: "sticky", top: 0, zIndex: 50 }}>
          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <button className="fh-burger" onClick={() => setRailOpen(o => !o)} style={{ display: "none", alignItems: "center", justifyContent: "center", background: "transparent", border: `1px solid ${C.bd}`, borderRadius: "6px", color: C.muted, fontSize: "14px", padding: "4px 9px", cursor: "pointer" }}>☰</button>
            <span style={{ color: C.txt, fontSize: "13px", fontWeight: 600 }}>{sectionTitle}</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
            <span style={{ color: healthColor, fontFamily: C.ff, fontSize: "10px", display: "flex", alignItems: "center", gap: "5px" }}>
              <span style={{ fontSize: "7px" }}>●</span>api {health ?? "checking…"}
            </span>
            <span style={{ width: 1, height: 14, background: C.bd, display: "inline-block" }} />
            <span style={{ color: C.dim, fontSize: "10px" }}>{DOMAIN}</span>
          </div>
        </div>

        {/* Content */}
        <div style={{ maxWidth: 1260, margin: "0 auto", padding: "18px 22px" }}>
          {showDeploy && (
            <DeployForm token={token}
              onDeployed={(data) => { setShowDeploy(false); handleInstalled(data); }}
              onSynced={fetchProjects}
              onCancel={() => setShowDeploy(false)} />
          )}

          {showPlugins && (
            <PluginPanel token={token} onInstalled={handleInstalled} onCancel={() => setShowPlugins(false)} />
          )}

          {/* The deployed-projects list shows only in the Projects view — Deploy / Plugins
              replace it with their own panel (above). */}
          {!showDeploy && !showPlugins && (
            loading ? (
              <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px", padding: "24px 0" }}>loading projects…</div>
            ) : projects.length === 0 ? (
              <div style={{ border: `1px dashed ${C.bd}`, borderRadius: "8px", padding: "40px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
                no projects yet — use <span style={{ color: C.brand }}>Deploy</span> or <span style={{ color: C.brand }}>Plugins</span> in the sidebar
              </div>
            ) : (
              projects.map(p => (
                <ProjectCard key={p.name} project={p} token={token}
                  onOperation={handleOperation}
                  onRefresh={fetchProjects}
                  onDeploy={handleInstalled}
                  onStream={handleDeployStream}
                  onRemoved={(name) => { setProjects(ps => ps.filter(x => x.name !== name)); if (activeLog?.project === name) setActiveLog(null); if (interactiveLog?.project === name) setInteractiveLog(null); }} />
              ))
            )
          )}

          {interactiveLog ? (
            <InstallPane
              token={token}
              wsPath={interactiveLog.wsPath}
              project={interactiveLog.project}
              interactive={interactiveLog.interactive}
              onExit={handleInstallExit}
              onClose={() => setInteractiveLog(null)}
            />
          ) : (
            <LogPane log={activeLog} onClose={() => { setActiveLog(null); if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } }} onAbort={handleAbort} />
          )}
        </div>
      </div>

      {showGitKey && <GitKeyModal token={token} onClose={() => setShowGitKey(false)} />}
    </div>
  );
};

// ── App root ──────────────────────────────────────────────────────────────────
// Capture a token handed in via the /token/{TOKEN} deep link: store it like a normal
// login, then strip it from the address bar / history so it isn't left lying around.
const tokenFromPath = () => {
  const m = window.location.pathname.match(/^\/token\/(.+)$/);
  if (!m) return "";
  const t = decodeURIComponent(m[1]).trim();
  if (t) localStorage.setItem("freeholdy_token", t);
  window.history.replaceState(null, "", "/");  // back to the app root, token-free URL
  return t;
};

export default function App() {
  const [token, setToken] = useState(() => tokenFromPath() || localStorage.getItem("freeholdy_token") || "");

  if (!token) return <LoginScreen onAuth={(t) => { localStorage.setItem("freeholdy_token", t); setToken(t); }} />;

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Roboto+Mono:wght@400;500&display=swap');
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { background: #111217; }
        body { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; color: #ccccdc; }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #181b1f; }
        ::-webkit-scrollbar-thumb { background: #3d444b; border-radius: 8px; }
        ::-webkit-scrollbar-thumb:hover { background: #4b5259; }
        input::placeholder { color: #5b5e63; }
        input:focus { border-color: #F55F3E !important; box-shadow: 0 0 0 3px rgba(245,95,62,.18); }
        select option { background: #181b1f; color: #ccccdc; }
        a { transition: color .15s; }
        a:hover { color: #F55F3E; }
        button { transition: background .12s, opacity .12s, box-shadow .12s; }
        button:hover:not(:disabled) { filter: brightness(1.12); }
        .fh-rail { transition: transform .2s ease; }
        @media (max-width: 820px) {
          .fh-rail { transform: translateX(-100%); }
          .fh-rail.open { transform: translateX(0); box-shadow: 0 0 40px rgba(0,0,0,.6); }
          .fh-main { margin-left: 0 !important; }
          .fh-burger { display: inline-flex !important; }
        }
      `}</style>
      <Dashboard token={token} onLogout={() => { localStorage.removeItem("freeholdy_token"); setToken(""); }} />
    </>
  );
}
