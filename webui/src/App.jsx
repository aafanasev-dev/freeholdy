import { useState, useEffect, useRef, useCallback, useMemo } from "react";

const BASE = import.meta.env.VITE_API_URL || "https://api.cloudopen.space";
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
const C = {
  bg:    "#07070f",
  s1:    "#0c0c1a",
  s2:    "#111120",
  s3:    "#191928",
  bd:    "#1e1e32",
  bdB:   "#2c2c48",
  green: "#3dd68c",
  amber: "#f0a835",
  red:   "#e05c5c",
  blue:  "#6aa3f5",
  txt:   "#c8d0e8",
  muted: "#606880",
  dim:   "#383d55",
  ff:    "'JetBrains Mono', 'Fira Code', monospace",
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
    default: { bg: C.s3,           color: C.txt,   bd: C.bdB },
    primary: { bg: "#182e22",      color: C.green, bd: "#254838" },
    danger:  { bg: "#2b1414",      color: C.red,   bd: "#482020" },
    amber:   { bg: "#28220e",      color: C.amber, bd: "#473a18" },
    ghost:   { bg: "transparent",  color: C.muted, bd: "transparent" },
    blue:    { bg: "#111e33",      color: C.blue,  bd: "#1e3358" },
  };
  const vv = vs[v] || vs.default;
  return (
    <button onClick={onClick} disabled={disabled || busy} title={title} style={{
      background: vv.bg, color: (disabled || busy) ? C.dim : vv.color,
      border: `1px solid ${(disabled || busy) ? C.bd : vv.bd}`,
      fontFamily: C.ff, fontSize: sm ? "10px" : "11px",
      padding: sm ? "2px 7px" : "5px 12px", cursor: (disabled || busy) ? "not-allowed" : "pointer",
      borderRadius: "2px", opacity: (disabled || busy) ? 0.55 : 1,
      whiteSpace: "nowrap", letterSpacing: "0.02em", transition: "opacity .1s",
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
      background: C.s1, border: `1px solid ${C.bd}`, color: C.txt,
      fontFamily: C.ff, fontSize: "11px", padding: "5px 10px",
      borderRadius: "2px", outline: "none", width: "100%", boxSizing: "border-box", ...st,
    }}
  />
);

const Err = ({ msg }) => msg ? (
  <div style={{ color: C.red, fontFamily: C.ff, fontSize: "11px", padding: "6px 10px", background: "#280f0f", border: `1px solid #442020`, borderRadius: "2px" }}>
    ✗ {msg}
  </div>
) : null;

const Ok = ({ msg }) => msg ? (
  <div style={{ color: C.green, fontFamily: C.ff, fontSize: "11px", padding: "6px 10px", background: "#0f2518", border: `1px solid #204030`, borderRadius: "2px" }}>
    ✓ {msg}
  </div>
) : null;

// ── Log pane ──────────────────────────────────────────────────────────────────
const LogPane = ({ log, onClose, onAbort }) => {
  const ref = useRef();
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [log?.logs]);
  if (!log) return null;
  return (
    <div style={{ background: C.s1, border: `1px solid ${C.bdB}`, borderRadius: "2px", display: "flex", flexDirection: "column", height: "280px", marginTop: "10px" }}>
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
      <div ref={ref} style={{ flex: 1, overflow: "auto", padding: "10px 14px", fontFamily: C.ff, fontSize: "11px", color: C.txt, lineHeight: "1.65", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
        {log.logs || <span style={{ color: C.dim }}>waiting for output…</span>}
      </div>
    </div>
  );
};

// ── Modal wrapper ─────────────────────────────────────────────────────────────
const Modal = ({ onClose, width = 460, children }) => (
  <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.72)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 9999 }} onClick={onClose}>
    <div style={{ background: C.s2, border: `1px solid ${C.bdB}`, borderRadius: "2px", padding: "22px", width, maxWidth: "95vw", boxShadow: "0 24px 64px rgba(0,0,0,.6)" }} onClick={e => e.stopPropagation()}>
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
      <ModalHeader title="INSTALL PLUGIN" color={C.green} onClose={onClose} />
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

  const dropStyle = { flex: 1, display: "block", border: `2px dashed ${C.bdB}`, borderRadius: "2px", padding: "16px", textAlign: "center", background: C.s1, cursor: "pointer" };

  return (
    <Modal onClose={onClose} width={540}>
      <ModalHeader title="UPLOAD FILES" color={C.green} onClose={onClose} />
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
        <div key={k} style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "2px", padding: "8px 12px" }}>
          <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.1em", marginBottom: "4px" }}>{k.toUpperCase()}</div>
          <div style={{ color: k === "status" ? (SC[v] || C.txt) : C.txt, fontFamily: C.ff, fontSize: "12px" }}>{String(v)}</div>
        </div>
      ))}
    </div>
    <Field label="LOGS">
      <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "2px", padding: "10px 12px", fontFamily: C.ff, fontSize: "11px", color: C.txt, lineHeight: "1.65", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: "220px", overflow: "auto" }}>
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
        <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "2px", padding: "10px 12px", fontFamily: C.ff, fontSize: "11px", color: C.txt, lineHeight: "1.6", whiteSpace: "pre-wrap", maxHeight: "200px", overflow: "auto" }}>
          {data.message}
        </div>
      </Field>
    )}
    <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "12px" }}>
      <Btn v="ghost" onClick={onClose}>close</Btn>
    </div>
  </Modal>
);

// ── Row cells (shared layout) ───────────────────────────────────────────────────
const Cells = ({ label, info }) => (
  <>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "11px", color: C.blue, minWidth: "90px" }}>{label}</td>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "10px" }}>
      {info.subdomain
        ? <a href={`https://${info.subdomain}`} target="_blank" rel="noreferrer" style={{ color: C.muted, textDecoration: "none" }}>{info.subdomain}</a>
        : <span style={{ color: C.dim }}>—</span>}
    </td>
    <td style={{ padding: "7px 10px", fontFamily: C.ff, fontSize: "11px", color: C.txt, textAlign: "right", minWidth: "55px" }}>{info.local_port ?? "—"}</td>
    <td style={{ padding: "7px 10px", textAlign: "center", minWidth: "38px" }}>
      <span style={{ color: info.ssl_enabled ? C.green : C.dim, fontFamily: C.ff, fontSize: "11px" }}>{info.ssl_enabled ? "✓" : "✗"}</span>
    </td>
    <td style={{ padding: "7px 10px", minWidth: "130px" }}><Tag status={info.container_status} /></td>
  </>
);

// ── Container row (dockerfile mode: one container per project, project-level ops) ──
const ContainerRow = ({ project, info, token, onOperation }) => {
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
            <Btn sm v="ghost" onClick={() => act("status")} busy={busy.status} title="View job status & logs">status</Btn>
          </div>
        </td>
      </tr>

      {modal?.type === "exec"       && <ExecModal project={project} onClose={() => setModal(null)} onSubmit={(cmd) => act("exec", { command: cmd })} />}
      {modal?.type === "status"     && <StatusModal data={modal.data} project={project} onClose={() => setModal(null)} />}
      {modal?.type === "ssl"        && <SslModal data={modal.data} project={project} onClose={() => setModal(null)} />}
    </>
  );
};

// ── Service row (compose mode: display-only — lifecycle is project-level) ─────────
const ServiceRow = ({ info }) => (
  <tr style={{ borderBottom: `1px solid ${C.bd}` }}>
    <Cells label={info.name} info={info} />
    <td style={{ padding: "6px 8px", fontFamily: C.ff, fontSize: "10px", color: C.dim }}>—</td>
  </tr>
);

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
      <div style={{ background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "2px", marginBottom: "8px", overflow: "hidden" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "8px 12px", background: C.s2, borderBottom: `1px solid ${C.bd}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <span style={{ color: C.green, fontFamily: C.ff, fontSize: "13px", fontWeight: 600 }}>{project.name}</span>
            {isCompose && (
              <span style={{ color: C.green, background: "#182e22", border: "1px solid #254838", fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.08em", padding: "1px 7px", borderRadius: "2px" }}>compose</span>
            )}
            {isPending && (
              <span style={{ color: C.amber, background: "#28220e", border: "1px solid #473a18", fontFamily: C.ff, fontSize: "9px", letterSpacing: "0.08em", padding: "1px 7px", borderRadius: "2px" }}>pending</span>
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
            awaiting upload — use <span style={{ color: C.green }}>upload</span> to add a Dockerfile or docker-compose.yml
          </div>
        ) : isCompose && (project.services || []).length === 0 ? (
          <div style={{ padding: "18px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
            no services yet — use <span style={{ color: C.green }}>upload</span> to add a docker-compose.yml
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
                ? (project.services || []).map(s => <ServiceRow key={s.name} info={s} />)
                : (project.container
                    ? <ContainerRow project={project.name} info={project.container} token={token} onOperation={onOperation} />
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
    <div style={{ background: C.s2, border: `1px solid ${C.bdB}`, borderRadius: "2px", padding: "18px", marginBottom: "12px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "14px" }}>
        <span style={{ color: C.green, fontFamily: C.ff, fontSize: "11px", letterSpacing: "0.1em" }}>NEW PROJECT</span>
        <Btn v="ghost" sm onClick={onCancel}>✕</Btn>
      </div>

      <div style={{ display: "grid", gap: "12px" }}>
        <Field label="PROJECT NAME (used as the subdomain)">
          <TextIn value={name} onChange={setName} placeholder="myapp" />
          <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px", marginTop: "5px" }}>
            → served at <span style={{ color: C.blue }}>https://{slug || "myapp"}.cloudopen.space</span>
          </div>
        </Field>

        <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.6", background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "2px", padding: "10px 12px" }}>
          Creates an empty project. After creating it, use <span style={{ color: C.green }}>upload</span> on the project card to
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

// ── Plugin panel ──────────────────────────────────────────────────────────────
const PluginPanel = ({ token, onInstalled, onCancel }) => {
  const [plugins, setPlugins] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [installing, setInstalling] = useState(null);

  useEffect(() => {
    mkApi(token).get("/plugins")
      // `system` plugins create hidden projects and are not offered in the UI
      .then(ps => setPlugins(ps.filter(p => p.type !== "system")))
      .catch(e => setError(e.message))
      .finally(() => setLoading(false));
  }, [token]);

  const chip = (text, accent) => (
    <span style={{ background: accent ? "#182e22" : C.s3, color: accent ? C.green : C.txt, border: `1px solid ${accent ? "#254838" : C.bd}`, borderRadius: "2px", padding: "2px 8px", fontFamily: C.ff, fontSize: "10px" }}>{text}</span>
  );

  return (
    <div style={{ background: C.s2, border: `1px solid ${C.bdB}`, borderRadius: "2px", padding: "18px", marginBottom: "12px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "14px" }}>
        <span style={{ color: C.green, fontFamily: C.ff, fontSize: "11px", letterSpacing: "0.1em" }}>AVAILABLE PLUGINS</span>
        <Btn v="ghost" sm onClick={onCancel}>✕</Btn>
      </div>

      {loading ? (
        <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "11px", padding: "10px 0" }}>loading plugins…</div>
      ) : error ? (
        <Err msg={error} />
      ) : plugins.length === 0 ? (
        <div style={{ color: C.dim, fontFamily: C.ff, fontSize: "11px", padding: "10px 0" }}>no plugins available</div>
      ) : (
        <div style={{ display: "grid", gap: "8px" }}>
          {plugins.map(p => (
            <div key={p.name} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px", background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "2px", padding: "10px 12px" }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "4px", flexWrap: "wrap" }}>
                  <span style={{ color: C.green, fontFamily: C.ff, fontSize: "12px", fontWeight: 600 }}>{p.name}</span>
                  {chip(p.deploy_mode === "compose" ? "compose" : `port ${p.container_port}`)}
                  {p.has_install && chip("install.sh", true)}
                </div>
                {p.description && <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.5" }}>{p.description}</div>}
              </div>
              <Btn v="primary" onClick={() => setInstalling(p)}>install</Btn>
            </div>
          ))}
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
      <div style={{ width: 380, background: C.s1, border: `1px solid ${C.bdB}`, borderRadius: "2px", padding: "36px" }}>
        <div style={{ marginBottom: "30px" }}>
          <div style={{ color: C.green, fontFamily: C.ff, fontSize: "20px", marginBottom: "6px" }}>🐾 freeholdy</div>
          <div style={{ color: C.muted, fontFamily: C.ff, fontSize: "10px", letterSpacing: "0.12em" }}>CLOUDOPEN.SPACE CONTROL PANEL</div>
        </div>

        <Field label="API TOKEN" style={{ marginBottom: "10px" }}>
          <TextIn type="password" value={token} onChange={setToken} placeholder="paste your token…" />
        </Field>

        <Err msg={error} />

        <Btn v="primary" onClick={submit} busy={busy} style={{ width: "100%", marginTop: error ? "10px" : "14px", padding: "7px 12px" }}>
          connect →
        </Btn>

        <div style={{ marginTop: "22px", color: C.dim, fontFamily: C.ff, fontSize: "10px", lineHeight: "1.7", borderTop: `1px solid ${C.bd}`, paddingTop: "14px" }}>
          generate token:<br />
          python scripts/generate_token.py generate --name web_ui
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
      <div style={{ background: C.s1, borderBottom: `1px solid ${C.bd}`, padding: "0 20px", display: "flex", alignItems: "center", justifyContent: "space-between", height: "44px", position: "sticky", top: 0, zIndex: 50 }}>
        <div style={{ display: "flex", alignItems: "center", gap: "14px" }}>
          <span style={{ color: C.green, fontSize: "14px" }}>🐾 freeholdy</span>
          <span style={{ width: 1, height: 16, background: C.bd, display: "inline-block" }} />
          <span style={{ color: C.dim, fontSize: "10px" }}>cloudopen.space</span>
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
          <div style={{ border: `1px dashed ${C.bd}`, borderRadius: "2px", padding: "40px", textAlign: "center", color: C.dim, fontFamily: C.ff, fontSize: "11px" }}>
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

        <div style={{ marginTop: "20px", padding: "12px", background: C.s1, border: `1px solid ${C.bd}`, borderRadius: "2px" }}>
          <span style={{ color: C.dim, fontFamily: C.ff, fontSize: "10px" }}>
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
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,300;0,400;0,500;0,600;1,400&display=swap');
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #07070f; }
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: #0c0c1a; }
        ::-webkit-scrollbar-thumb { background: #2c2c48; border-radius: 3px; }
        input::placeholder { color: #383d55; }
        select option { background: #111120; color: #c8d0e8; }
        a { transition: color .15s; }
        button { transition: opacity .1s; }
        button:hover:not(:disabled) { opacity: 0.85; }
      `}</style>
      <Dashboard token={token} onLogout={() => { localStorage.removeItem("freeholdy_token"); setToken(""); }} />
    </>
  );
}
