import { useEffect, useState } from "react";
import { getBuyerOrderMissing, setBuyerOrder } from "./api";
import { DataTable, PdfModal } from "./components";

// Manual Buyer's Order No. entry.
//
// Lists invoices parked at "BUYER ORDER NO DOESN'T EXIST" (PO missing or the
// OCR-read PO looked doubtful). The user types the correct PO for a row and
// saves; the backend re-validates the invoice and, if nothing else is wrong,
// makes it active (status = 1) and advances it. Clicking the invoice/file
// opens the PDF so a handwritten PO can be read.
export default function BuyerOrderEntry({ user }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [drafts, setDrafts] = useState({});   // header_id -> typed PO
  const [saving, setSaving] = useState(null);  // header_id being saved
  const [pdfFile, setPdfFile] = useState(null);

  const load = () => {
    setLoading(true); setError(null);
    getBuyerOrderMissing()
      .then((r) => setRows(r.invoices || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const save = async (row) => {
    const po = (drafts[row.header_id] || "").trim();
    if (!po) { setError("Enter a Buyer Order No first."); return; }
    setSaving(row.header_id); setError(null); setMsg(null);
    try {
      const r = await setBuyerOrder(row.header_id, po, user?.user_id);
      setMsg(`${row.invoice_no || row.file_name}: ${r.new_status}` +
             (r.reason ? ` — ${r.reason}` : ""));
      // Drop rows that are no longer missing a PO; reload to refresh the list.
      load();
    } catch (e) { setError(e.message); }
    finally { setSaving(null); }
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
    { key: "invoice_no", label: "Invoice No.", render: invoiceCell },
    { key: "file_name", label: "File" },
    { key: "vendor", label: "Vendor" },
    { key: "batch", label: "Batch" },
    { key: "po", label: "Buyer Order No", sortable: false,
      render: (row) => (
        <input className="input" style={{ minWidth: 200 }}
               placeholder="SPRPUR/2026/04/27-83650"
               value={drafts[row.header_id] || ""}
               onChange={(e) => setDrafts((d) => ({ ...d, [row.header_id]: e.target.value }))} />
      ) },
    { key: "save", label: "", sortable: false,
      render: (row) => (
        <button className="btn btn-primary btn-sm" disabled={saving === row.header_id}
                onClick={() => save(row)}>
          {saving === row.header_id ? "Saving…" : "Save"}
        </button>
      ) },
  ];

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>Buyer Order Entry</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={load}>Refresh</button>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Invoices whose Buyer Order No. could not be read (missing or doubtful).
          Enter the correct PO and Save — the invoice is re-validated and, if no
          other error remains, becomes active and moves forward.
        </p>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {msg && <div className="alert alert-success" style={{ marginBottom: 12 }}>{msg}</div>}
        {loading ? <div className="empty">Loading…</div> : (
          <DataTable columns={columns} rows={rows.map((r) => ({ ...r, _key: r.header_id }))}
                     searchKeys={["invoice_no", "file_name", "vendor", "batch"]}
                     pageSizeOptions={[10, 20, 30, "all"]}
                     empty="No invoices are waiting for a Buyer Order No." />
        )}
      </div>
      {pdfFile && <PdfModal file={pdfFile} onClose={() => setPdfFile(null)} />}
    </div>
  );
}
