import { Fragment, useEffect, useState } from "react";
import { getRoleMenus, saveRoleMenus } from "./api";
import { MENU, ROLE_MENU_EXCLUDED_KEYS, CONFIGURABLE_ROLES } from "./menuConfig";

const CONFIGURABLE_KEYS = MENU
  .map(([k]) => k)
  .filter((k) => !ROLE_MENU_EXCLUDED_KEYS.includes(k));

export default function RoleMenuAccess({ user, onRoleMenusSaved }) {
  const [mapping, setMapping] = useState(null);   // {role: Set(menuKey)}
  const [loadError, setLoadError] = useState(null);
  const [message, setMessage] = useState(null);
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoadError(null);
    return getRoleMenus()
      .then((r) => {
        const m = {};
        CONFIGURABLE_ROLES.forEach(({ key }) => m[key] = new Set(r.mapping?.[key] || []));
        setMapping(m);
      })
      .catch((e) => setLoadError(e.message));
  };

  useEffect(() => { load(); }, []);

  const toggle = (role, key) =>
    setMapping((prev) => {
      const next = { ...prev, [role]: new Set(prev[role]) };
      next[role].has(key) ? next[role].delete(key) : next[role].add(key);
      return next;
    });

  const onSave = async () => {
    setMessage(null);
    setSaving(true);
    try {
      const payload = {};
      CONFIGURABLE_ROLES.forEach(({ key }) => payload[key] = Array.from(mapping[key]));
      await saveRoleMenus(payload, user?.user_id);
      setMessage({ ok: true, text: "Screen access saved." });
      onRoleMenusSaved?.();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
    finally { setSaving(false); }
  };

  if (loadError) {
    return (
      <div className="page">
        <div className="card">
          <h3>Screen access</h3>
          <p className="hint" style={{ color: "var(--danger)" }}>
            Couldn't load screen access: {loadError}
          </p>
          <button className="btn btn-primary" onClick={load}>Retry</button>
        </div>
      </div>
    );
  }

  if (!mapping) return <div className="page"><div className="card">Loading…</div></div>;

  // Group rows by nav group, same order as the sidebar itself.
  const groups = [];
  MENU.forEach(([k, label, ico, grp]) => {
    if (!CONFIGURABLE_KEYS.includes(k)) return;
    let g = groups.find((x) => x.name === grp);
    if (!g) { g = { name: grp, items: [] }; groups.push(g); }
    g.items.push([k, label, ico]);
  });

  return (
    <div className="page">
      <div className="card">
        <h3>Screen access</h3>
        <p className="hint">
          Choose which screens each role can see. Super Admin (and the legacy Developer
          alias) always sees every screen — this menu itself included — and isn't shown here.
        </p>

        <div className="table-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Screen</th>
                {CONFIGURABLE_ROLES.map((r) => <th key={r.key} style={{ textAlign: "center" }}>{r.label}</th>)}
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => (
                <Fragment key={g.name}>
                  <tr>
                    <td colSpan={CONFIGURABLE_ROLES.length + 1}
                        style={{ fontWeight: 700, background: "var(--surface-2)" }}>
                      {g.name}
                    </td>
                  </tr>
                  {g.items.map(([k, label, ico]) => (
                    <tr key={k}>
                      <td><span style={{ marginRight: 8 }}>{ico}</span>{label}</td>
                      {CONFIGURABLE_ROLES.map((r) => (
                        <td key={r.key} style={{ textAlign: "center" }}>
                          <input
                            type="checkbox"
                            checked={mapping[r.key].has(k)}
                            onChange={() => toggle(r.key, k)}
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>

        <div style={{ marginTop: 16 }}>
          <button className="btn btn-primary" onClick={onSave} disabled={saving}>
            {saving ? "Saving…" : "Save Screen Access"}
          </button>
          {message && (
            <span style={{ marginLeft: 14, fontWeight: 600, color: message.ok ? "var(--success)" : "var(--danger)" }}>
              {message.text}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
