import { useEffect, useState } from "react";
import { getConfig, saveConfig } from "./api";

export default function Configuration() {
  const [folderPath, setFolderPath] = useState("");
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    getConfig()
      .then((cfg) => { setFolderPath(cfg.folder_path || ""); })
      .catch((e) => setMessage({ ok: false, text: e.message }));
  }, []);

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
    </div>
  );
}
