import { useEffect, useMemo, useState } from "react";
import { getMapping, saveMapping } from "./api";
import { DataTable } from "./components";

const EMPTY_SHEETS = [];

export default function Mapping({ user }) {
  const [columns, setColumns] = useState({});
  const [fields, setFields] = useState({ header: [], item: [] });
  const [mapping, setMapping] = useState({});
  const [active, setActive] = useState("");
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getMapping()
      .then((r) => {
        setColumns(r.columns || {});
        setFields(r.fields || { header: [], item: [] });
        setMapping(r.mapping || {});
        setActive(Object.keys(r.columns || {})[0] || "");
      })
      .catch((e) => setMessage({ ok: false, text: e.message }))
      .finally(() => setLoading(false));
  }, []);

  const optionsFor = (sheet) =>
    sheet === "Purchase Line" || sheet === "Reservation Entry"
      ? [{ group: "Item fields", opts: fields.item }, { group: "Header fields", opts: fields.header }]
      : [{ group: "Header fields", opts: fields.header }];

  const setCell = (sheet, col, value) =>
    setMapping((m) => ({ ...m, [sheet]: { ...(m[sheet] || {}), [col]: value } }));

  const onSave = async () => {
    setMessage(null);
    try { await saveMapping(mapping, user?.user_id); setMessage({ ok: true, text: "Mapping saved." }); }
    catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  const allRows = useMemo(
    () => Object.keys(columns).flatMap((sheet) =>
      (columns[sheet] || []).map((col) => ({
        _key: `${sheet}|${col}`, sheet, column: col,
        mapped: (mapping[sheet] || {})[col] || "",
      }))),
    [columns, mapping]
  );
  const totalMapped = allRows.filter((r) => r.mapped).length;

  if (loading) return <div className="page"><div className="card">Loading…</div></div>;

  return (
    <div className="page">
      <div className="card">
        <h3>Field mapping</h3>
        <p className="hint">Map each Excel column (per sheet) to a JSON field. Leave blank to skip. Used to fill the workbook and DB tables after processing.</p>
        <div className="hint">{totalMapped}/{allRows.length} columns mapped across all sheets</div>

        <DataTable
          rows={allRows}
          searchKeys={["sheet", "column", "mapped"]} empty="No columns."
          columns={[
            { key: "sheet", label: "Sheet name" },
            { key: "column", label: "Excel column" },
            { key: "mapped", label: "JSON field",
              render: (r) => (
                <select value={(mapping[r.sheet] || {})[r.column] || ""}
                        onChange={(e) => setCell(r.sheet, r.column, e.target.value)}>
                  <option value="">— (blank)</option>
                  {optionsFor(r.sheet).map((g) => (
                    <optgroup key={g.group} label={g.group}>
                      {g.opts.map((f) => <option key={g.group + f} value={f}>{f}</option>)}
                    </optgroup>
                  ))}
                </select>
              ) },
          ]} />

        <div style={{ marginTop: 16 }}>
          <button className="btn btn-primary" onClick={onSave}>Save Mapping</button>
          {message && <span style={{ marginLeft: 14, fontWeight: 600, color: message.ok ? "var(--success)" : "var(--danger)" }}>{message.text}</span>}
        </div>
      </div>
    </div>
  );
}
