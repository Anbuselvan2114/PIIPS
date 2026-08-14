import { useEffect, useState } from "react";
import { getTemplates, saveTemplate, deleteTemplate } from "./api";
import { DataTable } from "./components";

export default function Template({ user }) {
  const [data, setData] = useState(null);
  const [view, setView] = useState("list");
  const [editKey, setEditKey] = useState(null);
  const [error, setError] = useState(null);

  const refresh = () => getTemplates().then(setData).catch((e) => setError(e.message));
  useEffect(() => { refresh(); }, []);

  if (error) return <div className="page"><div className="alert alert-danger">{error}</div></div>;
  if (!data) return <div className="page"><div className="card">Loading…</div></div>;

  if (view === "edit") {
    return <TemplateEdit data={data} sources={data.sources || {}} editKey={editKey} user={user}
             onBack={() => { setView("list"); refresh(); }} />;
  }

  const templates = data.templates || {};
  const keys = Object.keys(templates);

  const onDelete = async (key, e) => {
    e.stopPropagation();
    if (!window.confirm(`Delete template "${key}"?`)) return;
    await deleteTemplate(key, user?.user_id); refresh();
  };

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <button className="btn btn-primary" onClick={() => { setEditKey(null); setView("edit"); }}>✚ New Template</button>
          <h3 style={{ margin: 0 }}>Templates</h3>
        </div>
        <DataTable
          rows={keys.map((key) => {
            const t = templates[key];
            const count = ["Purchase Header", "Purchase Line", "Reservation Entry"]
              .reduce((n, s) => n + Object.keys(t[s] || {}).length, 0);
            return { _key: key, template: key, entity: key.split("\\")[0],
                     invoiceType: key.split("\\")[1],
                     po: t.PO_Number_Format || "", statics: count };
          })}
          searchKeys={["template", "entity", "invoiceType", "po"]} empty="No templates yet."
          columns={[
            { key: "template", label: "Template",
              render: (r) => <button className="btn-link mono" style={{ fontWeight: 600 }}
                                     onClick={() => { setEditKey(r.template); setView("edit"); }}>{r.template}</button> },
            { key: "entity", label: "Entity" },
            { key: "invoiceType", label: "Invoice Type" },
            { key: "po", label: "PO Number Format" },
            { key: "statics", label: "Static values" },
            { key: "_action", label: "", sortable: false,
              render: (r) => <button className="btn btn-danger btn-sm" onClick={(e) => onDelete(r.template, e)}>Delete</button> },
          ]} />
      </div>
    </div>
  );
}

function TemplateEdit({ data, sources, editKey, user, onBack }) {
  const { entities, invoice_types: invoiceTypes, sheets, columns, mapping, templates } = data;
  const existing = editKey ? templates[editKey] : null;

  const [entity, setEntity] = useState(editKey ? editKey.split("\\")[0] : entities[0] || "");
  const [invoiceType, setInvoiceType] = useState(editKey ? editKey.split("\\")[1] : (invoiceTypes || [])[0] || "");
  const [name, setName] = useState(editKey ? editKey.split("\\").slice(2).join("\\") : "");
  const [poFormat, setPoFormat] = useState(existing?.PO_Number_Format || "");
  const [values, setValues] = useState(() => {
    const v = {}; (sheets || []).forEach((s) => (v[s] = { ...(existing?.[s] || {}) })); return v;
  });
  const [message, setMessage] = useState(null);

  const unmapped = (sheet) => (columns[sheet] || []).filter((c) => !(mapping[sheet] && mapping[sheet][c]));
  const setVal = (sheet, col, val) => setValues((v) => ({ ...v, [sheet]: { ...v[sheet], [col]: val } }));

  const onSave = async () => {
    setMessage(null);
    const staticObj = {};
    (sheets || []).forEach((s) => {
      staticObj[s] = {};
      unmapped(s).forEach((c) => { const val = (values[s] || {})[c]; if (val) staticObj[s][c] = val; });
    });
    try { const r = await saveTemplate({ entity, invoice_type: invoiceType, name, po_format: poFormat, static: staticObj, user_id: user?.user_id }); setMessage({ ok: true, text: `Saved "${r.key}"` }); }
    catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <button className="btn btn-subtle" onClick={onBack}>← Back</button>
          <h3 style={{ margin: 0 }}>{editKey ? `Edit: ${editKey}` : "New Template"}</h3>
        </div>

        <div className="row">
          <div className="field" style={{ flex: "0 0 120px" }}>
            <label className="label">Entity</label>
            <select value={entity} disabled={!!editKey} onChange={(e) => setEntity(e.target.value)}>
              {entities.map((en) => <option key={en} value={en}>{en}</option>)}
            </select>
          </div>
          <div className="field" style={{ flex: "0 0 140px" }}>
            <label className="label">Invoice Type</label>
            <select value={invoiceType} disabled={!!editKey} onChange={(e) => setInvoiceType(e.target.value)}>
              {(invoiceTypes || []).map((it) => <option key={it} value={it}>{it}</option>)}
            </select>
          </div>
          <div className="field">
            <label className="label">Template name</label>
            <input value={name} disabled={!!editKey} onChange={(e) => setName(e.target.value)} placeholder="Services_Chennai  or  trichy/service" />
          </div>
          <div className="field">
            <label className="label">PO Number Format</label>
            <input value={poFormat} onChange={(e) => setPoFormat(e.target.value)} placeholder="PO-2627-" />
          </div>
        </div>
        <p className="hint">Enter static values for columns <b>not mapped</b> in Field Mapping. Mapped columns are filled from the invoice JSON and hidden here.</p>
      </div>

      {(sheets || []).map((sheet) => {
        const cols = unmapped(sheet);
        return (
          <div className="card" key={sheet}>
            <h3>{sheet} <span className="muted" style={{ fontWeight: 400, fontSize: 13 }}>({cols.length} unmapped)</span></h3>
            <DataTable
              rows={cols.map((col) => {
                const base = (sources[sheet] || {})[col] || "Template";
                // A column that's neither mapped nor link-overridden shows
                // as "Template" by default, but that's only a real source
                // once a static value is actually entered here — until
                // then, nothing populates it at all.
                const hasStatic = Boolean((values[sheet] || {})[col]);
                const source = base === "Template" && !hasStatic ? "None" : base;
                return { _key: col, column: col, source };
              })}
              searchKeys={["column"]} empty="All columns are mapped."
              columns={[
                { key: "column", label: "Column" },
                { key: "source", label: "Source" },
                { key: "value", label: "Static value", sortable: false,
                  render: (r) => (
                    r.source === "Template" || r.source === "None"
                      ? <input value={(values[sheet] || {})[r.column] || ""}
                               onChange={(e) => setVal(sheet, r.column, e.target.value)}
                               placeholder="static value" />
                      : <span className="hint">Not used — value comes from {r.source}</span>
                  ) },
              ]} />
          </div>
        );
      })}

      <div className="card">
        <button className="btn btn-primary" onClick={onSave} disabled={!entity || !invoiceType || !name.trim()}>Save Template</button>
        {message && <span style={{ marginLeft: 14, fontWeight: 600, color: message.ok ? "var(--success)" : "var(--danger)" }}>{message.text}</span>}
      </div>
    </div>
  );
}
