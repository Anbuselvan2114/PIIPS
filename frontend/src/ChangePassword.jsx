import { useState } from "react";
import { changePassword } from "./api";
import { Logo } from "./components";

// Shown instead of the app shell whenever the logged-in user still has
// must_change_password set (a freshly-created account, or one that just
// went through Forgot password) - the temporary password is known in
// plaintext by the admin/system, so the user is required to replace it
// before doing anything else.
export default function ChangePassword({ user, onDone, onLogout }) {
  const [current, setCurrent] = useState("");
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (pw !== pw2) { setError("New passwords do not match."); return; }
    setBusy(true);
    try {
      await changePassword(user.user_id, current, pw);
      onDone();
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
          <div className="auth-brand">Set your password</div>
        </div>
        <div className="auth-sub">
          For your security, you must set your own password before continuing.
        </div>

        <div className="field">
          <label className="label">Current (temporary) password</label>
          <div className="input-group">
            <span className="ico">🔒</span>
            <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoFocus placeholder="Temporary password" />
          </div>
        </div>
        <div className="field">
          <label className="label">New password</label>
          <div className="input-group">
            <span className="ico">🔒</span>
            <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder="New password" />
          </div>
          <div className="hint" style={{ marginTop: 4 }}>
            At least 5 characters, with an uppercase letter, a lowercase letter, a number, and a special character.
          </div>
        </div>
        <div className="field">
          <label className="label">Confirm new password</label>
          <div className="input-group">
            <span className="ico">🔒</span>
            <input type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} placeholder="Confirm new password" />
          </div>
        </div>

        {error && <div className="alert alert-danger">{error}</div>}

        <button type="submit" className="btn btn-primary btn-lg" style={{ width: "100%", marginTop: 18 }}
                disabled={busy || !current || !pw || !pw2}>
          {busy ? "Saving…" : "Set password and continue"}
        </button>

        <div style={{ textAlign: "center", marginTop: 14 }}>
          <button type="button" className="btn-link" onClick={onLogout}>Log out</button>
        </div>
      </form>
    </div>
  );
}
