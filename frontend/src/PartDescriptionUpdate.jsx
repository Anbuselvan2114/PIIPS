import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { getPartDescriptionUpdateItems, savePartDescription } from "./api";
import { DataTable, PdfModal } from "./components";

// Service First's own purchase-line records for every Buyer's Order No
// currently sitting at DATA MISMATCH (GetPurchaseLineSpecificationMismatchRecord)
// - lets a user see what SF actually has on file (PartNo/PartSpecification/
// Nav_Part_Description/pricing) for a PO's parts, side by side, without
// leaving PIIPS.
export default function PartDescriptionUpdate() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [drafts, setDrafts] = useState({});   // SpareRequestID+PartID -> typed description
  const [saving, setSaving] = useState(null);
  const [pdfFile, setPdfFile] = useState(null);
  const [openSuggest, setOpenSuggest] = useState(null);   // row._key with its suggestion dropdown open
  const [suggestPos, setSuggestPos] = useState(null);   // {top, left, width} of that textarea, viewport-relative
  const lastValidRef = useRef({});   // row._key -> last non-duplicate value, for onBlur revert
  const openElRef = useRef(null);   // the textarea DOM node the open dropdown is anchored to

  // The suggestion dropdown is rendered via a portal on document.body (not
  // inline in the table cell) - a plain absolutely-positioned dropdown
  // inside the table got clipped for the LAST row: .table-wrap sets
  // overflow-x: auto, and per the CSS spec that forces its overflow-y to
  // compute as "auto" too (never "visible") even though only overflow-x was
  // set, so anything overflowing past the table's own bottom edge - like a
  // dropdown opening below the last row - was silently cut off/invisible.
  const openSuggestFor = (row, el) => {
    openElRef.current = el;
    const r = el.getBoundingClientRect();
    setSuggestPos({ top: r.bottom, left: r.left, width: r.width });
    setOpenSuggest(row._key);
  };

  // Being a portal, the dropdown isn't a DOM descendant of the scrolling
  // table anymore, so it doesn't move on its own when the table (or the
  // page) scrolls - track the anchor textarea's position live while a
  // dropdown is open so it keeps following the field instead of staying
  // frozen where it first opened. Capture phase so this also catches
  // scrolling on .table-wrap itself (an ancestor scroll container), not
  // just the window/page.
  useEffect(() => {
    if (!openSuggest) return undefined;
    const reposition = () => {
      const el = openElRef.current;
      if (!el || !el.isConnected) return;
      const r = el.getBoundingClientRect();
      setSuggestPos({ top: r.bottom, left: r.left, width: r.width });
    };
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [openSuggest]);

  const load = () => {
    setLoading(true); setError(null);
    getPartDescriptionUpdateItems()
      .then((r) => setRows(r.items || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const rowKey = (r, i) => `${r.SpareRequestID ?? ""}-${r.PartID ?? i}`;
  // Invoice No. lives nested inside PdfInvoices (one row can have several
  // invoices for the same PO), not as a plain top-level field - DataTable's
  // search only does a simple row[key] lookup, so flatten it onto a plain
  // string field here to make it searchable the same way PO/Part No. are.
  const keyedRows = rows.map((r, i) => ({
    ...r, _key: rowKey(r, i),
    _invoiceNos: (r.PdfInvoices || []).map((inv) => inv.InvoiceNo).filter(Boolean).join(" "),
  }));

  // Shown as-is (whatever Service First's API actually returned, including
  // a meaningless placeholder like "1") - the textarea selects all of it on
  // focus (see onFocus below) so the first keystroke replaces it cleanly
  // instead of appending and producing an unmatchable query like
  // "1TEFLON SHEET...".
  const initialValue = (r) => (r.Nav_Part_Description ?? "").trim();

  const effectiveValue = (r) => (drafts[r._key] ?? initialValue(r)).trim();

  // The same invoice description must map to exactly one SF part per PO -
  // reusing it for a second part on the same PO is never allowed (it would
  // recreate the exact ambiguity this screen exists to resolve).
  const descriptionUsedElsewhereInPo = (row, description) => {
    const target = (description || "").trim();
    if (!target) return false;
    return keyedRows.some((r) =>
      r._key !== row._key && r.PurchaseOrderNo === row.PurchaseOrderNo &&
      effectiveValue(r) === target
    );
  };

  const save = async (row, key) => {
    const description = effectiveValue(row);
    if (!description) {
      setError("Enter or pick a description before updating.");
      return;
    }
    if (descriptionUsedElsewhereInPo(row, description)) {
      setError(
        `"${description}" is already assigned to another part on PO ${row.PurchaseOrderNo}.`
      );
      return;
    }
    setSaving(key); setError(null); setMsg(null);
    try {
      await savePartDescription(row.PartNoMapID, description);
      lastValidRef.current[key] = description;
      setMsg(`Updated Service First's description for the part no ${row.PartNo || "this part"}.`);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(null);
    }
  };

  const columns = [
    { key: "purchaseDetails", label: "Purchase Details", sortable: false,
      render: (row) => (
        <div style={{ lineHeight: 1.6, whiteSpace: "nowrap" }}>
          <div><b>PO:</b> {row.PurchaseOrderNo || "—"}</div>
          {(row.PdfInvoices && row.PdfInvoices.length ? row.PdfInvoices : [{}]).map((inv, i) => (
            <div key={i}>
              <div><b>Invoice:</b> {inv.InvoiceNo || "—"}</div>
              <div>
                <b>File:</b>{" "}
                {inv.FileName ? (
                  <button className="btn-link" onClick={() => setPdfFile(inv.FileName)}
                          style={{ background: "none", border: "none", padding: 0, color: "var(--primary)",
                                   cursor: "pointer", textDecoration: "underline", font: "inherit" }}>
                    {inv.FileName}
                  </button>
                ) : "—"}
              </div>
            </div>
          ))}
        </div>
      ) },
    { key: "partDetails", label: "Part Details", sortable: false,
      render: (row) => (
        <div style={{ lineHeight: 1.6, whiteSpace: "nowrap" }}>
          <div><b>Part No:</b> {row.PartNo || "—"}</div>
          <div><b>Specification:</b> {row.PartSpecification || "—"}</div>
          <div><b>Quantity:</b> {row.Quantity ?? "—"}</div>
          <div><b>Unit Price:</b> {row.UnitPrice ?? "—"}</div>
        </div>
      ) },
    { key: "Nav_Part_Description", label: "Invoice Part Description", sortable: false,
      render: (row) => {
        // Plain <input list=...> (native datalist) truncates the shown text
        // to the box's width and doesn't support a textarea at all - a
        // custom type-and-filter dropdown replaces it so the autocomplete
        // still works alongside a full-text, non-hiding textarea.
        const autoGrow = (el) => {
          if (!el) return;
          el.style.height = "auto";
          el.style.height = `${el.scrollHeight}px`;
        };
        const value = drafts[row._key] ?? initialValue(row);
        const query = value.trim().toLowerCase();
        const suggestions = (row.PdfDescriptions || []).filter((d) =>
          !query || d.toLowerCase().includes(query)
        );
        const pick = (d) => {
          lastValidRef.current[row._key] = d;
          setDrafts((dr) => ({ ...dr, [row._key]: d }));
          setOpenSuggest(null);
        };
        return (
          <div>
            <textarea className="input" ref={autoGrow} rows={1}
                      style={{ width: "100%", minWidth: 220, boxSizing: "border-box",
                               resize: "vertical", overflow: "hidden",
                               whiteSpace: "pre-wrap", overflowWrap: "break-word" }}
                      value={value}
                      onFocus={(e) => {
                        lastValidRef.current[row._key] = value;
                        e.target.select();
                        openSuggestFor(row, e.target);
                      }}
                      onChange={(e) => {
                        setDrafts((d) => ({ ...d, [row._key]: e.target.value }));
                        autoGrow(e.target);
                        openSuggestFor(row, e.target);
                      }}
                      onBlur={(e) => {
                        // Close after a tick so a suggestion's onMouseDown
                        // (which fires before this blur) still registers.
                        setTimeout(() => setOpenSuggest((k) => (k === row._key ? null : k)), 150);
                        if (descriptionUsedElsewhereInPo(row, e.target.value)) {
                          setError(
                            `"${e.target.value.trim()}" is already assigned to another part on ` +
                            `PO ${row.PurchaseOrderNo} - each description can only map to one part.`
                          );
                          // Revert to whatever was here before this edit (a
                          // previously-picked suggestion, say) - not the
                          // row's raw SF value, which would silently
                          // discard that.
                          const fallback = lastValidRef.current[row._key] ?? initialValue(row);
                          setDrafts((d) => ({ ...d, [row._key]: fallback }));
                        }
                      }} />
            {openSuggest === row._key && suggestPos && suggestions.length > 0 &&
              createPortal(
                <div style={{
                  position: "fixed", top: suggestPos.top, left: suggestPos.left,
                  width: suggestPos.width, zIndex: 1000,
                  background: "var(--surface)", border: "1px solid var(--border)",
                  borderRadius: 6, marginTop: 2, maxHeight: 180, overflowY: "auto",
                  boxShadow: "0 4px 12px rgba(0,0,0,.12)",
                }}>
                  {suggestions.map((d, i) => {
                    const usedElsewhere = descriptionUsedElsewhereInPo(row, d);
                    return (
                      <div key={i}
                           onMouseDown={(e) => { e.preventDefault(); if (!usedElsewhere) pick(d); }}
                           title={usedElsewhere ? "Already assigned to another part on this PO" : undefined}
                           style={{
                             padding: "6px 10px", fontSize: 13, whiteSpace: "normal",
                             cursor: usedElsewhere ? "not-allowed" : "pointer",
                             opacity: usedElsewhere ? 0.5 : 1,
                             borderBottom: i < suggestions.length - 1 ? "1px solid var(--border)" : "none",
                           }}>
                        {d}
                      </div>
                    );
                  })}
                </div>,
                document.body
              )}
          </div>
        );
      } },
    { key: "action", label: "Action", sortable: false,
      render: (row) => (
        // Purchase/Part Details are nowrap (kept single-line on purpose),
        // so this table is often wider than the content pane - sticking
        // this column to the scroll container's right edge keeps Update
        // reachable no matter the horizontal scroll position or sidebar
        // state, instead of it scrolling out of view off-screen.
        <div style={{ position: "sticky", right: 0, background: "var(--surface)",
                       padding: "2px 0", zIndex: 5 }}>
          <button className="btn btn-primary btn-sm" disabled={saving === row._key}
                  onClick={() => save(row, row._key)}>
            {saving === row._key ? "Updating…" : "Update"}
          </button>
        </div>
      ) },
  ];

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>Part Description Mapping In Service First</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={load}>Refresh</button>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Service First's own purchase-line records for every Buyer's Order No
          currently at DATA MISMATCH - compare against the invoice's own
          description to spot why a part didn't match.
        </p>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {msg && <div className="alert alert-success" style={{ marginBottom: 12 }}>{msg}</div>}
        {loading ? <div className="empty">Loading…</div> : (
          <DataTable columns={columns}
                     rows={keyedRows}
                     searchKeys={["_invoiceNos", "PurchaseOrderNo", "PartNo", "PartSpecification", "Nav_Part_Description"]}
                     pageSizeOptions={[10, 20, 30, "all"]}
                     empty="No Service First records found for the current DATA MISMATCH invoices." />
        )}
      </div>
      {pdfFile && <PdfModal file={pdfFile} onClose={() => setPdfFile(null)} />}
    </div>
  );
}
