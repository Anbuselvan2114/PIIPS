import { useEffect, useState } from "react";
import { getApiConfig, saveApiConfig, getConfigHistory } from "./api";
import { Modal } from "./components";

export default function ApiConfiguration({ user }) {
  const [sfApiUrl, setSfApiUrl] = useState("");
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [history, setHistory] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const isSuperAdmin = ["super admin", "developer"].includes((user?.user_type || "").toLowerCase());

  useEffect(() => {
    getApiConfig()
      .then((c) => setSfApiUrl(c.sf_api_url || ""))
      .catch((e) => setMessage({ ok: false, text: e.message }));
  }, []);

  const onSave = async () => {
    setLoading(true); setMessage(null);
    try {
      await saveApiConfig(sfApiUrl.trim(), user?.user_id);
      setMessage({ ok: true, text: "API configuration saved." });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setLoading(false);
    }
  };

  const openHistory = async () => {
    setHistoryLoading(true);
    try {
      const r = await getConfigHistory("API_CONFIG_CHANGED", user?.user_id);
      setHistory(r.events || []);
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setHistoryLoading(false);
    }
  };

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>API configuration</h3>
          <div style={{ flex: 1 }} />
          {isSuperAdmin && (
            <button className="btn btn-subtle btn-sm" disabled={historyLoading} onClick={openHistory}>
              {historyLoading ? "Loading…" : "History"}
            </button>
          )}
        </div>

        <div className="field">
          <label className="label">Service First API URL</label>
          <div className="hint">
            Base URL of the Service First / NAV backend used to fetch reservation
            (spare-purchase) and HSN details, e.g. <code>http://10.0.1.10:8080</code>.
            Leave blank to skip the lookup (invoices process without reservation).
          </div>
          <div className="path-field">
            <span className="ico">🔗</span>
            <input value={sfApiUrl} onChange={(e) => setSfApiUrl(e.target.value)}
                   placeholder="http://sfuat.precisionit.co.in/sft" />
          </div>
        </div>

        <button className="btn btn-primary" onClick={onSave} disabled={loading}>
          {loading ? "Saving…" : "Save"}
        </button>

        {message && (
          <div className={`alert ${message.ok ? "alert-success" : "alert-danger"}`}>
            {message.text}
          </div>
        )}
      </div>

      {history && (
        <Modal title="API Configuration History" onClose={() => setHistory(null)} width={640}>
          {history.length === 0 ? (
            <div className="empty">No changes recorded yet.</div>
          ) : (
            <div className="timeline">
              {history.map((ev) => (
                <div key={ev.Id} className="timeline-item">
                  <div className="timeline-dot" />
                  <div className="timeline-content">
                    <div className="timeline-header">
                      <span className="timeline-action">{ev.Action}</span>
                      <span className="timeline-time">{ev.EventDatetime}</span>
                    </div>
                    <div className="timeline-who">{ev.UserName || "Unknown"}</div>
                    {ev.Detail && <div className="timeline-detail">{ev.Detail}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
