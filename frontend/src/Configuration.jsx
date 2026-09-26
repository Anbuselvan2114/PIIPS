import { useEffect, useState } from "react";
import { getConfig, saveConfig, setScannedPdfsEnabled } from "./api";

export default function Configuration({ user }) {
  const [folderPath, setFolderPath] = useState("");
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(false);

  const isSuperAdmin = ["super admin", "developer"].includes((user?.user_type || "").toLowerCase());
  const [scannedPdfsEnabled, setScannedPdfsEnabledState] = useState({ PART: false, SERVICE: false });
  const [scannedPdfsBusy, setScannedPdfsBusy] = useState({ PART: false, SERVICE: false });
  const [scannedPdfsMsg, setScannedPdfsMsg] = useState({ PART: null, SERVICE: null });

  useEffect(() => {
    getConfig()
      .then((cfg) => {
        setFolderPath(cfg.folder_path || "");
        setScannedPdfsEnabledState({
          PART: !!cfg.allow_scanned_pdfs_part,
          SERVICE: !!cfg.allow_scanned_pdfs_service,
        });
      })
      .catch((e) => setMessage({ ok: false, text: e.message }));
  }, []);

  const onToggleScannedPdfs = async (invoiceType) => {
    const next = !scannedPdfsEnabled[invoiceType];
    setScannedPdfsBusy((prev) => ({ ...prev, [invoiceType]: true }));
    setScannedPdfsMsg((prev) => ({ ...prev, [invoiceType]: null }));
    try {
      await setScannedPdfsEnabled(next, invoiceType, user?.user_id);
      setScannedPdfsEnabledState((prev) => ({ ...prev, [invoiceType]: next }));
    } catch (e) {
      setScannedPdfsMsg((prev) => ({ ...prev, [invoiceType]: { ok: false, text: e.message } }));
    } finally {
      setScannedPdfsBusy((prev) => ({ ...prev, [invoiceType]: false }));
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
            By default, an invoice with no embedded text layer (a scan or
            photocopy) is rejected as unsupported rather than OCR-read,
            since a first-time vendor's layout read purely off a scanned
            image is unreliable. Turning this on for a type lets a scanned
            or photocopied invoice of that type be OCR-extracted instead —
            safe in particular for a vendor whose layout is already
            trained, since it'll simply match (or fail to match) the same
            trained format as any other invoice. A genuine handheld photo
            is still rejected either way. Each invoice type has its own
            toggle, so turning it on for one doesn't affect the other.
          </p>
          {["PART", "SERVICE"].map((invoiceType) => (
            <div key={invoiceType} style={{ marginTop: 10 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
                <input type="checkbox" checked={scannedPdfsEnabled[invoiceType]}
                       disabled={scannedPdfsBusy[invoiceType]}
                       onChange={() => onToggleScannedPdfs(invoiceType)} />
                <span>Allow scanned/photocopied {invoiceType} invoices to be processed</span>
              </label>
              {scannedPdfsMsg[invoiceType] && (
                <div className={`alert ${scannedPdfsMsg[invoiceType].ok ? "alert-success" : "alert-danger"}`}
                     style={{ marginTop: 10 }}>
                  {scannedPdfsMsg[invoiceType].text}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
