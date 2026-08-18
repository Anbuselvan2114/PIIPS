import { useEffect, useState } from "react";
import { getUsers, createUser, setUserActive, adminResetPassword, changePassword } from "./api";
import { DataTable } from "./components";

// Fields stacked vertically instead of the shared ".row"'s default
// side-by-side layout — scoped here so other pages that reuse ".row"
// (DatabaseConfig, ApiConfiguration, ...) keep their own layout.
const stackStyle = { flexDirection: "column", alignItems: "stretch", maxWidth: 420, gap: 14 };

export default function UserManagement({ user }) {
  const [users, setUsers] = useState([]);
  const [types, setTypes] = useState([]);
  const [form, setForm] = useState({ username: "", email: "", user_type_id: "" });
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(true);
  const [ownPw, setOwnPw] = useState({ current: "", next: "", confirm: "" });
  const [ownPwMsg, setOwnPwMsg] = useState(null);
  const [ownPwBusy, setOwnPwBusy] = useState(false);

  const isSuperAdmin = ["super admin", "developer"].includes((user?.user_type || "").toLowerCase());

  const refresh = () =>
    getUsers()
      .then((r) => {
        setUsers(r.users || []); setTypes(r.user_types || []);
        setForm((f) => ({ ...f, user_type_id: f.user_type_id || (r.user_types?.[0]?.id ?? "") }));
      })
      .catch((e) => setMessage({ ok: false, text: e.message }))
      .finally(() => setLoading(false));

  useEffect(() => { refresh(); }, []);

  const emailNote = (r) =>
    r.email_sent === false
      ? ` (could not send the notification email: ${r.email_error})`
      : "";

  const onCreate = async () => {
    setMessage(null);
    try {
      const r = await createUser(form.username.trim(), form.email.trim(), Number(form.user_type_id), user?.user_id);
      setMessage({ ok: true, text: `User "${form.username.trim()}" created. A temporary password was emailed to them.${emailNote(r)}` });
      setForm({ username: "", email: "", user_type_id: types[0]?.id ?? "" });
      refresh();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  const toggle = async (u) => {
    const action = u.IsActive ? "inactivate" : "give access to";
    if (!window.confirm(`Are you sure you want to ${action} "${u.UserName}"?`)) return;
    try {
      const r = await setUserActive(u.UserId, !u.IsActive, user?.user_id);
      setMessage({ ok: true, text: `"${u.UserName}" ${u.IsActive ? "deactivated" : "activated"}.${emailNote(r)}` });
      refresh();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  const resetPw = async (u) => {
    if (!window.confirm(`Send "${u.UserName}" a new auto-generated password by email?`)) return;
    try {
      const r = await adminResetPassword(user?.user_id, u.UserId);
      setMessage({ ok: true, text: `New password emailed to "${u.UserName}".${emailNote(r)}` });
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  // An Admin may reset anyone except a Super Admin; a Super Admin may reset anyone.
  const canResetFor = (u) => isSuperAdmin || (u.UserTypeName || "").toLowerCase() !== "super admin";

  const onChangeOwnPw = async (e) => {
    e.preventDefault();
    setOwnPwMsg(null);
    if (ownPw.next !== ownPw.confirm) { setOwnPwMsg({ ok: false, text: "New passwords do not match." }); return; }
    setOwnPwBusy(true);
    try {
      await changePassword(user?.user_id, ownPw.current, ownPw.next);
      setOwnPwMsg({ ok: true, text: "Password updated." });
      setOwnPw({ current: "", next: "", confirm: "" });
    } catch (e) { setOwnPwMsg({ ok: false, text: e.message }); }
    finally { setOwnPwBusy(false); }
  };

  if (loading) return <div className="page"><div className="card">Loading…</div></div>;

  return (
    <div className="page">
      <div className="card">
        <h3>My password</h3>
        <p className="hint">Change the password for your own account ({user?.username}).</p>
        <form className="row" style={stackStyle} onSubmit={onChangeOwnPw}>
          <div className="field">
            <label className="label">Current password</label>
            <input type="password" value={ownPw.current} onChange={(e) => setOwnPw({ ...ownPw, current: e.target.value })} placeholder="Current password" />
          </div>
          <div className="field">
            <label className="label">New password</label>
            <input type="password" value={ownPw.next} onChange={(e) => setOwnPw({ ...ownPw, next: e.target.value })} placeholder="New password" />
          </div>
          <div className="field">
            <label className="label">Confirm new password</label>
            <input type="password" value={ownPw.confirm} onChange={(e) => setOwnPw({ ...ownPw, confirm: e.target.value })} placeholder="Confirm new password" />
          </div>
          <button type="submit" className="btn btn-primary" disabled={ownPwBusy || !ownPw.current || !ownPw.next} style={{ alignSelf: "flex-start" }}>
            {ownPwBusy ? "Saving…" : "Change password"}
          </button>
        </form>
        {ownPwMsg && <div className={`alert ${ownPwMsg.ok ? "alert-success" : "alert-danger"}`}>{ownPwMsg.text}</div>}
      </div>

      <div className="card">
        <h3>Add user</h3>
        <p className="hint">
          A temporary password is generated automatically and emailed to the
          address below — the admin never sets a password directly. The user
          is required to set their own password the first time they log in.
        </p>
        <div className="row" style={stackStyle}>
          <div className="field">
            <label className="label">User Name</label>
            <div className="input-group">
              <span className="ico">👤</span>
              <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="User name" />
            </div>
          </div>
          <div className="field">
            <label className="label">Email</label>
            <div className="input-group">
              <span className="ico">✉</span>
              <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="name@precisionit.co.in" />
            </div>
          </div>
          <div className="field">
            <label className="label">User Type</label>
            <select value={form.user_type_id} onChange={(e) => setForm({ ...form, user_type_id: e.target.value })}>
              {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </div>
          <button className="btn btn-primary" onClick={onCreate} disabled={!form.username.trim() || !form.email.trim()} style={{ alignSelf: "flex-start" }}>✚ Create user</button>
        </div>
        {message && <div className={`alert ${message.ok ? "alert-success" : "alert-danger"}`}>{message.text}</div>}
      </div>

      <div className="card">
        <h3>Users ({users.length})</h3>
        <DataTable
          rows={users.map((u) => ({
            _key: u.UserId, user: u.UserName, type: u.UserTypeName || u.UserTypeID,
            email: u.Email || "—",
            active: u.IsActive ? "Active" : "Inactive", created: u.CreatedDatetime || "—", _u: u,
          }))}
          searchKeys={["user", "type", "email", "active", "created"]}
          empty="No users yet."
          columns={[
            { key: "user", label: "User" },
            { key: "email", label: "Email" },
            { key: "type", label: "Type" },
            { key: "active", label: "Status",
              render: (r) => <span className={`badge ${r._u.IsActive ? "badge-success" : "badge-danger"}`}>{r.active}</span> },
            { key: "created", label: "Created" },
            { key: "_action", label: "Action", sortable: false,
              render: (r) => (
                <div style={{ display: "flex", gap: 6 }}>
                  <button className={`btn btn-sm ${r._u.IsActive ? "btn-danger" : "btn-success"}`} onClick={() => toggle(r._u)}>
                    {r._u.IsActive ? "Inactivate" : "Give access"}
                  </button>
                  {canResetFor(r._u) && (
                    <button className="btn btn-sm btn-subtle" onClick={() => resetPw(r._u)}>
                      Reset password
                    </button>
                  )}
                </div>
              ) },
          ]} />
      </div>
    </div>
  );
}
