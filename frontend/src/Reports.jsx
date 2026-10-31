import { useEffect, useState } from "react";
import { getReports, runReport, exportReportExcel, exportReportPdf } from "./api";
import { DataTable } from "./components";

// "BatchLoadedDatetime" -> "Batch Loaded Datetime" - the stored procedure's
// own column names are used as-is for data access/sorting, only prettified
// for display here, so a new report's column names never need a matching
// label table maintained anywhere.
function prettifyColumn(name) {
  return String(name)
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .trim();
}

// Reports menu (Super Admin only) - a list of registered reports, each
// opening into its own result screen (sortable/paged table + Excel/PDF
// download), driven entirely by whatever the backend's generic
// {columns, rows} shape for that report happens to be - this component
// never hardcodes a single report's own column names.
export default function Reports({ user }) {
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedKey, setSelectedKey] = useState(null);

  useEffect(() => {
    setLoading(true); setError(null);
    getReports(user?.user_id)
      .then((r) => setReports(r.reports || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [user?.user_id]);

  if (selectedKey) {
    const report = reports.find((r) => r.key === selectedKey);
    return (
      <ReportView
        user={user}
        reportKey={selectedKey}
        name={report?.name || selectedKey}
        onBack={() => setSelectedKey(null)}
      />
    );
  }

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>Reports</h3>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Pick a report to see its results - sortable, paged, and downloadable
          as Excel or PDF.
        </p>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {loading ? <div className="empty">Loading…</div> : (
          reports.length ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {reports.map((r) => (
                <div key={r.key} onClick={() => setSelectedKey(r.key)}
                     style={{ padding: "12px 16px", border: "1px solid var(--border)",
                              borderRadius: 10, background: "var(--surface)", cursor: "pointer" }}>
                  <div style={{ fontWeight: 600, fontSize: 15 }}>{r.name}</div>
                  {r.description && (
                    <div className="muted" style={{ fontSize: 13, marginTop: 4 }}>{r.description}</div>
                  )}
                </div>
              ))}
            </div>
          ) : <div className="empty">No reports available.</div>
        )}
      </div>
    </div>
  );
}

function ReportView({ user, reportKey, name, onBack }) {
  const [data, setData] = useState(null);   // {columns, rows}
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [exporting, setExporting] = useState(null);   // "excel" | "pdf" | null

  const load = () => {
    setLoading(true); setError(null);
    runReport(reportKey, user?.user_id)
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(load, [reportKey, user?.user_id]);

  const doExport = async (format) => {
    setExporting(format); setError(null);
    try {
      const fn = format === "excel" ? exportReportExcel : exportReportPdf;
      const { blob, filename } = await fn(reportKey, user?.user_id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { setError(e.message); }
    finally { setExporting(null); }
  };

  // A composite report column (e.g. the SLA report's "Invoice Details")
  // packs several labeled sub-values into one cell with CHAR(13)+CHAR(10)
  // between them (see database.py's usp_Report_InvoiceStageSLA) - a plain
  // <td>{value}</td> collapses that to one run-on line (HTML's default
  // white-space handling), so render it with pre-line to keep the line
  // breaks the stored procedure put there, scoped to just this cell
  // rather than the DataTable component everyone else also uses.
  const columns = data
    ? data.columns.map((c) => ({
        key: c,
        label: prettifyColumn(c),
        render: (row) => <span style={{ whiteSpace: "pre-line" }}>{row[c] ?? "—"}</span>,
      }))
    : [];
  const rows = data
    ? data.rows.map((r, i) => ({
        _key: i,
        ...Object.fromEntries(data.columns.map((c, idx) => [c, r[idx]])),
      }))
    : [];

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <button className="btn btn-subtle btn-sm" onClick={onBack}>‹ Reports</button>
          <h3 style={{ marginLeft: 12 }}>{name}</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={load} disabled={loading}>Refresh</button>
          <button className="btn btn-subtle btn-sm" style={{ marginLeft: 8 }}
                  disabled={!data || exporting === "excel"} onClick={() => doExport("excel")}>
            {exporting === "excel" ? "Exporting…" : "Download Excel"}
          </button>
          <button className="btn btn-subtle btn-sm" style={{ marginLeft: 8 }}
                  disabled={!data || exporting === "pdf"} onClick={() => doExport("pdf")}>
            {exporting === "pdf" ? "Exporting…" : "Download PDF"}
          </button>
        </div>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {loading ? <div className="empty">Loading…</div> : (
          <DataTable
            columns={columns}
            rows={rows}
            pageSizeOptions={[10, 25, 50, 100, "all"]}
            empty="This report has no rows right now."
          />
        )}
      </div>
    </div>
  );
}
