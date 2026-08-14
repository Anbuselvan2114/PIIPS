import { useState } from "react";
import { login } from "./api";
import { Logo } from "./components";

export default function Login({ onSuccess, onForgot }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      onSuccess(await login(username.trim(), password));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-wrap">
      <form className="auth-card" onSubmit={submit}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 4 }}>
          <Logo size={40} />
          <div className="auth-brand">PIIPS</div>
        </div>
        <div className="auth-sub">Precision Intelligence Invoice Processing Suite</div>

        <div className="field">
          <label className="label">User Name</label>
          <div className="input-group">
            <span className="ico">👤</span>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus placeholder="User name" />
          </div>
        </div>
        <div className="field">
          <label className="label">Password</label>
          <div className="input-group">
            <span className="ico">🔒</span>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" />
          </div>
        </div>

        {error && <div className="alert alert-danger">{error}</div>}

        <button type="submit" className="btn btn-primary btn-lg" style={{ width: "100%", marginTop: 18 }}
                disabled={busy || !username.trim() || !password}>
          {busy ? "Signing in…" : "Login"}
        </button>

        <div style={{ textAlign: "center", marginTop: 14 }}>
          <button type="button" className="btn-link" onClick={onForgot}>Forgot password?</button>
        </div>
      </form>
    </div>
  );
}
