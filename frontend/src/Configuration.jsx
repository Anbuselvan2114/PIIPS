import { useEffect, useState } from "react";
import { getConfig, saveConfig, setScannedPdfsEnabled } from "./api";

export default function Configuration({ user }) {
  const [folderPath, setFolderPath] = useState("");
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(false);

  const isSuperAdmin = ["super admin", "developer"].includes((user?.user_type || "").toLowerCase());
  const [scannedPdfsEnabled, setScannedPdfsEnabledState] = useState(false);
  const [scannedPdfsBusy, setScannedPdfsBusy] = useState(false);
  const [scannedPdfsMsg, setScannedPdfsMsg] = useState(null);

  useEffect(() => {
    getConfig()
      .then((cfg) => {
        setFolderPath(cfg.folder_path || "");
        setScannedPdfsEnabledState(!!cfg.allow_scanned_pdfs);
      })
      .catch((e) => setMessage({ ok: false, text: e.message }));
  }, []);

  const onToggleScannedPdfs = async () => {
    const next = !scannedPdfsEnabled;
    setScannedPdfsBusy(true); setScannedPdfsMsg(null);
    try {
      await setScannedPdfsEnabled(next, user?.user_id);
      setScannedPdfsEnabledState(next);
    } catch (e) {
      setScannedPdfsMsg({ ok: false, text: e.message });
    } finally {
      setScannedPdfsBusy(false);
    }
  };

  const onSave = async () => {
    setLoading(true); setMessage(null);
    try {
      const cfg = await saveConfig(folderPath.trim());
      setMessage({ ok: true, text: "Configuration saved.", folders: cfg.folders });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page">
      <div className="card">
        <h3>Folder</h3>

        <div className="field">
          <label className="label">Folder Path</label>
          <div className="hint">
            The working folder. Must be a folder on the <b>server machine</b> —
            not a network share or a mapped network drive. On save, the
            <b> Input</b> folder (with a subfolder for each template) and
            <b> New_Format</b> are created here, and extracted output is written here.
          </div>
          <div className="path-field">
            <span className="ico">📁</span>
            <input value={folderPath} onChange={(e) => setFolderPath(e.target.value)}
                   placeholder="D:\\PIIPS" />
          </div>
        </div>

        <button className="btn btn-primary" onClick={onSave} disabled={loading || !folderPath.trim()}>
          {loading ? "Saving…" : "Save"}
        </button>

        {message && (
          <div className={`alert ${message.ok ? "alert-success" : "alert-danger"}`}>
            {message.text}
            {message.ok && message.folders && (
              <div className="mono" style={{ marginTop: 8, fontSize: 12 }}>
                <div>Input: {message.folders.input}</div>
                <div>New_Format: {message.folders.new_format}</div>
                <div>Output: {message.folders.output}</div>
              </div>
            )}
          </div>
        )}
      </div>

      {isSuperAdmin && (
        <div className="card">
          <h3>Scanned / Photocopied PDFs</h3>
          <p className="hint">
            By default, an invoice (Part or Service) with no embedded text
            layer (a scan or photocopy) is rejected as unsupported rather
            than OCR-read, since a first-time vendor's layout read purely
            off a scanned image is unreliable. Turning this on lets a
            scanned or photocopied invoice of either type be OCR-extracted
            instead — safe in particular for a vendor whose layout is
            already trained, since it'll simply match (or fail to match)
            the same trained format as any other invoice. A genuine
            handheld photo is still rejected either way.
          </p>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
            <input type="checkbox" checked={scannedPdfsEnabled} disabled={scannedPdfsBusy}
                   onChange={onToggleScannedPdfs} />
            <span>Allow scanned/photocopied invoices to be processed</span>
          </label>
          {scannedPdfsMsg && (
            <div className={`alert ${scannedPdfsMsg.ok ? "alert-success" : "alert-danger"}`}
                 style={{ marginTop: 10 }}>
              {scannedPdfsMsg.text}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
