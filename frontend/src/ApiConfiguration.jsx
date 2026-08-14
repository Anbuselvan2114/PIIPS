import { useEffect, useState } from "react";
import { getApiConfig, saveApiConfig } from "./api";

export default function ApiConfiguration() {
  const [sfApiUrl, setSfApiUrl] = useState("");
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    getApiConfig()
      .then((c) => setSfApiUrl(c.sf_api_url || ""))
      .catch((e) => setMessage({ ok: false, text: e.message }));
  }, []);

  const onSave = async () => {
    setLoading(true); setMessage(null);
    try {
      await saveApiConfig(sfApiUrl.trim());
      setMessage({ ok: true, text: "API configuration saved." });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page">
      <div className="card">
        <h3>API configuration</h3>

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
    </div>
  );
}
