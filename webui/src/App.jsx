import { useState, useEffect, useRef, useCallback, useMemo } from "react";

const BASE = import.meta.env.VITE_API_URL || "https://api.your_domain.com";
const POLL_MS = 1000;

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
  return {
    get:  (p)    => fetch(`${BASE}${p}`, { headers: h }).then(unwrap),
    post: (p, b) => fetch(`${BASE}${p}`, { method: "POST", headers: h, body: b != null ? JSON.stringify(b) : undefined }).then(unwrap),
    form: (p, fd) => fetch(`${BASE}${p}`, { method: "POST", headers: hf, body: fd }).then(unwrap),
    del:  (p)    => fetch(`${BASE}${p}`, { method: "DELETE", headers: h }).then(unwrap),
  };
};

// ── Theme ─────────────────────────────────────────────────────────────────────
// Light, calm palette inspired by hostinger.com: lavender-white surfaces, a
// royal-purple primary accent, dark-navy text, soft borders and gentle shadows.
const C = {
  bg:     "#f4f4fb",   // app background — light lavender
  s1:     "#ffffff",   // primary card surface
  s2:     "#faf9ff",   // header / secondary surface
  s3:     "#f1f0fa",   // tertiary surface (default buttons, fills)
  bd:     "#ececf4",   // subtle border
  bdB:    "#dedaee",   // stronger border
  purple: "#673de6",   // brand / primary accent
  green:  "#149a6a",   // success / running
  amber:  "#c77f1a",   // warning / exited
  red:    "#dc4549",   // error / danger
  blue:   "#2f6bff",   // info / links
  txt:    "#1b1a3a",   // primary text (dark meteorite)
  muted:  "#6c6a86",   // secondary text
  dim:    "#a6a3c0",   // tertiary text / placeholders
  shadow: "0 1px 2px rgba(27,26,58,.05), 0 6px 20px rgba(27,26,58,.05)",
  ff:     "'DM Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  mono:   "'JetBrains Mono', 'Fira Code', monospace",
};

const SC = {
  running: C.green, done: C.green,
  exited: C.amber, aborted: C.amber,
  error: C.red,
  no_image: C.muted, not_found: C.muted, no_job: C.muted,
};
const SI = {
  running: "▶", done: "✓", exited: "■", aborted: "⚠",
  error: "✗", no_image: "○", not_found: "○", no_job: "—",
};

// ── Tiny shared components ────────────────────────────────────────────────────
const Btn = ({ children, onClick, v = "default", sm, disabled, busy, title, style: st = {} }) => {
  const vs = {
    default: { bg: C.s3,           color: C.txt,    bd: C.bdB },
    primary: { bg: "#f0ecfe",      color: C.purple, bd: "#dccffb" },
    danger:  { bg: "#fdecec",      color: C.red,    bd: "#f6d2d3" },
    amber:   { bg: "#fbf2e0",      color: C.amber,  bd: "#f0e1bc" },
    ghost:   { bg: "transparent",  color: C.muted,  bd: "transparent" },
    blue:    { bg: "#e9f0ff",      color: C.blue,   bd: "#cfddff" },
    green:   { bg: C.green,        color: "#ffffff", bd: C.green },
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
  <div style={{ color: C.red, fontFamily: C.ff, fontSize: "11px", padding: "6px 10px", background: "#fdecec", border: `1px solid #f6d2d3`, borderRadius: "8px" }}>
    ✗ {msg}
  </div>
) : null;

const Ok = ({ msg }) => msg ? (
  <div style={{ color: C.green, fontFamily: C.ff, fontSize: "11px", padding: "6px 10px", background: "#e6f6ef", border: `1px solid #c6e9d9`, borderRadius: "8px" }}>
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
          <div key={k} style={{ color: b.level === 1 ? C.txt : C.purple, fontWeight: b.level === 1 ? 700 : 600, fontSize: hSize[b.level] || "12px", letterSpacing: b.level >= 3 ? "0.06em" : 0, textTransform: b.level >= 3 ? "uppercase" : "none", margin: k === 0 ? "0 0 10px" : "18px 0 8px" }}>{mdInline(b.text, `h${k}`)}</div>
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

// ── Modal wrapper ─────────────────────────────────────────────────────────────
const Modal = ({ onClose, width = 460, children }) => (
  <div style={{ position: "fixed", inset: 0, background: "rgba(27,26,58,.38)", backdropFilter: "blur(2px)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999 }} onClick={onClose}>
    <div style={{ background: C.s1, border: `1px solid ${C.bdB}`, borderRadius: "8px", padding: "22px", width, maxWidth: "95vw", boxShadow: "0 24px 64px rgba(27,26,58,.18)" }} onClick={e => e.stopPropagation()}>
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

// ── Exec modal ────────────────────────────────────────────────────────────────
const ExecModal = ({ project, onClose, onSubmit }) => {
  const [cmd, setCmd] = useState("");
  return (
    <Modal onClose={onClose}>
      <ModalHeader title="EXEC IN CONTAINER" color={C.amber} onClose={onClose} />
      <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginBottom: "10px" }}>{project}</div>
      <TextIn value={cmd} onChange={setCmd} placeholder="ls /app" style={{ marginBottom: "12px" }} />
      <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
        <Btn v="ghost" onClick={onClose}>cancel</Btn>
        <Btn v="amber" onClick={() => { if (cmd.trim()) { onSubmit(cmd.trim()); onClose(); } }} disabled={!cmd.trim()}>run</Btn>
      </div>
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
      <ModalHeader title="INSTALL PLUGIN" color={C.purple} onClose={onClose} />
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

const UploadModal = ({ token, project, onClose, onUploaded }) => {
  const [entries, setEntries] = useState([]);   // [{ file, rel }]
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const dirRef = useRef();

  // React drops the non-standard directory attributes, so set them on mount.
  useEffect(() => {
    if (dirRef.current) {
      dirRef.current.setAttribute("webkitdirectory", "");
      dirRef.current.setAttribute("directory", "");
    }
  }, []);

  const pickFiles = (fileList) => {
    setError(""); setResult(null);
    setEntries([...fileList].map(f => ({ file: f, rel: f.name })));
  };
  const pickFolder = (fileList) => {
    setError(""); setResult(null);
    setEntries([...fileList].map(f => ({ file: f, rel: stripRoot(f.webkitRelativePath || f.name) })));
  };

  const upload = async () => {
    if (!entries.length) return setError("Select a file or folder first");
    setError(""); setBusy(true);
    try {
      const fd = new FormData();
      for (const { file, rel } of entries) fd.append("files", file, rel);
      const data = await mkApi(token).form(`/projects/${project}/upload`, fd);
      setResult(data);
      onUploaded && onUploaded();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  const dropStyle = { flex: 1, display: "block", border: `2px dashed ${C.bdB}`, borderRadius: "8px", padding: "16px", textAlign: "center", background: C.s1, cursor: "pointer" };

  return (
    <Modal onClose={onClose} width={540}>
      <ModalHeader title="UPLOAD FILES" color={C.purple} onClose={onClose} />
      <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", marginBottom: "14px", lineHeight: "1.6" }}>
        {project} · a <span style={{ color: C.txt }}>Dockerfile</span> or <span style={{ color: C.txt }}>docker-compose.yml</span> in the
        uploaded root sets the deploy mode and provisions automatically (compose wins).
      </div>

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

      {entries.length > 0 && (
        <div style={{ color: C.green, fontFamily: C.ff, fontSize: "11px", marginBottom: "12px" }}>
          ✓ {entries.length} file(s) selected
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
        <Btn v="primary" onClick={upload} busy={busy} disabled={!entries.length}>upload</Btn>
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

// ── Row cells (shared layout) ───────────────────────────────────────────────────
const Cells = ({ label, info }) => (
  <>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "11px", color: C.blue, minWidth: "90px" }}>{label}</td>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "10px" }}>
      {info.subdomain
        ? <a href={`https://${info.subdomain}`} target="_blank" rel="noreferrer" style={{ color: C.muted, textDecoration: "none" }}>{info.subdomain}</a>
        : <span style={{ color: C.dim }}>—</span>}
      {info.custom_domain && (
        <span style={{ color: C.blue, background: "#e9f0ff", border: "1px solid #cfddff", fontSize: "8px", letterSpacing: "0.08em", padding: "1px 5px", borderRadius: "8px", marginLeft: "7px" }}>custom</span>
      )}
    </td>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "11px", color: C.txt, textAlign: "right", minWidth: "55px" }}>{info.local_port ?? "—"}</td>
    <td style={{ padding: "7px 10px", textAlign: "center", minWidth: "38px" }}>
      <span style={{ color: info.ssl_enabled ? C.green : C.dim, fontFamily: C.ff, fontSize: "11px" }}>{info.ssl_enabled ? "✓" : "✗"}</span>
    </td>
    <td style={{ padding: "7px 10px", minWidth: "130px" }}><Tag status={info.container_status} /></td>
  </>
);

// ── Container row (dockerfile mode: one container per project, project-level ops) ──
const ContainerRow = ({ project, info, token, onOperation, onRefresh }) => {
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
            <Btn sm v="primary" onClick={() => act("build")} busy={busy.build} title="Build Docker image">build</Btn>
            <Btn sm v="primary" onClick={() => act("start")} busy={busy.start} disabled={isRunning} title="Start container">start</Btn>
            <Btn sm v="danger" onClick={() => act("stop")} busy={busy.stop} disabled={!isRunning} title="Stop container">stop</Btn>
            <Btn sm v="amber" onClick={() => setModal({ type: "exec" })} disabled={!isRunning} title="Exec command in container">exec</Btn>
            <Btn sm onClick={() => act("ssl")} busy={busy.ssl} title="Issue/renew SSL cert">ssl</Btn>
            <Btn sm v="blue" onClick={() => setModal({ type: "domain" })} title="Set or clear a custom domain">domain</Btn>
            <Btn sm v="ghost" onClick={() => act("status")} busy={busy.status} title="View job status & logs">status</Btn>
          </div>
        </td>
      </tr>

      {modal?.type === "exec"       && <ExecModal project={project} onClose={() => setModal(null)} onSubmit={(cmd) => act("exec", { command: cmd })} />}
      {modal?.type === "status"     && <StatusModal data={modal.data} project={project} onClose={() => setModal(null)} />}
      {modal?.type === "ssl"        && <SslModal data={modal.data} project={project} onClose={() => setModal(null)} />}
      {modal?.type === "domain"     && <DomainModal token={token} project={project} info={info} onClose={() => setModal(null)} onDone={onRefresh} />}
    </>
  );
};

// ── Service row (compose mode: display-only lifecycle, but custom domain is per-service) ──
const ServiceRow = ({ project, info, token, onRefresh }) => {
  const [modal, setModal] = useState(null);
  return (
    <tr style={{ borderBottom: `1px solid ${C.bd}` }}>
      <Cells label={info.name} info={info} />
      <td style={{ padding: "6px 8px" }}>
        <Btn sm v="blue" onClick={() => setModal("domain")} title="Set or clear a custom domain">domain</Btn>
        {modal === "domain" && <DomainModal token={token} project={project} service={info.name} info={info} onClose={() => setModal(null)} onDone={onRefresh} />}
      </td>
    </tr>
  );
};

// ── Project card ──────────────────────────────────────────────────────────────
const ProjectCard = ({ project, token, onOperation, onRemoved, onRefresh }) => {
  const [confirm, setConfirm] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [busy, setBusy] = useState({});
  const [uploadModal, setUploadModal] = useState(false);
  const isCompose = project.deploy_mode === "compose";
  const isPending = project.deploy_mode !== "compose" && project.deploy_mode !== "dockerfile";

  const remove = async () => {
    setRemoving(true);
    try {
      await mkApi(token).del(`/projects/${project.name}`);
      onRemoved(project.name);
    } catch (e) { alert(`Remove failed: ${e.message}`); }
    finally { setRemoving(false); setConfirm(false); }
  };

  // Project-level docker compose build/up/down — streamed via the bottom LogPane.
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
            <span style={{ color: C.purple, fontFamily: C.ff, fontSize: "14px", fontWeight: 600 }}>{project.name}</span>
            {isCompose && (
              <span style={{ color: C.purple, background: "#f0ecfe", border: "1px solid #dccffb", fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.08em", padding: "1px 7px", borderRadius: "8px" }}>compose</span>
            )}
            {isPending && (
              <span style={{ color: C.amber, background: "#fbf2e0", border: "1px solid #f0e1bc", fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.08em", padding: "1px 7px", borderRadius: "8px" }}>pending</span>
            )}
            <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px" }}>
              {isCompose ? `${project.services?.length ?? 0} service${project.services?.length !== 1 ? "s" : ""}` : isPending ? "awaiting upload" : "container"}
            </span>
            {project.created_at && (
              <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px" }}>created {new Date(project.created_at).toLocaleDateString()}</span>
            )}
          </div>
          <div style={{ display: "flex", gap: "6px" }}>
            <Btn sm onClick={() => setUploadModal(true)} title="Upload files or a folder — auto-detects Dockerfile / docker-compose.yml and provisions">upload</Btn>
            {isCompose && (
              <>
                <Btn sm v="primary" onClick={() => composeAct("build")} busy={busy.build} title="docker compose build">build</Btn>
                <Btn sm v="primary" onClick={() => composeAct("up")} busy={busy.up} title="docker compose up -d">up</Btn>
                <Btn sm v="danger" onClick={() => composeAct("down")} busy={busy.down} title="docker compose down">down</Btn>
              </>
            )}
            <Btn v="danger" sm onClick={() => setConfirm(true)} busy={removing}>remove</Btn>
          </div>
        </div>

        {isPending ? (
          <div style={{ padding: "18px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
            awaiting upload — use <span style={{ color: C.purple }}>upload</span> to add a Dockerfile or docker-compose.yml
          </div>
        ) : isCompose && (project.services || []).length === 0 ? (
          <div style={{ padding: "18px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
            no services yet — use <span style={{ color: C.purple }}>upload</span> to add a docker-compose.yml
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
                ? (project.services || []).map(s => <ServiceRow key={s.name} project={project.name} info={s} token={token} onRefresh={onRefresh} />)
                : (project.container
                    ? <ContainerRow project={project.name} info={project.container} token={token} onOperation={onOperation} onRefresh={onRefresh} />
                    : null)}
            </tbody>
          </table>
        </div>
        )}
      </div>

      {uploadModal && <UploadModal token={token} project={project.name} onClose={() => setUploadModal(false)} onUploaded={() => onRefresh && onRefresh()} />}
      {confirm && <ConfirmModal message={`Delete "${project.name}"? This stops containers, removes images and nginx config.`} onConfirm={remove} onCancel={() => setConfirm(false)} loading={removing} />}
    </>
  );
};

// ── Create project form ───────────────────────────────────────────────────────
const CreateForm = ({ token, onCreated, onCancel }) => {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    if (!name.trim()) return setError("Project name is required");
    setError(""); setBusy(true);
    try {
      const data = await mkApi(token).post("/projects", { name: name.trim() });
      onCreated(data);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };

  // The project name is the subdomain label, so it must be a DNS-safe slug.
  const slug = name.trim().toLowerCase();

  return (
    <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "18px", marginBottom: "12px", boxShadow: C.shadow }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "14px" }}>
        <span style={{ color: C.purple, fontFamily: C.ff, fontSize: "11px", letterSpacing: "0.1em", fontWeight: 600 }}>NEW PROJECT</span>
        <Btn v="ghost" sm onClick={onCancel}>✕</Btn>
      </div>

      <div style={{ display: "grid", gap: "12px" }}>
        <Field label="PROJECT NAME (used as the subdomain)">
          <TextIn value={name} onChange={setName} placeholder="myapp" />
          <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px", marginTop: "5px" }}>
            → served at <span style={{ color: C.blue }}>https://{slug || "myapp"}.your_domain.com</span>
            <span style={{ color: C.dim }}> · point a custom domain at it later from the project card</span>
          </div>
        </Field>

        <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.6", background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "10px 12px" }}>
          Creates an empty project. After creating it, use <span style={{ color: C.purple }}>upload</span> on the project card to
          send your files (a single file or a whole folder). The server scans the uploaded root for a
          <span style={{ color: C.txt }}> Dockerfile</span> or <span style={{ color: C.txt }}>docker-compose.yml</span>, picks the
          deploy mode automatically (compose wins), and wires up nginx + SSL. A Dockerfile must
          <span style={{ color: C.txt }}> EXPOSE</span> its port.
        </div>

        <Err msg={error} />

        <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
          <Btn v="ghost" onClick={onCancel} disabled={busy}>cancel</Btn>
          <Btn v="primary" onClick={submit} busy={busy}>create project</Btn>
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
    <span style={{ background: accent ? "#f0ecfe" : C.s3, color: accent ? C.purple : C.muted, border: `1px solid ${accent ? "#dccffb" : C.bd}`, borderRadius: "8px", padding: "2px 9px", fontFamily: C.ff, fontSize: "10px" }}>{text}</span>
  );

  const active = plugins.find(p => p.name === selected) || null;

  return (
    <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", marginBottom: "12px", boxShadow: C.shadow, overflow: "hidden" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", borderBottom: `1px solid ${C.bd}`, background: C.s2 }}>
        <span style={{ color: C.purple, fontFamily: C.ff, fontSize: "11px", letterSpacing: "0.1em", fontWeight: 600 }}>AVAILABLE PLUGINS</span>
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
                  background: sel ? "#f0ecfe" : "transparent",
                  color: sel ? C.purple : C.txt,
                  border: `1px solid ${sel ? "#dccffb" : "transparent"}`,
                  borderRadius: "8px", padding: "9px 11px", marginBottom: "2px", cursor: "pointer",
                  fontFamily: C.ff, fontSize: "13px", fontWeight: sel ? 600 : 500,
                }}>
                  {p.name}
                  <div style={{ color: sel ? C.purple : C.dim, fontSize: "10px", fontWeight: 400, marginTop: "2px", opacity: sel ? 0.8 : 1 }}>
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
                    </div>
                  </div>
                  <Btn v="green" onClick={() => setInstalling(active)} style={{ flexShrink: 0 }}>install</Btn>
                </div>
                <Markdown text={active.about || active.description} />
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
      <div style={{ width: 380, background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "8px", padding: "36px", boxShadow: "0 12px 48px rgba(27,26,58,.12)" }}>
        <div style={{ marginBottom: "30px" }}>
          <div style={{ color: C.purple, fontFamily: C.ff, fontSize: "22px", fontWeight: 700, marginBottom: "6px" }}>🐾 freeholdy</div>
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

// ── Dashboard ─────────────────────────────────────────────────────────────────
const Dashboard = ({ token, onLogout }) => {
  const [projects, setProjects] = useState([]);
  const [health, setHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [showPlugins, setShowPlugins] = useState(false);
  const [activeLog, setActiveLog] = useState(null);
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

  useEffect(() => { checkHealth(); fetchProjects(); }, []);

  // Both modes poll a project-level status endpoint (compose has its own path).
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
    if (data.project.deploy_mode === "compose") {
      // Compose plugins stream `docker compose up` at the project level.
      handleOperation({
        project: data.project.name,
        kind: "compose",
        operation: data.job.operation || "compose_up",
        status: data.job.status,
        logs: data.job.logs || data.job.message || "",
      });
    } else {
      // Dockerfile plugins stream the provision job at the project level.
      handleOperation({
        project: data.project.name,
        operation: data.job.operation || "provision",
        status: data.job.status,
        logs: data.job.logs || data.job.message || "",
      });
    }
    setShowPlugins(false);
  }, [handleOperation]);

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

  return (
    <div style={{ minHeight: "100vh", background: C.bg, color: C.txt, fontFamily: C.ff }}>
      {/* Header */}
      <div style={{ background: C.s1, borderBottom: `1px solid ${C.bd}`, padding: "0 22px", display: "flex", alignItems: "center", justifyContent: "space-between", height: "52px", position: "sticky", top: 0, zIndex: 50, boxShadow: "0 1px 3px rgba(27,26,58,.06)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
          <span style={{ color: C.purple, fontSize: "15px", fontWeight: 700 }}>🐾 freeholdy</span>
          <span style={{ width: 1, height: 16, background: C.bd, display: "inline-block" }} />
          <span style={{ color: C.dim, fontSize: "10px" }}>your_domain.com</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
          <span style={{ color: healthColor, fontFamily: C.ff, fontSize: "10px", display: "flex", alignItems: "center", gap: "5px" }}>
            <span style={{ fontSize: "7px" }}>●</span>api {health ?? "checking…"}
          </span>
          <Btn v="ghost" sm onClick={() => { checkHealth(); fetchProjects(); }}>↻ refresh</Btn>
          <Btn v="ghost" sm onClick={onLogout}>logout</Btn>
        </div>
      </div>

      {/* Content */}
      <div style={{ maxWidth: 1260, margin: "0 auto", padding: "16px 20px" }}>
        {/* Toolbar */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "12px" }}>
          <span style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", letterSpacing: "0.1em" }}>
            PROJECTS ({projects.length})
          </span>
          <div style={{ display: "flex", gap: "8px" }}>
            <Btn v="primary" onClick={() => { setShowPlugins(false); setShowCreate(s => !s); }}>
              {showCreate ? "✕ cancel" : "+ new project"}
            </Btn>
            <Btn v="blue" onClick={() => { setShowCreate(false); setShowPlugins(s => !s); }}>
              {showPlugins ? "✕ cancel" : "+ add plugin"}
            </Btn>
          </div>
        </div>

        {showCreate && (
          <CreateForm token={token}
            onCreated={(data) => { setProjects(p => [data, ...p.filter(x => x.name !== data.name)]); setShowCreate(false); }}
            onCancel={() => setShowCreate(false)} />
        )}

        {showPlugins && (
          <PluginPanel token={token} onInstalled={handleInstalled} onCancel={() => setShowPlugins(false)} />
        )}

        {loading ? (
          <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px", padding: "24px 0" }}>loading projects…</div>
        ) : projects.length === 0 ? (
          <div style={{ border: `1px dashed ${C.bd}`, borderRadius: "8px", padding: "40px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
            no projects yet — create one above
          </div>
        ) : (
          projects.map(p => (
            <ProjectCard key={p.name} project={p} token={token}
              onOperation={handleOperation}
              onRefresh={fetchProjects}
              onRemoved={(name) => { setProjects(ps => ps.filter(x => x.name !== name)); if (activeLog?.project === name) setActiveLog(null); }} />
          ))
        )}

        <LogPane log={activeLog} onClose={() => { setActiveLog(null); if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } }} onAbort={handleAbort} />

        <div style={{ marginTop: "20px", padding: "12px 14px", background: C.s2, border: `1px solid ${C.bd}`, borderRadius: "8px" }}>
          <span style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px" }}>
            ⚠ direct SFTP transfers (fhold sftp-upload) are CLI-only — the web UI uploads files over the API via the upload button
          </span>
        </div>
      </div>
    </div>
  );
};

// ── App root ──────────────────────────────────────────────────────────────────
export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem("freeholdy_token") || "");

  if (!token) return <LoginScreen onAuth={(t) => { localStorage.setItem("freeholdy_token", t); setToken(t); }} />;

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:ital,wght@0,400;0,500;1,400&display=swap');
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        html, body { background: #f4f4fb; }
        body { -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; color: #1b1a3a; }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #f1f0fa; }
        ::-webkit-scrollbar-thumb { background: #d6d2e8; border-radius: 8px; }
        ::-webkit-scrollbar-thumb:hover { background: #c2bdde; }
        input::placeholder { color: #a6a3c0; }
        input:focus { border-color: #673de6 !important; box-shadow: 0 0 0 3px rgba(103,61,230,.12); }
        select option { background: #ffffff; color: #1b1a3a; }
        a { transition: color .15s; }
        a:hover { color: #673de6; }
        button { transition: background .12s, opacity .12s, box-shadow .12s; }
        button:hover:not(:disabled) { filter: brightness(0.98); }
      `}</style>
      <Dashboard token={token} onLogout={() => { localStorage.removeItem("freeholdy_token"); setToken(""); }} />
    </>
  );
}
