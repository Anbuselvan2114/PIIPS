import { useEffect, useRef, useState } from "react";
import {
  startTraining, getStatus, getBackups, restoreBackup, getActiveJob,
  getTrainFiles, trainFileUrl,
} from "./api";
import { DataTable, confirmDialog } from "./components";

export default function Training({ user }) {
  const [job, setJob] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [backups, setBackups] = useState([]);
  const [notice, setNotice] = useState(null);
  const [current, setCurrent] = useState(null);
  const [files, setFiles] = useState([]);
  const [currentModel, setCurrentModel] = useState(null);
  const pollRef = useRef(null);

  const loadBackups = () => getBackups()
    .then((r) => { setBackups(r.backups || []); setCurrentModel(r.current || null); })
    .catch(() => {});
  const loadFiles = () => getTrainFiles().then((r) => setFiles(r.files || [])).catch(() => {});

  useEffect(() => {
    loadBackups();
    loadFiles();
    (async () => {
      try {
        const j = await getActiveJob("train");
        if (j && j.status) { setJob(j); if (j.status === "running" || j.status === "pending") { setRunning(true); poll(j.job_id); } }
      } catch { /* none */ }
    })();
    return () => clearInterval(pollRef.current);
  }, []);

  const onRestore = async (name) => {
    if (!(await confirmDialog(`Restore model from backup:\n${name}?`, { confirmLabel: "Restore" }))) return;
    setError(null); setNotice(null);
    try { const r = await restoreBackup(name, user?.user_id); setNotice(r.message || `Restored ${name}`); setCurrent(name); }
    catch (e) { setError(e.message); }
  };

  const poll = (jobId) => {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const s = await getStatus(jobId);
        setJob(s);
        if (s.status === "completed" || s.status === "failed") {
          clearInterval(pollRef.current); setRunning(false);
          if (s.status === "failed") setError(s.error || "Training failed");
          loadBackups(); loadFiles();
        }
      } catch (e) { clearInterval(pollRef.current); setRunning(false); setError(e.message); }
    }, 1000);
  };

  const onTrain = async () => {
    setError(null); setNotice(null); setJob(null);
    try { const { job_id } = await startTraining(user?.user_id); setRunning(true); poll(job_id); }
    catch (e) { setError(e.message); }
  };

  const percent = job?.percent ?? 0;

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>Train formats</h3>
          <div className="spacer" />
          <button className="btn btn-primary btn-lg" onClick={onTrain} disabled={running}>
            {running ? "Training…" : "🧠  Train"}
          </button>
        </div>
        <p className="hint" style={{ margin: 0 }}>
          Place new invoice PDFs in the <code>New_Format</code> folder, then Train.
          Each new layout is learned by its seller GSTIN and merged into the model; known ones are skipped.
        </p>

        {job && (
          <div style={{ marginTop: 18 }}>
            <div className="progress-meta">
              <span>{job.processed}/{job.total}{job.current_file ? ` — ${job.current_file}` : ""}</span>
              <span>{percent}%</span>
            </div>
            <div className="progress">
              <div className={`progress-bar${job.status === "failed" ? " failed" : ""}`} style={{ width: `${percent}%` }} />
            </div>
            {job.status === "completed" && (
              <div style={{ marginTop: 14 }}>
                <DataTable
                  rows={(job.results || []).map((r, i) => ({
                    _key: `${r.file}-${i}`, file: r.file,
                    result: r.format_status,
                  }))}
                  searchKeys={["file", "result"]} empty="No results."
                  columns={[
                    { key: "file", label: "File" },
                    { key: "result", label: "Result" },
                  ]} />
              </div>
            )}
          </div>
        )}
        {error && <div className="alert alert-danger">{error}</div>}
        {notice && <div className="alert alert-success">{notice}</div>}
      </div>

      <div className="card">
        <div className="card-title-row">
          <h3>Files to train ({files.length})</h3>
          <div className="spacer" />
          <button className="btn btn-subtle btn-sm" onClick={loadFiles}>Refresh</button>
        </div>
        <p className="hint">Files currently in the <code>New_Format</code> folder. Click a file name to view it.</p>
        <DataTable
          rows={files.map((f) => ({ _key: f.name, name: f.name, size: f.size }))}
          searchKeys={["name"]} empty="No files in the New_Format folder."
          columns={[
            { key: "name", label: "File",
              render: (r) => (
                <a href={trainFileUrl(r.name)} target="_blank" rel="noreferrer"
                   className="btn-link" title="View file">{r.name}</a>
              ) },
            { key: "size", label: "Size", render: (r) => `${(r.size / 1024).toFixed(1)} KB` },
          ]} />
      </div>

      <div className="card">
        <h3>Model backups ({backups.length})</h3>
        <p className="hint">The current (live) model is shown first; below it are the automatic backups saved before each Train. Restore an earlier one if a run goes wrong.</p>
        <DataTable
          rows={[
            ...(currentModel ? [{
              _key: "__current__", _current: true,
              name: "format_model.json (current model)",
              modified: currentModel.modified, formats: currentModel.formats,
            }] : []),
            ...backups.map((b) => ({ _key: b.name, name: b.name, modified: b.modified, formats: b.formats })),
          ]}
          searchKeys={["name", "modified"]} empty="No model yet."
          columns={[
            { key: "name", label: "Backup", render: (r) => <span className="mono">{r.name}</span> },
            { key: "modified", label: "Saved" },
            { key: "formats", label: "Formats" },
            { key: "_action", label: "", sortable: false,
              render: (r) => (r._current || r.name === current
                ? <span className="badge badge-success">current model</span>
                : <button className="btn btn-ghost btn-sm" onClick={() => onRestore(r.name)}>Restore</button>) },
          ]} />
      </div>
    </div>
  );
}
