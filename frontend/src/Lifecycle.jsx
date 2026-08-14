import { useEffect, useMemo, useState } from "react";
import { getLifecycleInvoices, advanceLifecycle } from "./api";
import { DataTable, PdfModal } from "./components";

// Load / Post / Complete lifecycle page (one component, parameterised by
// `stage`). Lists invoices at the stage's source status(es); the user selects
// invoices — individually or a whole batch via the batch filter — and advances
// them to the stage's target status in one action.
const STAGES = {
  load:     { title: "Load",     action: "Mark as Loaded",    to: "LOADED" },
  post:     { title: "Post",     action: "Mark as Posted",    to: "POSTED" },
  complete: { title: "Complete", action: "Mark as Completed", to: "COMPLETED" },
};

export default function Lifecycle({ user, stage }) {
  const cfg = STAGES[stage] || STAGES.load;
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [selected, setSelected] = useState({});   // header_id -> bool
  const [batchFilter, setBatchFilter] = useState("");
  const [advancing, setAdvancing] = useState(false);
  const [pdfFile, setPdfFile] = useState(null);

  const load = () => {
    setLoading(true); setError(null); setSelected({});
    getLifecycleInvoices(stage)
      .then((r) => setRows(r.invoices || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); /* reload when the stage changes */ }, [stage]);

  const batches = useMemo(
    () => Array.from(new Set(rows.map((r) => r.batch).filter(Boolean))).sort(),
    [rows]);

  // Batch filter = "batch-wise" selection: narrow the visible rows to a batch.
  const visible = useMemo(
    () => (batchFilter ? rows.filter((r) => r.batch === batchFilter) : rows),
    [rows, batchFilter]);

  const selectedIds = Object.keys(selected).filter((k) => selected[k]).map(Number);
  const allVisibleSelected = visible.length > 0 && visible.every((r) => selected[r.header_id]);

  const toggleAll = () => {
    const next = { ...selected };
    const target = !allVisibleSelected;
    visible.forEach((r) => { next[r.header_id] = target; });
    setSelected(next);
  };

  const advance = async () => {
    if (!selectedIds.length) { setError("Select at least one invoice."); return; }
    setAdvancing(true); setError(null); setMsg(null);
    try {
      const r = await advanceLifecycle(stage, selectedIds, user?.user_id);
      setMsg(`${r.count} invoice(s) → ${r.to}.`);
      load();
    } catch (e) { setError(e.message); }
    finally { setAdvancing(false); }
  };

  const invoiceCell = (row) => (
    row.file_name ? (
      <button className="btn-link" onClick={() => setPdfFile(row.file_name)}
              style={{ background: "none", border: "none", padding: 0, color: "var(--primary)",
                       cursor: "pointer", textDecoration: "underline", font: "inherit" }}>
        {row.invoice_no || row.file_name}
      </button>
    ) : (row.invoice_no || "—")
  );

  const columns = [
    { key: "_sel", sortable: false,
      label: (
        <input type="checkbox" checked={allVisibleSelected} onChange={toggleAll}
               title="Select all" />
      ),
      render: (row) => (
        <input type="checkbox" checked={!!selected[row.header_id]}
               onChange={() => setSelected((s) => ({ ...s, [row.header_id]: !s[row.header_id] }))} />
      ) },
    { key: "invoice_no", label: "Invoice No.", render: invoiceCell },
    { key: "file_name", label: "File" },
    { key: "vendor", label: "Vendor" },
    { key: "status", label: "Status" },
    { key: "batch", label: "Batch" },
  ];

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>{cfg.title}</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={load}>Refresh</button>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Select invoices (individually or filter by batch) and {cfg.action.toLowerCase()}.
        </p>

        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
          <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
            Batch
            <select className="input" style={{ width: "auto" }} value={batchFilter}
                    onChange={(e) => setBatchFilter(e.target.value)}>
              <option value="">All batches</option>
              {batches.map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
          </label>
          <div style={{ flex: 1 }} />
          <span className="muted" style={{ fontSize: 13 }}>{selectedIds.length} selected</span>
          <button className="btn btn-primary" disabled={advancing || !selectedIds.length}
                  onClick={advance}>
            {advancing ? "Working…" : cfg.action}
          </button>
        </div>

        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {msg && <div className="alert alert-success" style={{ marginBottom: 12 }}>{msg}</div>}

        {loading ? <div className="empty">Loading…</div> : (
          <DataTable columns={columns} rows={visible.map((r) => ({ ...r, _key: r.header_id }))}
                     searchKeys={["invoice_no", "file_name", "vendor", "batch", "status"]}
                     pageSizeOptions={[10, 20, 30, "all"]}
                     empty="No invoices are eligible for this step." />
        )}
      </div>
      {pdfFile && <PdfModal file={pdfFile} onClose={() => setPdfFile(null)} />}
    </div>
  );
}
