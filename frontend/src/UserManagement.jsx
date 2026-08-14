import { useEffect, useState } from "react";
import { getUsers, createUser, setUserActive } from "./api";
import { DataTable } from "./components";

export default function UserManagement() {
  const [users, setUsers] = useState([]);
  const [types, setTypes] = useState([]);
  const [form, setForm] = useState({ username: "", password: "", user_type_id: "" });
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = () =>
    getUsers()
      .then((r) => {
        setUsers(r.users || []); setTypes(r.user_types || []);
        setForm((f) => ({ ...f, user_type_id: f.user_type_id || (r.user_types?.[0]?.id ?? "") }));
      })
      .catch((e) => setMessage({ ok: false, text: e.message }))
      .finally(() => setLoading(false));

  useEffect(() => { refresh(); }, []);

  const onCreate = async () => {
    setMessage(null);
    try {
      await createUser(form.username.trim(), form.password, Number(form.user_type_id));
      setMessage({ ok: true, text: `User "${form.username.trim()}" created.` });
      setForm({ username: "", password: "", user_type_id: types[0]?.id ?? "" });
      refresh();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  const toggle = async (u) => {
    const action = u.IsActive ? "inactivate" : "give access to";
    if (!window.confirm(`Are you sure you want to ${action} "${u.UserName}"?`)) return;
    try { await setUserActive(u.UserId, !u.IsActive); refresh(); }
    catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  if (loading) return <div className="page"><div className="card">Loading…</div></div>;

  return (
    <div className="page">
      <div className="card">
        <h3>Add user</h3>
        <div className="row">
          <div className="field">
            <label className="label">User Name</label>
            <div className="input-group">
              <span className="ico">👤</span>
              <input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="User name" />
            </div>
          </div>
          <div className="field">
            <label className="label">Password</label>
            <div className="input-group">
              <span className="ico">🔒</span>
              <input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} placeholder="Password" />
            </div>
          </div>
          <div className="field">
            <label className="label">User Type</label>
            <select value={form.user_type_id} onChange={(e) => setForm({ ...form, user_type_id: e.target.value })}>
              {types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </div>
          <button className="btn btn-primary" onClick={onCreate} disabled={!form.username.trim() || !form.password}>✚ Create user</button>
        </div>
        {message && <div className={`alert ${message.ok ? "alert-success" : "alert-danger"}`}>{message.text}</div>}
      </div>

      <div className="card">
        <h3>Users ({users.length})</h3>
        <DataTable
          rows={users.map((u) => ({
            _key: u.UserId, user: u.UserName, type: u.UserTypeName || u.UserTypeID,
            active: u.IsActive ? "Active" : "Inactive", created: u.CreatedDatetime || "—", _u: u,
          }))}
          searchKeys={["user", "type", "active", "created"]}
          empty="No users yet."
          columns={[
            { key: "user", label: "User" },
            { key: "type", label: "Type" },
            { key: "active", label: "Status",
              render: (r) => <span className={`badge ${r._u.IsActive ? "badge-success" : "badge-danger"}`}>{r.active}</span> },
            { key: "created", label: "Created" },
            { key: "_action", label: "Action", sortable: false,
              render: (r) => (
                <button className={`btn btn-sm ${r._u.IsActive ? "btn-danger" : "btn-success"}`} onClick={() => toggle(r._u)}>
                  {r._u.IsActive ? "Inactivate" : "Give access"}
                </button>
              ) },
          ]} />
      </div>
    </div>
  );
}
