import { useEffect, useMemo, useState } from "react";
import { getUsers, createUser, setUserActive, adminResetPassword } from "./api";
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
  const [assignPw, setAssignPw] = useState({ target_user_id: "", next: "", confirm: "" });
  const [assignPwMsg, setAssignPwMsg] = useState(null);
  const [assignPwBusy, setAssignPwBusy] = useState(false);

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

  // Super Admin may target anyone, including themselves. An Admin may
  // target only a plain User/Accounts account - never themselves, another
  // Admin, or a Super Admin. Mirrors the backend's own check in
  // /api/users/reset-password (which is the real enforcement).
  const canAssignFor = (u) => {
    if (isSuperAdmin) return true;
    const role = (u.UserTypeName || "").toLowerCase();
    return u.UserId !== user?.user_id && role !== "admin" && role !== "super admin";
  };

  const assignableUsers = useMemo(
    () => users.filter((u) => u.IsActive && canAssignFor(u)),
    [users, isSuperAdmin, user]
  );

  const onAssignPw = async (e) => {
    e.preventDefault();
    setAssignPwMsg(null);
    if (assignPw.next !== assignPw.confirm) { setAssignPwMsg({ ok: false, text: "New passwords do not match." }); return; }
    const target = users.find((u) => String(u.UserId) === String(assignPw.target_user_id));
    setAssignPwBusy(true);
    try {
      const r = await adminResetPassword(user?.user_id, Number(assignPw.target_user_id), assignPw.next);
      setAssignPwMsg({ ok: true, text: `Password updated for "${target?.UserName}" and emailed to them.${emailNote(r)}` });
      setAssignPw({ target_user_id: "", next: "", confirm: "" });
    } catch (e) { setAssignPwMsg({ ok: false, text: e.message }); }
    finally { setAssignPwBusy(false); }
  };

  if (loading) return <div className="page"><div className="card">Loading…</div></div>;

  return (
    <div className="page">
      <div className="card">
        <h3>Change Password</h3>
        <p className="hint">
          Assign a new password for another user — {isSuperAdmin
            ? "as Super Admin you can select any user."
            : "you can select any User/Accounts account (not yourself, another Admin, or a Super Admin)."}
          {" "}The new password is emailed to them and they must set their own on next login.
        </p>
        <form className="row" style={stackStyle} onSubmit={onAssignPw}>
          <div className="field">
            <label className="label">User</label>
            <select value={assignPw.target_user_id}
                    onChange={(e) => setAssignPw({ ...assignPw, target_user_id: e.target.value })}>
              <option value="">Select a user…</option>
              {assignableUsers.map((u) => (
                <option key={u.UserId} value={u.UserId}>{u.UserName} ({u.UserTypeName})</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label className="label">New password</label>
            <input type="password" value={assignPw.next} onChange={(e) => setAssignPw({ ...assignPw, next: e.target.value })} placeholder="New password" />
          </div>
          <div className="field">
            <label className="label">Confirm new password</label>
            <input type="password" value={assignPw.confirm} onChange={(e) => setAssignPw({ ...assignPw, confirm: e.target.value })} placeholder="Confirm new password" />
          </div>
          <button type="submit" className="btn btn-primary" disabled={assignPwBusy || !assignPw.target_user_id || !assignPw.next} style={{ alignSelf: "flex-start" }}>
            {assignPwBusy ? "Saving…" : "Change password"}
          </button>
        </form>
        {assignPwMsg && <div className={`alert ${assignPwMsg.ok ? "alert-success" : "alert-danger"}`}>{assignPwMsg.text}</div>}
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
                  {canAssignFor(r._u) && (
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
