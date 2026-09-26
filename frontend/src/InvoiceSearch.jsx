import { useState } from "react";
import { searchInvoices, getInvoiceHistory } from "./api";
import { DataTable, Modal } from "./components";

// Look up one or more invoices by Invoice No. and see, per invoice: its file
// name, vendor, which batch it's in and how that batch itself is
// progressing, its own current status, and a full who-did-what-when
// timeline (Buyer Order Entry, Load/Post/Complete, Part Description Mapping
// updates, ...).
export default function InvoiceSearch() {
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [history, setHistory] = useState(null);   // {invoice_no, file_name, events} | null
  const [historyLoading, setHistoryLoading] = useState(null);   // header_id currently loading

  const runSearch = async () => {
    const q = query.trim();
    if (!q) return;
    setLoading(true); setError(null);
    try {
      const r = await searchInvoices(q);
      setRows(r.invoices || []);
      setSearched(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const openHistory = async (row) => {
    setHistoryLoading(row.header_id);
    setError(null);
    try {
      const r = await getInvoiceHistory(row.header_id);
      setHistory({ invoice_no: row.invoice_no, file_name: row.file_name, events: r.events || [] });
    } catch (e) {
      setError(e.message);
    } finally {
      setHistoryLoading(null);
    }
  };

  const keyedRows = rows.map((r, i) => ({ ...r, _key: r.header_id ?? i }));

  const columns = [
    { key: "file_name", label: "File Name" },
    { key: "invoice_no", label: "Invoice No." },
    { key: "vendor", label: "Vendor Name" },
    { key: "batch", label: "Batch" },
    { key: "batch_status", label: "Batch Status" },
    { key: "status", label: "File Status" },
    { key: "_history", label: "", sortable: false,
      render: (row) => (
        <button className="btn btn-subtle btn-sm" disabled={historyLoading === row.header_id}
                onClick={() => openHistory(row)}>
          {historyLoading === row.header_id ? "Loading…" : "History"}
        </button>
      ) },
  ];

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>Invoice Search</h3>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Look up an invoice by Invoice No. (or part of it) to see its file,
          vendor, batch, current status, and full tracking history.
        </p>
        <div className="row" style={{ marginBottom: 14 }}>
          <input className="input" placeholder="Invoice No., e.g. AVS/26-27/00668" value={query}
                 style={{ maxWidth: 320 }}
                 onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter") runSearch(); }} />
          <button className="btn btn-primary" disabled={loading || !query.trim()} onClick={runSearch}>
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {searched && (
          <DataTable columns={columns} rows={keyedRows}
                     searchKeys={["file_name", "invoice_no", "vendor", "batch", "batch_status", "status"]}
                     pageSizeOptions={[10, 20, 30, "all"]}
                     empty="No invoice matches that Invoice No." />
        )}
      </div>

      {history && (
        <Modal title={`History — ${history.invoice_no || history.file_name}`}
               onClose={() => setHistory(null)} width={720}>
          {history.events.length === 0 ? (
            <div className="empty">No tracking history recorded for this invoice yet.</div>
          ) : (
            <div className="timeline">
              {history.events.map((ev) => (
                <div key={ev.Id} className="timeline-item">
                  <div className="timeline-dot" />
                  <div className="timeline-content">
                    <div className="timeline-header">
                      <span className="timeline-action">{ev.Action}</span>
                      <span className="timeline-time">{ev.EventDatetime}</span>
                    </div>
                    <div className="timeline-who">
                      {ev.UserName}
                      {ev.ToStatus ? (
                        <> · {ev.FromStatus ? `${ev.FromStatus} → ${ev.ToStatus}` : ev.ToStatus}</>
                      ) : null}
                    </div>
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
