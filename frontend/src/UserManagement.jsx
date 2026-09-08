import { useEffect, useMemo, useState } from "react";
import { getUsers, createUser, setUserActive, adminResetPassword, adminChangeUserType } from "./api";
import { DataTable, confirmDialog, PasswordInput } from "./components";

// Fields stacked vertically instead of the shared ".row"'s default
// side-by-side layout — scoped here so other pages that reuse ".row"
// (DatabaseConfig, ApiConfiguration, ...) keep their own layout.
const stackStyle = { flexDirection: "column", alignItems: "stretch", maxWidth: 420, gap: 14 };

export default function UserManagement({ user }) {
  const [users, setUsers] = useState([]);
  const [types, setTypes] = useState([]);
  const [form, setForm] = useState({ username: "", email: "", user_type_id: "", password: "" });
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(true);
  const [assignPw, setAssignPw] = useState({ target_user_id: "", next: "", confirm: "" });
  const [assignPwMsg, setAssignPwMsg] = useState(null);
  const [assignPwBusy, setAssignPwBusy] = useState(false);
  const [typeEdits, setTypeEdits] = useState({});   // user_id -> pending selected type id
  const [typeBusyId, setTypeBusyId] = useState(null);

  const isSuperAdmin = ["super admin", "developer"].includes((user?.user_type || "").toLowerCase());

  // A Viewer account is set up directly by a Super Admin with a chosen
  // password (no email, no auto-generated temp password - see app.py's
  // api_create_user) instead of the normal emailed-temp-password flow
  // every other type uses, so the form below swaps Email for Password
  // when this type is selected.
  const isViewerType = (types.find((t) => String(t.id) === String(form.user_type_id))?.name || "")
    .trim().toLowerCase() === "viewer";

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
      const r = await createUser(
        form.username.trim(), form.email.trim(), Number(form.user_type_id), user?.user_id,
        isViewerType ? form.password : undefined,
      );
      const text = isViewerType
        ? `User "${form.username.trim()}" created with the password you set.`
        : `User "${form.username.trim()}" created. A temporary password was emailed to them.${emailNote(r)}`;
      setMessage({ ok: true, text });
      setForm({ username: "", email: "", user_type_id: types[0]?.id ?? "", password: "" });
      refresh();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  const toggle = async (u) => {
    const action = u.IsActive ? "inactivate" : "give access to";
    const ok = await confirmDialog(`Are you sure you want to ${action} "${u.UserName}"?`, {
      confirmLabel: u.IsActive ? "Inactivate" : "Give access", danger: u.IsActive,
    });
    if (!ok) return;
    try {
      const r = await setUserActive(u.UserId, !u.IsActive, user?.user_id);
      setMessage({ ok: true, text: `"${u.UserName}" ${u.IsActive ? "deactivated" : "activated"}.${emailNote(r)}` });
      refresh();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  const resetPw = async (u) => {
    const ok = await confirmDialog(`Send "${u.UserName}" a new auto-generated password by email?`, {
      confirmLabel: "Send password",
    });
    if (!ok) return;
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

  // Same rule as canAssignFor, applied to the role being ASSIGNED rather
  // than who it's assigned to: an Admin may only hand out the plain
  // User/Accounts role - never promote someone to Admin/Super Admin.
  // Mirrors /api/users/change-type's own check (the real enforcement).
  const assignableTypes = isSuperAdmin
    ? types
    : types.filter((t) => ["user", "accounts"].includes((t.name || "").toLowerCase()));

  const saveType = async (u) => {
    const newTypeId = Number(typeEdits[u.UserId]);
    if (!newTypeId || newTypeId === u.UserTypeID) return;
    setTypeBusyId(u.UserId);
    try {
      const r = await adminChangeUserType(user?.user_id, u.UserId, newTypeId);
      setMessage({ ok: true, text: `"${u.UserName}" is now ${r.user_type_name}.` });
      setTypeEdits((s) => { const n = { ...s }; delete n[u.UserId]; return n; });
      refresh();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
    finally { setTypeBusyId(null); }
  };

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
            <PasswordInput value={assignPw.next} onChange={(e) => setAssignPw({ ...assignPw, next: e.target.value })} placeholder="New password" />
          </div>
          <div className="field">
            <label className="label">Confirm new password</label>
            <PasswordInput value={assignPw.confirm} onChange={(e) => setAssignPw({ ...assignPw, confirm: e.target.value })} placeholder="Confirm new password" />
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
          {isViewerType
            ? "A Viewer account is read-only, so it's set up directly here with a password of your choosing instead of an emailed temporary one — no email address is needed."
            : "A temporary password is generated automatically and emailed to the address below — the admin never sets a password directly. The user is required to set their own password the first time they log in."}
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
            <label className="label">User Type</label>
            <select value={form.user_type_id} onChange={(e) => setForm({ ...form, user_type_id: e.target.value, password: "" })}>
              {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </div>
          {isViewerType ? (
            <div className="field">
              <label className="label">Password</label>
              <PasswordInput value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Password" />
            </div>
          ) : (
            <div className="field">
              <label className="label">Email</label>
              <div className="input-group">
                <span className="ico">✉</span>
                <input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="name@precisionit.co.in" />
              </div>
            </div>
          )}
          <button className="btn btn-primary" onClick={onCreate}
                  disabled={!form.username.trim() || (isViewerType ? !form.password : !form.email.trim())}
                  style={{ alignSelf: "flex-start" }}>✚ Create user</button>
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
            { key: "type", label: "Type", sortable: false,
              render: (r) => {
                if (!canAssignFor(r._u)) return r.type;
                const pending = typeEdits[r._u.UserId];
                const changed = pending !== undefined && Number(pending) !== r._u.UserTypeID;
                return (
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <select value={pending ?? r._u.UserTypeID}
                            onChange={(e) => setTypeEdits((s) => ({ ...s, [r._u.UserId]: e.target.value }))}>
                      {assignableTypes.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                    </select>
                    {changed && (
                      <button className="btn btn-sm btn-primary" disabled={typeBusyId === r._u.UserId}
                              onClick={() => saveType(r._u)}>
                        {typeBusyId === r._u.UserId ? "Saving…" : "Save"}
                      </button>
                    )}
                  </div>
                );
              } },
            { key: "active", label: "Status",
              render: (r) => <span className={`badge ${r._u.IsActive ? "badge-success" : "badge-danger"}`}>{r.active}</span> },
            { key: "created", label: "Created" },
            { key: "_action", label: "Action", sortable: false,
              render: (r) => {
                // Deactivating your own row, or the default Sadmin account,
                // is blocked server-side too - disabled here so the button
                // doesn't invite a click that can only fail.
                const lockedFromDeactivate = r._u.IsActive
                  && (r._u.UserId === user?.user_id || r._u.UserName === "Sadmin");
                return (
                <div style={{ display: "flex", gap: 6 }}>
                  <button className={`btn btn-sm ${r._u.IsActive ? "btn-danger" : "btn-success"}`}
                          onClick={() => toggle(r._u)}
                          disabled={lockedFromDeactivate}
                          title={lockedFromDeactivate
                            ? (r._u.UserName === "Sadmin"
                                ? "Sadmin is always available and can't be deactivated."
                                : "You can't deactivate your own account.")
                            : undefined}>
                    {r._u.IsActive ? "Inactivate" : "Give access"}
                  </button>
                  {canAssignFor(r._u) && (
                    <button className="btn btn-sm btn-subtle" onClick={() => resetPw(r._u)}>
                      Reset password
                    </button>
                  )}
                </div>
              );} },
          ]} />
      </div>
    </div>
  );
}
