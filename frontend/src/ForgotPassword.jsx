import { useState } from "react";
import { forgotPassword } from "./api";

export default function ForgotPassword({ onDone }) {
  const [value, setValue] = useState("");
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setMsg(null);
    setBusy(true);
    try {
      const r = await forgotPassword(value.trim());
      setMsg({ ok: true, text: r.message || "If that account exists, a new password has been emailed to it." });
    } catch (err) {
      // Even a server error shouldn't reveal whether the account exists.
      setMsg({ ok: true, text: "If that account exists, a new password has been emailed to it." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">Forgot password</div>
        <div className="auth-sub">
          Enter your username or email — we'll send a new temporary password
          to the address on file.
        </div>

        <div className="field">
          <label className="label">Username or email</label>
          <div className="input-group">
            <span className="ico">✉</span>
            <input value={value} onChange={(e) => setValue(e.target.value)} autoFocus placeholder="Username or email" />
          </div>
        </div>

        {msg && <div className={`alert ${msg.ok ? "alert-success" : "alert-danger"}`}>{msg.text}</div>}

        <button type="submit" className="btn btn-primary btn-lg" style={{ width: "100%", marginTop: 18 }}
                disabled={busy || !value.trim()}>
          {busy ? "Sending…" : "Send new password"}
        </button>

        <div style={{ textAlign: "center", marginTop: 14 }}>
          <button type="button" className="btn-link" onClick={onDone}>← Back to login</button>
        </div>
      </form>
    </div>
  );
}
