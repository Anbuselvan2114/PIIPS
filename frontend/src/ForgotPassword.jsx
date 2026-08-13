import { useState } from "react";
import { resetPassword } from "./api";

export default function ForgotPassword({ onDone }) {
  const [username, setUsername] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setMsg(null);
    if (pw !== pw2) { setMsg({ ok: false, text: "Passwords do not match." }); return; }
    setBusy(true);
    try {
      await resetPassword(username.trim(), pw);
      setMsg({ ok: true, text: "Password updated. You can now log in." });
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">Reset password</div>
        <div className="auth-sub">Enter your username and a new password.</div>

        <div className="field">
          <label className="label">User Name</label>
          <div className="input-group">
            <span className="ico">👤</span>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus placeholder="User name" />
          </div>
        </div>
        <div className="field">
          <label className="label">New password</label>
          <div className="input-group">
            <span className="ico">🔒</span>
            <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder="New password" />
          </div>
        </div>
        <div className="field">
          <label className="label">Confirm new password</label>
          <div className="input-group">
            <span className="ico">🔒</span>
            <input type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} placeholder="Confirm new password" />
          </div>
        </div>

        {msg && <div className={`alert ${msg.ok ? "alert-success" : "alert-danger"}`}>{msg.text}</div>}

        <button type="submit" className="btn btn-primary btn-lg" style={{ width: "100%", marginTop: 18 }}
                disabled={busy || !username.trim() || !pw}>
          {busy ? "Updating…" : "Update password"}
        </button>

        <div style={{ textAlign: "center", marginTop: 14 }}>
          <button type="button" className="btn-link" onClick={onDone}>← Back to login</button>
        </div>
      </form>
    </div>
  );
}
