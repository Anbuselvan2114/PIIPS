import { useEffect, useRef, useState } from "react";
import { getInputFiles, uploadInputFiles, getTemplates } from "./api";
import { DataTable } from "./components";

export default function InputFiles({ user }) {
  const [templates, setTemplates] = useState([]);
  const [tpl, setTpl] = useState("");            // selected template key e.g. "PT\\Services_Chennai"
  const [folder, setFolder] = useState("");
  const [files, setFiles] = useState([]);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [results, setResults] = useState([]);
  const fileRef = useRef(null);

  const subpath = tpl ? tpl.replace(/\\/g, "/") : "";

  useEffect(() => {
    getTemplates()
      .then((r) => setTemplates(Object.keys(r.templates || {})))
      .catch((e) => setError(e.message));
  }, []);

  const load = (sp) =>
    getInputFiles(sp)
      .then((r) => { setFolder(r.folder || ""); setFiles(r.files || []); })
      .catch((e) => setError(e.message));

  // reload the listing when the selected template changes
  useEffect(() => {
    if (tpl) load(subpath); else { setFiles([]); setFolder(""); }
  }, [tpl]);

  const requireTpl = () => {
    if (!tpl) { setError("Select a template first."); setNotice(null); return false; }
    return true;
  };

  const upload = async (fileList) => {
    if (!requireTpl()) return;
    if (!fileList || fileList.length === 0) return;
    setError(null); setNotice(null); setResults([]); setBusy(true);
    try {
      const r = await uploadInputFiles(fileList, subpath, user?.user_id);
      const res = r.results || [];
      setResults(res);
      const n = (s) => res.filter((x) => x.status === s).length;
      const parts = [];
      if (n("loaded")) parts.push(`loaded ${n("loaded")} new`);
      if (n("already_initiated")) parts.push(`ignored ${n("already_initiated")} already initiated`);
      if (n("skipped")) parts.push(`skipped ${n("skipped")} unsupported`);
      setNotice(parts.join(", ") || "Nothing uploaded");
      await load(subpath);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ""; }
  };

  const STATUS_LABEL = {
    loaded: ["Loaded", "var(--success)"],
    already_initiated: ["Already initiated", "var(--warning, #d97706)"],
    skipped: ["Skipped", "var(--danger)"],
  };

  const onDrop = (e) => { e.preventDefault(); setDragOver(false); upload(e.dataTransfer.files); };

  return (
    <div className="page">
      <div className="card">
        <h3>File explorer</h3>
        <p className="hint">Pick a template, then drop invoice PDFs — they are copied into that template's folder under <code>Input</code>.</p>

        <div className="field" style={{ maxWidth: 420 }}>
          <label className="label">Template</label>
          <select value={tpl} onChange={(e) => { setTpl(e.target.value); setError(null); setNotice(null); }}>
            <option value="">— select a template —</option>
            {templates.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          {templates.length === 0 && <div className="hint" style={{ marginTop: 6 }}>No templates yet — create one under the Template menu.</div>}
        </div>

        <div className="row" style={{ alignItems: "center" }}>
          <button className="btn btn-primary" onClick={() => { if (requireTpl()) fileRef.current?.click(); }} disabled={busy || !tpl}>
            {busy ? "Copying…" : "Upload PDFs"}
          </button>
          <input ref={fileRef} type="file" multiple
                 accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp"
                 style={{ display: "none" }} onChange={(e) => upload(e.target.files)} />
        </div>

        <div className={`dropzone${dragOver ? " over" : ""}`}
             onDragOver={(e) => { e.preventDefault(); if (tpl) setDragOver(true); }}
             onDragLeave={() => setDragOver(false)} onDrop={onDrop}
             style={!tpl ? { opacity: .6 } : undefined}>
          {tpl ? <>Drag &amp; drop PDF files here to copy into <b>{tpl}</b></> : "Select a template above, then drop files here"}
        </div>

        {folder && <p className="hint mono" style={{ marginTop: 12 }}>Target folder: {folder}</p>}
        {error && <div className="alert alert-danger">{error}</div>}
        {notice && <div className="alert alert-success">{notice}</div>}
      </div>

      {results.length > 0 && (
        <div className="card">
          <h3>Upload result ({results.length})</h3>
          <p className="hint">New files are loaded; files already initiated (by anyone) are ignored.</p>
          <DataTable
            rows={results.map((r, i) => {
              const [label, color] = STATUS_LABEL[r.status] || [r.status, "inherit"];
              return { _key: `${r.file}-${i}`, file: r.file, status: label, _color: color, details: r.reason || "" };
            })}
            searchKeys={["file", "status", "details"]} empty="No results."
            columns={[
              { key: "file", label: "File" },
              { key: "status", label: "Status", render: (r) => <span style={{ fontWeight: 600, color: r._color }}>{r.status}</span> },
              { key: "details", label: "Details", render: (r) => <span className="muted">{r.details}</span> },
            ]} />
        </div>
      )}

      <div className="card">
        <div className="card-title-row">
          <h3>Files in {tpl || "template"} ({files.length})</h3>
          <button className="btn btn-subtle btn-sm" onClick={() => tpl && load(subpath)} disabled={!tpl}>Refresh</button>
        </div>
        {!tpl ? (
          <div className="empty">Select a template to see its files.</div>
        ) : (
          <DataTable
            rows={files.map((f) => ({ _key: f.name, name: f.name, size: f.size }))}
            searchKeys={["name"]} empty="No files in this template folder yet."
            columns={[
              { key: "name", label: "File" },
              { key: "size", label: "Size", render: (r) => `${(r.size / 1024).toFixed(1)} KB` },
            ]} />
        )}
      </div>
    </div>
  );
}
