import { useEffect, useRef, useState } from "react";
import { getFields, saveFields } from "./api";
import { confirmDialog } from "./components";

export default function CreateField({ user }) {
  const [columns, setColumns] = useState({});
  const [template, setTemplate] = useState({});
  const [sources, setSources] = useState({});
  const [active, setActive] = useState("");
  const [newField, setNewField] = useState("");
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(true);
  const dragFrom = useRef(null);

  useEffect(() => {
    getFields()
      .then((r) => {
        setColumns(r.columns || {}); setTemplate(r.template || {});
        setSources(r.sources || {}); setActive(Object.keys(r.columns || {})[0] || "");
      })
      .catch((e) => setMessage({ ok: false, text: e.message }))
      .finally(() => setLoading(false));
  }, []);

  const cols = columns[active] || [];
  const setCols = (next) => setColumns((c) => ({ ...c, [active]: next }));
  // A column added this session and not yet saved has no server-computed
  // source yet - it's a brand new, unmapped column, so it's "None" (same
  // as any other unmapped column) until a mapping/value is set for it.
  const sourceOf = (col) => (sources[active] || {})[col] || "None";
  const isDeletable = (col) => ["Template", "None"].includes(sourceOf(col));

  const addField = () => {
    const name = newField.trim();
    if (!name) return;
    if (cols.includes(name)) { setMessage({ ok: false, text: `"${name}" already exists on this sheet.` }); return; }
    setCols([...cols, name]); setNewField(""); setMessage(null);
  };
  const removeField = async (name) => {
    if (!isDeletable(name)) return;
    if (await confirmDialog(`Delete column "${name}" from ${active}?`, { confirmLabel: "Delete", danger: true })) {
      setCols(cols.filter((c) => c !== name));
    }
  };
  const onDrop = (to) => {
    const from = dragFrom.current; dragFrom.current = null;
    if (from === null || from === to) return;
    const next = [...cols]; const [m] = next.splice(from, 1); next.splice(to, 0, m); setCols(next);
  };
  const resetSheet = async () => {
    if (await confirmDialog(`Reset ${active} columns back to the template?`, { confirmLabel: "Reset" })) {
      setCols([...(template[active] || [])]);
    }
  };
  const onSave = async () => {
    setMessage(null);
    try { await saveFields(columns, user?.user_id); setMessage({ ok: true, text: "Fields saved." }); }
    catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  if (loading) return <div className="page"><div className="card">Loading…</div></div>;

  return (
    <div className="page">
      <div className="card">
        <h3>Create field</h3>
        <p className="hint">Add columns, drag to reorder, or delete. These become the Excel columns; assign their values in <b>Field Mapping</b>. Each column's <b>Source</b> shows where its value actually comes from — only <b>Template</b> and <b>None</b> columns can be deleted here; <b>Service First</b>/<b>PDF</b>/<b>System</b> columns are relied on elsewhere in the app and are protected from accidental deletion.</p>

        <div className="pills">
          {Object.keys(columns).map((s) => (
            <div key={s} className={`pill${active === s ? " active" : ""}`} onClick={() => setActive(s)}>
              {s} ({(columns[s] || []).length})
            </div>
          ))}
        </div>

        <div className="row" style={{ alignItems: "center" }}>
          <div className="input-group" style={{ flex: 1, minWidth: 220 }}>
            <span className="ico">🏷️</span>
            <input value={newField}
                   onChange={(e) => setNewField(e.target.value)}
                   onKeyDown={(e) => e.key === "Enter" && addField()}
                   placeholder="New column name" />
          </div>
          <button className="btn btn-success" onClick={addField}>✚ Add field</button>
          <button className="btn btn-subtle" onClick={resetSheet}>Reset to template</button>
        </div>

        <div className="table-wrap" style={{ marginTop: 14 }}>
          {cols.map((col, i) => {
            const source = sourceOf(col);
            const deletable = isDeletable(col);
            return (
              <div key={col} draggable
                   onDragStart={() => (dragFrom.current = i)}
                   onDragOver={(e) => e.preventDefault()} onDrop={() => onDrop(i)}
                   style={{ display: "flex", alignItems: "center", gap: 10, padding: "9px 14px",
                            borderBottom: i < cols.length - 1 ? "1px solid var(--border)" : "none",
                            background: "var(--surface)", cursor: "grab" }}>
                <span style={{ color: "#cbd5e1" }}>⠿</span>
                <span className="muted" style={{ width: 26, fontSize: 12 }}>{i + 1}</span>
                <span style={{ flex: 1 }}>{col}</span>
                <span className="muted" style={{ width: 90, fontSize: 12 }}>{source}</span>
                <button className="btn-link" title={deletable ? "Delete" : `Not deletable — source is ${source}`}
                        style={{ color: deletable ? "var(--danger)" : "var(--border)", fontSize: 16,
                                 cursor: deletable ? "pointer" : "not-allowed" }}
                        disabled={!deletable} onClick={() => removeField(col)}>×</button>
              </div>
            );
          })}
          {cols.length === 0 && <div className="empty" style={{ padding: 14 }}>No columns.</div>}
        </div>

        <div style={{ marginTop: 16 }}>
          <button className="btn btn-primary" onClick={onSave}>Save Fields</button>
          {message && <span style={{ marginLeft: 14, fontWeight: 600, color: message.ok ? "var(--success)" : "var(--danger)" }}>{message.text}</span>}
        </div>
      </div>
    </div>
  );
}
