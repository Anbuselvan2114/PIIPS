import { useEffect, useRef, useState } from "react";
import { getDeployServers, saveDeployServer, publishDeploy } from "./api";

const BLANK = { server_host: "", folder_path: "", site_name: "", service_name: "PIIPS_Backend", port: "" };

// Publish runs several minutes of real work (git, npm install/build,
// robocopy, remote restart) behind one blocking request - the backend has
// no step-by-step progress API, so this is a timed approximation of where
// it's likely at, not a true percentage. It always jumps to 100% (or the
// failed state) the moment the real request actually resolves.
const PUBLISH_STAGES = [
  { label: "Checking out branch…", percent: 8 },
  { label: "Pulling latest…", percent: 18 },
  { label: "Installing frontend dependencies…", percent: 35 },
  { label: "Building frontend…", percent: 55 },
  { label: "Copying files to server…", percent: 80 },
  { label: "Restarting service & health check…", percent: 92 },
];
const PUBLISH_STAGE_MS = 6000;

function EnvironmentCard({ environment, label, userId, saved, onSaved }) {
  const [form, setForm] = useState(BLANK);
  const [saving, setSaving] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [message, setMessage] = useState(null);
  const [log, setLog] = useState(null);
  const [stageIdx, setStageIdx] = useState(0);
  const [failed, setFailed] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => {
    if (saved) {
      setForm({
        server_host: saved.ServerHost || "",
        folder_path: saved.FolderPath || "",
        site_name: saved.SiteName || "",
        service_name: saved.ServiceName || "PIIPS_Backend",
        port: saved.Port || "",
      });
    }
  }, [saved]);

  const setField = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  const onSave = async () => {
    setSaving(true); setMessage(null);
    try {
      await saveDeployServer({
        user_id: userId,
        environment,
        server_host: form.server_host.trim(),
        folder_path: form.folder_path.trim(),
        site_name: form.site_name.trim(),
        service_name: form.service_name.trim() || "PIIPS_Backend",
        port: form.port ? Number(form.port) : null,
      });
      setMessage({ ok: true, text: "Server details saved." });
      onSaved();
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setSaving(false);
    }
  };

  useEffect(() => () => clearInterval(timerRef.current), []);

  const onPublish = async () => {
    setPublishing(true); setMessage(null); setLog(null); setFailed(false); setStageIdx(0);
    timerRef.current = setInterval(() => {
      setStageIdx((i) => Math.min(i + 1, PUBLISH_STAGES.length - 1));
    }, PUBLISH_STAGE_MS);

    try {
      const r = await publishDeploy(environment, userId);
      clearInterval(timerRef.current);
      setStageIdx(PUBLISH_STAGES.length - 1);
      setLog(r.log);
      setMessage({ ok: true, text: "Publish complete." });
      onSaved();
    } catch (e) {
      clearInterval(timerRef.current);
      setFailed(true);
      setLog(e.message);
      setMessage({ ok: false, text: "Publish failed." });
    } finally {
      setPublishing(false);
    }
  };

  const stagePercent = failed ? 100 : PUBLISH_STAGES[stageIdx].percent;

  const registered = Boolean(saved);

  return (
    <div className="card">
      <h3>{label}</h3>

      <div className="field">
        <label className="label">Server host / IP</label>
        <input value={form.server_host} onChange={setField("server_host")} placeholder="10.0.1.213" />
      </div>

      <div className="field">
        <label className="label">Folder path (UNC)</label>
        <input value={form.folder_path} onChange={setField("folder_path")}
               placeholder={`\\\\10.0.1.213\\D$\\PIIPS_${environment.toUpperCase()}`} />
      </div>

      <div className="field">
        <label className="label">IIS site name</label>
        <input value={form.site_name} onChange={setField("site_name")} placeholder={`PIIPS_AI_${environment.toUpperCase()}`} />
      </div>

      <div className="field">
        <label className="label">Windows service name</label>
        <input value={form.service_name} onChange={setField("service_name")} placeholder="PIIPS_Backend" />
      </div>

      <div className="field">
        <label className="label">Health-check port</label>
        <input value={form.port} onChange={setField("port")} placeholder="8000" />
      </div>

      <button className="btn btn-subtle" onClick={onSave} disabled={saving}>
        {saving ? "Saving…" : "Save server details"}
      </button>

      <button className="btn btn-primary" onClick={onPublish}
              disabled={!registered || publishing} style={{ marginLeft: 8 }}>
        {publishing ? "Publishing…" : `Publish to ${label}`}
      </button>

      {publishing && (
        <div style={{ marginTop: 12 }}>
          <div className="progress-meta">
            <span>{PUBLISH_STAGES[stageIdx].label}</span>
            <span>{stagePercent}%</span>
          </div>
          <div className="progress"><div className="progress-bar" style={{ width: `${stagePercent}%` }} /></div>
        </div>
      )}

      {saved?.LastPublishedDatetime && (
        <div className="hint" style={{ marginTop: 8 }}>
          Last published {saved.LastPublishedDatetime} by {saved.LastPublishedBy || "?"} —{" "}
          <strong>{saved.LastPublishStatus}</strong>
        </div>
      )}

      {message && (
        <div className={`alert ${message.ok ? "alert-success" : "alert-danger"}`}>
          {message.text}
        </div>
      )}

      {log && (
        <pre style={{ maxHeight: 300, overflow: "auto", fontSize: 12, marginTop: 8 }}>{log}</pre>
      )}
    </div>
  );
}

export default function Publish({ user }) {
  const [servers, setServers] = useState(null);
  const [error, setError] = useState(null);

  const refresh = () => {
    getDeployServers(user?.user_id)
      .then((r) => setServers(r.servers))
      .catch((e) => setError(e.message));
  };

  useEffect(refresh, []);

  const byEnv = (env) => (servers || []).find((s) => s.Environment === env);

  return (
    <div className="page">
      {error && <div className="alert alert-danger">{error}</div>}
      <EnvironmentCard environment="uat" label="UAT" userId={user?.user_id} saved={byEnv("uat")} onSaved={refresh} />
      <div style={{ height: 16 }} />
      <EnvironmentCard environment="live" label="Live" userId={user?.user_id} saved={byEnv("live")} onSaved={refresh} />
    </div>
  );
}
