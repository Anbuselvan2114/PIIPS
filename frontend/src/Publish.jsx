import { useEffect, useRef, useState } from "react";
import { getPublishConfig, savePublishConfig, publishDeploy } from "./api";

// Publish runs several minutes of real work (git, npm install/build,
// robocopy) behind one blocking request - the backend has no step-by-step
// progress API, so this is a timed approximation of where it's likely at,
// not a true percentage. It always jumps to 100% (or the failed state) the
// moment the real request actually resolves.
const PUBLISH_STAGES = [
  { label: "Checking out branch…", percent: 8 },
  { label: "Pulling latest…", percent: 18 },
  { label: "Installing frontend dependencies…", percent: 35 },
  { label: "Building frontend…", percent: 55 },
  { label: "Copying files…", percent: 85 },
];
const PUBLISH_STAGE_MS = 6000;

export default function Publish({ user }) {
  const [root, setRoot] = useState("");
  const [environment, setEnvironment] = useState("uat");
  const [status, setStatus] = useState({});
  const [saving, setSaving] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [message, setMessage] = useState(null);
  const [log, setLog] = useState(null);
  const [stageIdx, setStageIdx] = useState(0);
  const [failed, setFailed] = useState(false);
  const timerRef = useRef(null);

  const refresh = () => {
    getPublishConfig(user?.user_id)
      .then((r) => { setRoot(r.publish_root || ""); setStatus(r.status || {}); })
      .catch((e) => setMessage({ ok: false, text: e.message }));
  };

  useEffect(refresh, []);
  useEffect(() => () => clearInterval(timerRef.current), []);

  const onSaveRoot = async () => {
    setSaving(true); setMessage(null);
    try {
      await savePublishConfig(root.trim(), user?.user_id);
      setMessage({ ok: true, text: "Root path saved." });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setSaving(false);
    }
  };

  const onPublish = async () => {
    setPublishing(true); setMessage(null); setLog(null); setFailed(false); setStageIdx(0);
    timerRef.current = setInterval(() => {
      setStageIdx((i) => Math.min(i + 1, PUBLISH_STAGES.length - 1));
    }, PUBLISH_STAGE_MS);

    try {
      const r = await publishDeploy(environment, user?.user_id);
      clearInterval(timerRef.current);
      setStageIdx(PUBLISH_STAGES.length - 1);
      setLog(r.log);
      setMessage({ ok: true, text: "Publish complete." });
      refresh();
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
  const envStatus = status[environment];

  return (
    <div className="page">
      <div className="card">
        <h3>Publish</h3>

        <div className="field">
          <label className="label">Root path (local)</label>
          <div className="hint">
            Where a published build is staged on this machine, e.g.{" "}
            <code>D:\WorkSpace\Projects\Python\PIIPS_Published</code>. Each environment
            gets its own subfolder (<code>\uat</code>, <code>\live</code>) — move it onto
            the real server by hand from there.
          </div>
          <div className="path-field">
            <span className="ico">📁</span>
            <input value={root} onChange={(e) => setRoot(e.target.value)}
                   placeholder={"D:\\WorkSpace\\Projects\\Python\\PIIPS_Published"} />
          </div>
        </div>
        <button className="btn btn-subtle" onClick={onSaveRoot} disabled={saving}>
          {saving ? "Saving…" : "Save root path"}
        </button>

        <div className="field" style={{ marginTop: 18 }}>
          <label className="label">Environment</label>
          <select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
            <option value="uat">UAT</option>
            <option value="live">Live</option>
          </select>
        </div>

        <button className="btn btn-primary" onClick={onPublish} disabled={publishing}>
          {publishing ? "Publishing…" : `Publish to ${environment.toUpperCase()}`}
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

        {envStatus && (
          <div className="hint" style={{ marginTop: 8 }}>
            Last published {envStatus.at} by {envStatus.by} — <strong>{envStatus.status}</strong>
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
    </div>
  );
}
