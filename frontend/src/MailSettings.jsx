import { useEffect, useState } from "react";
import { getMailSettings, saveMailSettings } from "./api";

export default function MailSettings({ user }) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [passwordSet, setPasswordSet] = useState(false);
  const [smtpHost, setSmtpHost] = useState("");
  const [smtpPort, setSmtpPort] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    getMailSettings(user?.user_id)
      .then((c) => {
        setUsername(c.username || "");
        setEmail(c.email || "");
        setSmtpHost(c.smtp_host || "");
        setSmtpPort(c.smtp_port ?? "");
        setPasswordSet(!!c.password_set);
      })
      .catch((e) => setMessage({ ok: false, text: e.message }))
      .finally(() => setLoading(false));
  }, [user]);

  const onSave = async () => {
    setSaving(true);
    setMessage(null);
    try {
      await saveMailSettings({
        user_id: user?.user_id,
        username: username.trim(),
        email: email.trim(),
        smtp_host: smtpHost.trim(),
        smtp_port: Number(smtpPort),
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

  const canSave = username.trim() && email.trim() && smtpHost.trim() && smtpPort &&
    (password !== "" || passwordSet);

  return (
    <div className="page">
      <div className="card">
        <h3>Mail server setting</h3>
        <p className="hint">
          SMTP account PIIPS sends welcome, password-reset, and account-status
          emails from. The password is stored <b>encrypted</b> on the server
          and is never shown here in full. Saving first <b>tests</b> the
          connection.
        </p>

        <div className="row" style={{ flexDirection: "column", alignItems: "stretch", maxWidth: 420, gap: 14 }}>
          <div className="field">
            <label className="label">Display name</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="SF APP" />
          </div>
          <div className="field">
            <label className="label">Email address</label>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                   placeholder="sfapp@precisionit.co.in" autoComplete="off" />
          </div>
          <div className="field">
            <label className="label">SMTP host</label>
            <input value={smtpHost} onChange={(e) => setSmtpHost(e.target.value)}
                   placeholder="mail.precisionit.co.in" />
          </div>
          <div className="field">
            <label className="label">SMTP port</label>
            <input type="number" value={smtpPort} onChange={(e) => setSmtpPort(e.target.value)} placeholder="587" />
          </div>
          <div className="field">
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
