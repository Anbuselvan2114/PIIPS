import { useEffect, useState } from "react";
import { getDbConfig, saveDbConfig } from "./api";

export default function DatabaseConfig({ user }) {
  const [server, setServer] = useState("");
  const [database, setDatabase] = useState("");
  const [auth, setAuth] = useState("sql"); // sql | windows
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordSet, setPasswordSet] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    getDbConfig(user?.user_id)
      .then((c) => {
        setServer(c.server || "");
        setDatabase(c.database || "");
        setAuth(c.auth || "sql");
        setUsername(c.username || "");
        setPasswordSet(!!c.password_set);
      })
      .catch((e) => setMessage({ ok: false, text: e.message }))
      .finally(() => setLoading(false));
  }, [user]);

  const onSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      await saveDbConfig({
        user_id: user?.user_id,
        server: server.trim(),
        database: database.trim(),
        auth,
        username: username.trim(),
        // blank => keep the existing password on the server
        password: password === "" ? null : password,
      });
      setPassword("");
      setPasswordSet(true);
      setMessage({ ok: true, text: "Connection tested and saved." });
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="page"><div className="card">Loading…</div></div>;

  const sqlAuth = auth === "sql";
  const canSave =
    server.trim() &&
    database.trim() &&
    (!sqlAuth || (username.trim() && (password !== "" || passwordSet)));

  return (
    <div className="page">
      <div className="card">
        <h3>Database configuration</h3>
        <p className="hint">
          Point PIIPS at a SQL Server database. The connection string is stored
          <b> encrypted</b> on the server and is never shown here in full.
          Saving first <b>tests</b> the connection. Changing this affects the
          whole application — an unreachable database will lock everyone out.
        </p>

        <div className="row">
          <div className="field" style={{ flex: 1, minWidth: 220 }}>
            <label className="label">Server</label>
            <input value={server} onChange={(e) => setServer(e.target.value)}
                   placeholder="10.0.1.213  or  SERVER\\SQLEXPRESS" />
          </div>
          <div className="field" style={{ flex: 1, minWidth: 200 }}>
            <label className="label">Database</label>
            <input value={database} onChange={(e) => setDatabase(e.target.value)}
                   placeholder="PIIPS" />
          </div>
        </div>

        <div className="field">
          <label className="label">Authentication</label>
          <div className="row" style={{ gap: 18, alignItems: "center" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input type="radio" name="auth" checked={sqlAuth}
                     onChange={() => setAuth("sql")} />
              SQL Server login
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input type="radio" name="auth" checked={!sqlAuth}
                     onChange={() => setAuth("windows")} />
              Windows (Integrated)
            </label>
          </div>
        </div>

        {sqlAuth && (
          <div className="row">
            <div className="field" style={{ flex: 1, minWidth: 200 }}>
              <label className="label">Username</label>
              <input value={username} onChange={(e) => setUsername(e.target.value)}
                     placeholder="sa" autoComplete="off" />
            </div>
            <div className="field" style={{ flex: 1, minWidth: 200 }}>
              <label className="label">Password</label>
              <input type="password" value={password} autoComplete="new-password"
                     onChange={(e) => setPassword(e.target.value)}
                     placeholder={passwordSet ? "•••••• (unchanged)" : "password"} />
              {passwordSet && (
                <div className="hint" style={{ marginTop: 4 }}>
                  Leave blank to keep the current password.
                </div>
              )}
            </div>
          </div>
        )}

        <div style={{ marginTop: 16 }}>
          <button className="btn btn-primary" onClick={onSave} disabled={saving || !canSave}>
            {saving ? "Testing & saving…" : "Test & Save"}
          </button>
          {message && (
            <span style={{ marginLeft: 14, fontWeight: 600,
                           color: message.ok ? "var(--success)" : "var(--danger)" }}>
              {message.text}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
