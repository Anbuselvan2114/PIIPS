import { useEffect, useState } from "react";
import { getPurchaseInvoiceMappingItems, setPurchaseInvoiceNo } from "./api";
import { DataTable } from "./components";

// Purchase Invoice Mapping.
//
// Lists every invoice currently at PURCHASE INVOICE PENDING (an invoice
// lands here after the Load lifecycle stage - see Lifecycle.jsx's "load"
// stage, whose real target status is Purchase Invoice Pending, not Loaded
// directly), showing its Navision Document No. (the header's own [No.]
// column) alongside an editable Purchase Invoice No. field. Saving a
// non-blank value promotes the invoice straight to LOADED; a clashing
// Navision Document No. on another invoice still saves the value but
// blocks the promotion (surfaced as an error).
export default function PurchaseInvoiceMapping({ user }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [drafts, setDrafts] = useState({});   // header_id -> typed Purchase Invoice No
  const [saving, setSaving] = useState(null);  // header_id being saved

  const load = () => {
    setLoading(true); setError(null);
    getPurchaseInvoiceMappingItems()
      .then((r) => {
        setRows(r.items || []);
        setDrafts(Object.fromEntries(
          (r.items || []).map((it) => [it.header_id, it.purchase_invoice_no || ""])
        ));
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const save = async (row) => {
    const value = (drafts[row.header_id] || "").trim();
    if (!value) { setError("Enter a Purchase Invoice No first."); return; }
    setSaving(row.header_id); setError(null); setMsg(null);
    try {
      const r = await setPurchaseInvoiceNo(row.header_id, value, user?.user_id);
      setMsg(`${row.navision_doc_no || row.header_id}: ${r.new_status || "Purchase Invoice No saved"}.`);
      load();
    } catch (e) { setError(e.message); }
    finally { setSaving(null); }
  };

  const columns = [
    { key: "navision_doc_no", label: "Navision Document No" },
    { key: "purchase_invoice_no", label: "Purchase Invoice No", sortable: false,
      render: (row) => (
        <input className="input" style={{ minWidth: 200 }}
               placeholder="Purchase Invoice No"
               value={drafts[row.header_id] ?? ""}
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
          <h3>Purchase Invoice Mapping</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={load}>Refresh</button>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Invoices marked as Loaded are waiting for their Purchase Invoice No.
          Enter the vendor's Purchase Invoice No. for each Navision Document No.
          and Save — the invoice then moves to Loaded.
        </p>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {msg && <div className="alert alert-success" style={{ marginBottom: 12 }}>{msg}</div>}
        {loading ? <div className="empty">Loading…</div> : (
          <DataTable columns={columns} rows={rows.map((r) => ({ ...r, _key: r.header_id }))}
                     searchKeys={["navision_doc_no", "purchase_invoice_no"]}
                     pageSizeOptions={[10, 20, 30, "all"]}
                     empty="No invoices are waiting for a Purchase Invoice No." />
        )}
      </div>
    </div>
  );
}
