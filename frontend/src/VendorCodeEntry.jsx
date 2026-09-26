import { useEffect, useState } from "react";
import { getVendorCodeMissing, setVendorCode } from "./api";
import { DataTable, PdfModal } from "./components";

// Manual NAV Vendor Code entry.
//
// Lists SERVICE invoices parked at "NAV VENDOR CODE DOESN'T EXIST" (the
// vendor code could not be read off the scanned PDF). SERVICE never calls
// Service First, so this is the only source for the code at all. The user
// types the correct code for a row and saves; the invoice becomes active
// (status = 1) and moves to Ready to Load. Clicking the invoice/file opens
// the PDF so the hand-written code can be read.
export default function VendorCodeEntry({ user }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [drafts, setDrafts] = useState({});   // header_id -> typed vendor code
  const [saving, setSaving] = useState(null);  // header_id being saved
  const [pdfFile, setPdfFile] = useState(null);

  const load = () => {
    setLoading(true); setError(null);
    getVendorCodeMissing()
      .then((r) => {
        const invoices = r.invoices || [];
        setRows(invoices);
        // Pre-fill each row's input with whatever PIIPS already extracted
        // (blank or a doubtful best-guess - see vendor_code.py) instead of
        // starting empty, so confirming an already-correct value is just
        // Save, not retyping it. Only seeds rows with no draft yet, so a
        // Refresh mid-edit never clobbers what the user is currently typing.
        setDrafts((d) => {
          const next = { ...d };
          for (const inv of invoices) {
            if (!(inv.header_id in next)) next[inv.header_id] = inv.vendor_code || "";
          }
          return next;
        });
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const save = async (row) => {
    const code = (drafts[row.header_id] || "").trim();
    if (!code) { setError("Enter a Vendor Code first."); return; }
    setSaving(row.header_id); setError(null); setMsg(null);
    try {
      const r = await setVendorCode(row.header_id, code, user?.user_id);
      setMsg(`${row.invoice_no || row.file_name}: ${r.new_status}` +
             (r.reason ? ` — ${r.reason}` : ""));
      // Drop rows that are no longer missing a vendor code; reload to refresh the list.
      load();
    } catch (e) { setError(e.message); }
    finally { setSaving(null); }
  };

  const invoiceCell = (row) => (
    row.file_name ? (
      <button className="btn-link" onClick={() => setPdfFile({ file: row.file_name, page: row.page_start ?? row.page, pageEnd: row.page_end })}
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
    { key: "code", label: "NAV Vendor Code", sortable: false,
      render: (row) => (
        <input className="input" style={{ minWidth: 200 }}
               placeholder="V00123"
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
          <h3>Vendor Code Entry</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={load}>Refresh</button>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          SERVICE invoices whose NAV Vendor Code could not be read off the scanned PDF.
          Enter the correct code and Save — the invoice becomes active and moves forward.
        </p>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {msg && <div className="alert alert-success" style={{ marginBottom: 12 }}>{msg}</div>}
        {loading ? <div className="empty">Loading…</div> : (
          <DataTable columns={columns} rows={rows.map((r) => ({ ...r, _key: r.header_id }))}
                     searchKeys={["invoice_no", "file_name", "vendor", "batch"]}
                     pageSizeOptions={[10, 20, 30, "all"]}
                     empty="No invoices are waiting for a Vendor Code." />
        )}
      </div>
      {pdfFile && <PdfModal file={pdfFile.file} page={pdfFile.page} pageEnd={pdfFile.pageEnd} onClose={() => setPdfFile(null)} />}
    </div>
  );
}
