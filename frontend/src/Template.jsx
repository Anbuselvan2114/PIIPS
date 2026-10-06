import { useEffect, useState } from "react";
import { getTemplates, saveTemplate, deleteTemplate, activateTemplate, getTemplateHistory } from "./api";
import { DataTable, confirmDialog, Modal } from "./components";

export default function Template({ user }) {
  const [data, setData] = useState(null);
  const [view, setView] = useState("list");
  const [editKey, setEditKey] = useState(null);
  const [error, setError] = useState(null);
  const [actionError, setActionError] = useState(null);
  const [history, setHistory] = useState(null);   // {key, events} | null
  const [historyLoading, setHistoryLoading] = useState(null);   // template key currently loading

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

  const onToggleActive = async (key, isActive, e) => {
    e.stopPropagation();
    setActionError(null);
    const action = isActive ? "Deactivate" : "Activate";
    if (!(await confirmDialog(`${action} template "${key}"?`,
          { confirmLabel: action, danger: isActive }))) return;
    try {
      if (isActive) await deleteTemplate(key, user?.user_id);
      else await activateTemplate(key, user?.user_id);
      refresh();
    } catch (e2) {
      setActionError(e2.message);
    }
  };

  const openHistory = async (key) => {
    setHistoryLoading(key);
    setActionError(null);
    try {
      const r = await getTemplateHistory(key);
      setHistory({ key, events: r.events || [] });
    } catch (e2) {
      setActionError(e2.message);
    } finally {
      setHistoryLoading(null);
    }
  };

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <button className="btn btn-primary" onClick={() => { setEditKey(null); setView("edit"); }}>✚ New Template</button>
          <h3 style={{ margin: 0 }}>Templates</h3>
        </div>
        {actionError && <div className="alert alert-danger" style={{ whiteSpace: "pre-line" }}>{actionError}</div>}
        <DataTable
          rows={keys.map((key) => {
            const t = templates[key];
            const count = ["Purchase Header", "Purchase Line", "Reservation Entry"]
              .reduce((n, s) => n + Object.keys(t[s] || {}).length, 0);
            const isActive = t.IsActive !== false;
            return { _key: key, template: key, entity: key.split("\\")[0],
                     invoiceType: key.split("\\")[1],
                     po: t.PO_Number_Format || "", statics: count, isActive };
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
            { key: "isActive", label: "Status",
              render: (r) => <span style={{ fontWeight: 600, color: r.isActive ? "var(--success)" : "var(--danger)" }}>
                                {r.isActive ? "Active" : "Inactive"}
                              </span> },
            { key: "_action", label: "", sortable: false,
              render: (r) => <button className={`btn btn-sm ${r.isActive ? "btn-danger" : "btn-primary"}`}
                                     onClick={(e) => onToggleActive(r.template, r.isActive, e)}>
                                {r.isActive ? "Deactivate" : "Activate"}
                              </button> },
            { key: "_history", label: "", sortable: false,
              render: (r) => <button className="btn btn-subtle btn-sm" disabled={historyLoading === r.template}
                                     onClick={(e) => { e.stopPropagation(); openHistory(r.template); }}>
                                {historyLoading === r.template ? "Loading…" : "History"}
                              </button> },
          ]} />
      </div>

      {history && (
        <Modal title={`History — ${history.key}`} onClose={() => setHistory(null)} width={600}>
          {history.events.length === 0 ? (
            <div className="empty">No activate/deactivate history recorded for this template yet.</div>
          ) : (
            <div className="timeline">
              {history.events.map((ev) => (
                <div key={ev.Id} className="timeline-item">
                  <div className="timeline-dot" />
                  <div className="timeline-content">
                    <div className="timeline-header">
                      <span className="timeline-action">{ev.Action || "Unknown"}</span>
                      <span className="timeline-time">{ev.EventDatetime || "Unknown"}</span>
                    </div>
                    <div className="timeline-who">{ev.UserName || "Unknown"}</div>
                    {ev.Detail && <div className="timeline-detail">{ev.Detail}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
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
  // Columns with no saved value (source "None") the user has explicitly
  // opted to configure this session via the dropdown below - kept visible
  // even while still blank, unlike every other "None" column which stays
  // hidden until picked.
  const [addedFields, setAddedFields] = useState({});
  const [pendingField, setPendingField] = useState({});
  const [message, setMessage] = useState(null);
  // The key this row is CURRENTLY saved under - starts at editKey, but
  // updates after a successful rename so a second Save (without leaving
  // this screen first) renames again in place instead of looking up a
  // key that no longer exists and creating a duplicate row.
  const [currentKey, setCurrentKey] = useState(editKey || null);

  // Purchase Line "No." is globally mapped to Service First (PART's own
  // Nav Item lookup), so the ordinary "already mapped -> not template-
  // editable" rule below would hide it for SERVICE too - but SERVICE
  // never calls Service First at all, so its own "No." is a template
  // field instead (see excel_export._SERVICE_TEMPLATE_FIELDS / the
  // backend override that actually uses this value at export time). This
  // is the one deliberate carve-out; PART's own "No." stays exactly as
  // hidden/Service-First-sourced as it always was.
  const isServiceNo = (sheet, col) => invoiceType === "SERVICE" && sheet === "Purchase Line" && col === "No.";
  const unmapped = (sheet) => (columns[sheet] || []).filter((c) =>
    isServiceNo(sheet, c) || !(mapping[sheet] && mapping[sheet][c]));
  const setVal = (sheet, col, val) => setValues((v) => ({ ...v, [sheet]: { ...v[sheet], [col]: val } }));
  const sourceOf = (sheet, col) => {
    const base = isServiceNo(sheet, col) ? "Template" : ((sources[sheet] || {})[col] || "Template");
    const hasStatic = Boolean((values[sheet] || {})[col]);
    return base === "Template" && !hasStatic ? "None" : base;
  };
  const addField = (sheet) => {
    const col = pendingField[sheet];
    if (!col) return;
    setAddedFields((a) => ({ ...a, [sheet]: [...(a[sheet] || []), col] }));
    setPendingField((p) => ({ ...p, [sheet]: "" }));
  };

  const onSave = async () => {
    setMessage(null);
    const staticObj = {};
    (sheets || []).forEach((s) => {
      staticObj[s] = {};
      unmapped(s).forEach((c) => { const val = (values[s] || {})[c]; if (val) staticObj[s][c] = val; });
    });
    try {
      const r = await saveTemplate({
        entity, invoice_type: invoiceType, name, po_format: poFormat, static: staticObj,
        user_id: user?.user_id, original_key: currentKey || undefined,
      });
      setMessage({ ok: true, text: currentKey && currentKey !== r.key ? `Renamed to "${r.key}".` : `Saved "${r.key}".` });
      setCurrentKey(r.key);
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <button className="btn btn-subtle" onClick={onBack}>← Back</button>
          <h3 style={{ margin: 0 }}>{editKey ? `Edit: ${currentKey}` : "New Template"}</h3>
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
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Services_Chennai  or  trichy/service" />
          </div>
          <div className="field">
            <label className="label">PO Number Format</label>
            <input value={poFormat} onChange={(e) => setPoFormat(e.target.value)} placeholder="PO-2627-" />
          </div>
        </div>
        <p className="hint">Enter static values for columns <b>not mapped</b> in Field Mapping. Mapped columns are filled from the invoice JSON and hidden here.</p>
      </div>

      {(sheets || []).map((sheet) => {
        const added = addedFields[sheet] || [];
        // On load, only columns that already have a saved value (source
        // "Template") - blank/"None" columns stay hidden unless explicitly
        // added below. Service First / PDF / System-computed columns are
        // never shown at all, since a static value here can't affect them.
        const rows = unmapped(sheet)
          .filter((col) => sourceOf(sheet, col) === "Template" || added.includes(col))
          .map((col) => ({ _key: col, column: col, source: sourceOf(sheet, col) }));

        // Candidates for the dropdown: blank, not-yet-added "None" columns.
        const noneOptions = unmapped(sheet).filter(
          (col) => sourceOf(sheet, col) === "None" && !added.includes(col)
        );

        const addControls = (
          <>
            <select style={{ width: "auto", minWidth: 220 }}
                    value={pendingField[sheet] || ""}
                    onChange={(e) => setPendingField((p) => ({ ...p, [sheet]: e.target.value }))}>
              <option value="">Add a field…</option>
              {noneOptions.map((col) => <option key={col} value={col}>{col}</option>)}
            </select>
            <button className="btn btn-subtle btn-sm" onClick={() => addField(sheet)}
                    disabled={!pendingField[sheet]}>+ Add</button>
          </>
        );

        return (
          <div className="card" key={sheet}>
            <h3>{sheet} <span className="muted" style={{ fontWeight: 400, fontSize: 13 }}>({rows.length} configured)</span></h3>

            <DataTable
              rows={rows}
              actions={addControls}
              searchKeys={["column"]} empty="No configured static-value columns for this sheet yet — add one above."
              columns={[
                { key: "column", label: "Column" },
                { key: "source", label: "Source" },
                { key: "value", label: "Static value", sortable: false,
                  render: (r) => (
                    <input value={(values[sheet] || {})[r.column] || ""}
                           onChange={(e) => setVal(sheet, r.column, e.target.value)}
                           placeholder="static value" />
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
