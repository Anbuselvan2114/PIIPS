import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { invoicePdfUrl, getActiveAnnouncements, announcementImageUrl, getActiveJob } from "./api";

// PIIPS logo — a monogram "P" (source: logo/PIIPS-logo.svg) on an indigo-to-
// magenta gradient badge; the P's counter doubles as a precision-target dot,
// with three ledger-line notches at the base. Gradient ids are suffixed with
// useId() so multiple <Logo> instances on one page (e.g. collapsed + full
// sidebar) never collide.
export function Logo({ size = 40 }) {
  const uid = useId();
  const bg = `piips-bg-${uid}`;
  const mark = `piips-mark-${uid}`;
  return (
    <svg width={size} height={size} viewBox="0 0 512 512"
         xmlns="http://www.w3.org/2000/svg" role="img" aria-label="PIIPS">
      <defs>
        <linearGradient id={bg} x1="0" y1="0" x2="512" y2="512" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#1e1b4b" /><stop offset="0.55" stopColor="#5b21b6" />
          <stop offset="1" stopColor="#c026d3" />
        </linearGradient>
        <linearGradient id={mark} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#fde68a" /><stop offset="1" stopColor="#f59e0b" />
        </linearGradient>
      </defs>
      <rect x="16" y="16" width="480" height="480" rx="112" fill={`url(#${bg})`} />
      <rect x="16" y="16" width="480" height="480" rx="112" fill="none" stroke="#ffffff" strokeOpacity="0.10" strokeWidth="2" />
      <g fill={`url(#${mark})`}>
        <rect x="176" y="132" width="56" height="248" rx="20" />
        <path fillRule="evenodd" d="M216 132 H288 A76 76 0 0 1 288 284 H216 V132 Z
              M232 176 V240 H288 A32 32 0 0 0 288 176 H232 Z" />
      </g>
      <circle cx="260" cy="208" r="9" fill="#1e1b4b" />
      <g fill="#ffffff" fillOpacity="0.85">
        <rect x="176" y="330" width="34" height="10" rx="5" />
        <rect x="176" y="350" width="50" height="10" rx="5" />
        <rect x="176" y="370" width="42" height="10" rx="5" />
      </g>
    </svg>
  );
}

// Plain stroke-based eye / eye-slash glyphs - a crisp, theme-colored SVG
// renders identically everywhere, unlike an emoji (👁️/🙈), which varies
// wildly in size/style across OS emoji sets and looked out of place next
// to the rest of the UI's clean line icons.
function _EyeIcon({ off }) {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7Z" />
      <circle cx="12" cy="12" r="3" />
      {off && <line x1="2" y1="2" x2="22" y2="22" />}
    </svg>
  );
}

// Password field with a show/hide toggle ("eye" button) - drop-in
// replacement for a bare <input type="password">. `icon` is optional (a
// leading glyph like Login.jsx's 🔒); omitted, it's just the input + toggle.
export function PasswordInput({
  value, onChange, placeholder, icon, onPaste, autoComplete, autoFocus, disabled,
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="input-group">
      {icon && <span className="ico">{icon}</span>}
      <input type={show ? "text" : "password"} value={value} onChange={onChange}
             placeholder={placeholder} onPaste={onPaste} autoComplete={autoComplete}
             autoFocus={autoFocus} disabled={disabled} />
      <button type="button" onClick={() => setShow((s) => !s)} disabled={disabled}
              aria-label={show ? "Hide password" : "Show password"}
              title={show ? "Hide password" : "Show password"}
              style={{ background: "none", border: "none", cursor: disabled ? "not-allowed" : "pointer",
                       padding: 0, display: "flex", alignItems: "center", color: "var(--muted)",
                       flex: "0 0 auto" }}>
        <_EyeIcon off={show} />
      </button>
    </div>
  );
}

// Type-and-search <select> replacement: an input that filters `options`
// (an array of strings) as you type, with a click/keyboard-navigable
// dropdown below it. Stays a PICKER, not free text - blurring without
// landing on an exact option reverts the text to `value`, so the parent
// only ever sees one of `options` (or "") through onChange, same
// contract a plain <select> has. Built for File Explorer's Template
// dropdown (which can grow to many entries a native <select> makes
// tedious to scan) but generic enough for any string-option picker.
export function SearchableSelect({ value, onChange, options, placeholder = "Search…", disabled, emptyLabel }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState(value || "");
  const [activeIdx, setActiveIdx] = useState(-1);
  const blurTimer = useRef(null);

  // Reflect an externally-changed `value` into the displayed text, but
  // never fight the user while they're actively typing/filtering.
  useEffect(() => { if (!open) setQuery(value || ""); }, [value, open]);
  useEffect(() => () => clearTimeout(blurTimer.current), []);

  const q = query.trim().toLowerCase();
  const filtered = useMemo(
    () => options.filter((o) => !q || o.toLowerCase().includes(q)),
    [options, q]
  );

  const commit = (v) => { onChange(v); setQuery(v); setOpen(false); setActiveIdx(-1); };
  const revert = () => { setQuery(value || ""); setOpen(false); setActiveIdx(-1); };

  return (
    <div style={{ position: "relative" }}>
      <input
        className="input"
        value={query}
        placeholder={placeholder}
        disabled={disabled}
        onFocus={() => { setQuery(value || ""); setOpen(true); setActiveIdx(-1); }}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); setActiveIdx(-1); }}
        onBlur={() => {
          // Delayed so a suggestion's onMouseDown (which fires first)
          // still registers before this closes/reverts the field.
          blurTimer.current = setTimeout(revert, 150);
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") { revert(); e.currentTarget.blur(); }
          else if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActiveIdx((i) => Math.min(i + 1, filtered.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIdx((i) => Math.max(i - 1, 0)); }
          else if (e.key === "Enter") { e.preventDefault(); if (activeIdx >= 0 && filtered[activeIdx]) commit(filtered[activeIdx]); }
        }} />
      {open && filtered.length > 0 && (
        <div style={{
          position: "absolute", top: "100%", left: 0, right: 0, zIndex: 50,
          background: "var(--surface)", border: "1px solid var(--border)",
          borderRadius: "var(--radius-sm)", marginTop: 2, maxHeight: 220,
          overflowY: "auto", boxShadow: "0 8px 24px rgba(0,0,0,.12)",
        }}>
          {filtered.map((o, i) => (
            <div key={o} onMouseDown={() => commit(o)}
                 onMouseEnter={() => setActiveIdx(i)}
                 className={`searchable-select-option${i === activeIdx ? " active" : ""}`}>
              {o}
            </div>
          ))}
        </div>
      )}
      {open && filtered.length === 0 && emptyLabel && (
        <div style={{
          position: "absolute", top: "100%", left: 0, right: 0, zIndex: 50,
          background: "var(--surface)", border: "1px solid var(--border)",
          borderRadius: "var(--radius-sm)", marginTop: 2, boxShadow: "0 8px 24px rgba(0,0,0,.12)",
        }}>
          <div className="searchable-select-option" style={{ color: "var(--muted)", cursor: "default" }}>
            {emptyLabel}
          </div>
        </div>
      )}
    </div>
  );
}

// Generic table: search box, click-to-sort headers, 10-row pagination.
// columns: [{ key, label, render?(row), sortable? }]
// defaultSortKey/defaultSortDir: sort applied up front (shown pre-sorted,
// with the header's arrow already lit) instead of leaving it unsorted
// until the user clicks a header - use when the rows' own incoming order
// isn't a reliable enough guarantee on its own for the caller's intent.
export function DataTable({ columns, rows, searchKeys, pageSize = 10,
                            pageSizeOptions, empty, actions,
                            defaultSortKey = null, defaultSortDir = "asc" }) {
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState(defaultSortKey);
  const [sortDir, setSortDir] = useState(defaultSortDir);
  const [page, setPage] = useState(0);
  // When pageSizeOptions is given, the user can change rows-per-page (incl.
  // "All"); otherwise the fixed `pageSize` prop is used.
  const [size, setSize] = useState(pageSize);
  const effectiveSize = size === "all" ? Math.max(rows.length, 1) : size;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    const keys = searchKeys || columns.map((c) => c.key);
    return rows.filter((row) =>
      keys.some((k) => String(row[k] ?? "").toLowerCase().includes(q))
    );
  }, [rows, query, searchKeys, columns]);

  const sorted = useMemo(() => {
    if (!sortKey) return filtered;
    const arr = [...filtered];
    arr.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      const an = Number(av), bn = Number(bv);
      let cmp;
      if (!isNaN(an) && !isNaN(bn) && av !== "" && bv !== "") cmp = an - bn;
      else cmp = String(av ?? "").localeCompare(String(bv ?? ""));
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [filtered, sortKey, sortDir]);

  const pageCount = Math.max(1, Math.ceil(sorted.length / effectiveSize));
  const cur = Math.min(page, pageCount - 1);
  const view = sorted.slice(cur * effectiveSize, cur * effectiveSize + effectiveSize);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir("asc"); }
  };

  // Reset to page 1 on a new search or page-size change - NOT on every
  // `rows` prop change. A caller typically rebuilds that array fresh on
  // every render (e.g. Dashboard's batchRows = batches.map(...)), so an
  // unrelated re-render elsewhere on the page (typing into a Doc No./
  // Entry No. input, say) was giving `rows` a new reference each
  // keystroke and silently bouncing the table back to page 1 even though
  // nothing about its own data changed. If the row count genuinely
  // shrinks out from under the current page, `cur` below already clamps
  // to the last valid page instead of crashing - no separate reset needed.
  useEffect(() => { setPage(0); }, [query, size]);

  const searchRow = (
    <div style={{ marginBottom: 10, display: "flex", alignItems: "center", gap: 8 }}>
      <input className="input" placeholder="Search…" value={query}
             onChange={(e) => setQuery(e.target.value)} style={{ maxWidth: 260 }} />
      {actions}
    </div>
  );

  if (!rows.length) {
    return (
      <div>
        {actions && searchRow}
        <div className="empty">{empty || "No rows."}</div>
      </div>
    );
  }

  return (
    <div>
      {searchRow}
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key}
                    onClick={c.sortable === false ? undefined : () => toggleSort(c.key)}
                    style={{ cursor: c.sortable === false ? "default" : "pointer", userSelect: "none" }}>
                  {c.label}{sortKey === c.key ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.map((row, i) => (
              <tr key={row._key ?? i}>
                {columns.map((c) => (
                  <td key={c.key}>{c.render ? c.render(row) : (row[c.key] ?? "—")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 10 }}>
        <span className="muted" style={{ fontSize: 13 }}>
          {sorted.length} row{sorted.length === 1 ? "" : "s"}
          {query ? ` (filtered from ${rows.length})` : ""}
        </span>
        {pageSizeOptions && (
          <label className="muted" style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            Rows
            <select className="input" style={{ width: "auto", padding: "2px 6px" }}
                    value={size}
                    onChange={(e) => setSize(e.target.value === "all" ? "all" : Number(e.target.value))}>
              {pageSizeOptions.map((o) => (
                <option key={o} value={o}>{o === "all" ? "All" : o}</option>
              ))}
            </select>
          </label>
        )}
        <div style={{ flex: 1 }} />
        <button className="btn btn-subtle btn-sm" disabled={cur <= 0}
                onClick={() => setPage(cur - 1)}>‹ Prev</button>
        <span className="muted" style={{ fontSize: 13 }}>Page {cur + 1} / {pageCount}</span>
        <button className="btn btn-subtle btn-sm" disabled={cur >= pageCount - 1}
                onClick={() => setPage(cur + 1)}>Next ›</button>
      </div>
    </div>
  );
}

// A progress bar split into one piece per step: every file is a piece, and
// (process runs) a final extra piece is the Service First sync + save - so a
// 100-file run shows 101 pieces and only fills the last one when the run is
// really complete. `done` pieces are filled left to right; with files being
// extracted in parallel the fill advances as each one finishes. The last
// piece pulses while its step is running.
export function SegmentedProgress({ total, done, segments, syncStep = false, failed = false }) {
  const n = Math.max(0, total || 0);
  if (!n) return <div className="progress"><div className="progress-bar" style={{ width: "0%" }} /></div>;
  const filled = Math.min(done || 0, n);
  // Each piece is a small bar of its own: 0..100% for its file (files being
  // extracted in parallel fill side by side), the last one for the sync step.
  // Every piece looks the same - only its fill differs.
  const pct = (i) => (segments && segments.length === n ? segments[i] : (i < filled ? 100 : 0));
  const cells = [];
  for (let i = 0; i < n; i++) {
    const isSync = syncStep && i === n - 1;
    const p = Math.max(0, Math.min(100, pct(i)));
    cells.push(
      <span key={i} className="seg"
        title={`${isSync ? "Service First sync & save" : `File ${i + 1} of ${syncStep ? n - 1 : n}`} — ${p}%`}>
        <span className={`seg-fill${failed ? " seg-failed" : ""}`} style={{ width: `${p}%` }} />
      </span>
    );
  }
  return (
    <div className="seg-progress" style={{ gridTemplateColumns: `repeat(${n}, 1fr)` }}
         role="progressbar" aria-valuemin={0} aria-valuemax={n} aria-valuenow={filled}>
      {cells}
    </div>
  );
}


// Slim, app-wide progress strip for the invoice-processing run that is
// currently going - whoever started it. Only one process run can exist at a
// time, so every signed-in user (on any screen) sees the same live bar, who
// started it, and knows Start is unavailable until it finishes. Hidden when
// nothing is running (and on the Dashboard, which shows the full card).
export function JobBanner({ hidden = false }) {
  const [job, setJob] = useState(null);
  useEffect(() => {
    let stop = false;
    let timer = null;
    const tick = async () => {
      let next = null;
      try {
        const j = await getActiveJob("process", true);
        if (j && (j.status === "running" || j.status === "pending")) next = j;
      } catch { /* server briefly unreachable - keep the last view */ next = undefined; }
      if (stop) return;
      if (next !== undefined) setJob(next);
      timer = setTimeout(tick, next ? 1000 : 3000);
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
  }, []);
  if (!job || hidden) return null;
  const files = job.file_total ?? job.total;
  const phase = job.processed >= job.total ? "finishing"
    : job.processed >= job.total - 1 ? "syncing with Service First" : "extracting";
  return (
    <div className="job-banner">
      <div className="progress-meta" style={{ marginBottom: 6 }}>
        <span>
          <strong>Invoice processing in progress</strong>
          {job.started_by_name ? ` — started by ${job.started_by_name}` : ""}
          {" · "}{phase}
        </span>
        <span>{Math.min(job.processed, files)}/{files} files · {job.percent}%</span>
      </div>
      <SegmentedProgress total={job.total} done={job.processed} segments={job.segments} syncStep />
    </div>
  );
}


export function Modal({ title, onClose, children, width = 1000 }) {
  return (
    <div onClick={onClose}
         style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", zIndex: 1000,
                  display: "flex", alignItems: "flex-start", justifyContent: "center",
                  padding: "48px 16px", overflow: "auto" }}>
      <div onClick={(e) => e.stopPropagation()} className="card"
           style={{ width: `min(${width}px, 100%)`, maxWidth: width, margin: 0 }}>
        <div className="card-title-row">
          <h3>{title}</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={onClose}>✕ Close</button>
        </div>
        {children}
      </div>
    </div>
  );
}

// PDF/image viewer pop-up (opened by clicking an invoice number).
export function PdfModal({ file, page, pageEnd, onClose }) {
  // A vendor can print more than one invoice in a single PDF (e.g. 2
  // invoices, 1 per page, all sharing one uploaded file), or one invoice
  // can itself span several pages - every row for the SAME file_name
  // otherwise looks identical, so without a page (range) the viewer would
  // always open at page 1 (and a save/download from it would include
  // every page, or miss the invoice's own later pages) regardless of
  // which invoice's row was actually clicked. Passing `page`/`pageEnd`
  // through to invoicePdfUrl has the backend extract and serve just that
  // page range instead of the whole file (see app.py's invoice_pdf) - so
  // both viewing and downloading are already scoped to the right
  // invoice, no client-side page-jump needed.
  return (
    <Modal title={file} onClose={onClose} width={1100}>
      <div style={{ height: "75vh" }}>
        <iframe title={file} src={invoicePdfUrl(file, page, pageEnd)}
                style={{ width: "100%", height: "100%", border: "none", borderRadius: 8 }} />
      </div>
    </Modal>
  );
}

// The dialog UI behind confirmDialog() below - a small themed card, not the
// browser's native confirm() box.
function ConfirmDialog({ message, confirmLabel, cancelLabel, danger, onResolve }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onResolve(false); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onResolve]);

  return (
    <div onClick={() => onResolve(false)}
         style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,.55)", zIndex: 2000,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  padding: 16, animation: "piips-fade-in .15s ease-out" }}>
      <div onClick={(e) => e.stopPropagation()} className="card"
           style={{ width: "min(380px, 100%)", margin: 0, textAlign: "center",
                    animation: "piips-pop-in .15s ease-out" }}>
        <div style={{ fontSize: 34, lineHeight: 1, marginBottom: 4 }}>
          {danger ? "⚠️" : "❓"}
        </div>
        <div style={{ color: "var(--text)", fontSize: 15, lineHeight: 1.5, margin: "10px 0 18px" }}>
          {String(message).split("\n").map((line, i) => <div key={i}>{line}</div>)}
        </div>
        <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
          <button className="btn btn-subtle" onClick={() => onResolve(false)} autoFocus>
            {cancelLabel}
          </button>
          <button className={`btn ${danger ? "btn-danger" : "btn-primary"}`} onClick={() => onResolve(true)}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// Promise-based replacement for window.confirm(message) - same call shape
// (await it, treat the result as a boolean) but renders the app's own
// themed dialog instead of the browser's native box. Mounts itself into a
// throwaway DOM node and cleans up after resolving.
// Sample: if (!(await confirmDialog("Delete this template?"))) return;
export function confirmDialog(message, { confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false } = {}) {
  return new Promise((resolve) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const cleanup = (result) => {
      root.unmount();
      host.remove();
      resolve(result);
    };
    root.render(
      <ConfirmDialog message={message} confirmLabel={confirmLabel} cancelLabel={cancelLabel}
                     danger={danger} onResolve={cleanup} />
    );
  });
}

const READ_KEY = "piips_read_announcements";

function loadRead() {
  try { return new Set(JSON.parse(localStorage.getItem(READ_KEY)) || []); }
  catch { return new Set(); }
}

// Dispatched by Announcement.jsx right after a successful publish/stop, so
// the Super Admin who just acted sees the bell update immediately instead
// of waiting out the poll interval below.
export const ANNOUNCEMENT_CHANGED_EVENT = "piips:announcements-changed";

// Bell icon (topbar, next to the theme dropdown) - every logged-in user
// gets a red dot when a new site-wide announcement (Super Admin >
// Announcement) is active. Clicking it opens a pop-up with the content;
// it never disturbs the page layout the way an inline banner would.
// Announcements stay visible in the pop-up (re-openable) until their end
// date/time passes or a Super Admin stops them - the dot just tracks
// whether THIS browser has opened the bell since the announcement went up.
// There's no push/realtime channel, so a short poll is what makes it show
// up for everyone ELSE without them refreshing.
export function AnnouncementBell() {
  const [items, setItems] = useState([]);
  const [read, setRead] = useState(loadRead);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const load = () => getActiveAnnouncements().then((r) => setItems(r.announcements || [])).catch(() => {});
    load();
    const id = setInterval(load, 15_000);
    window.addEventListener(ANNOUNCEMENT_CHANGED_EVENT, load);
    return () => {
      clearInterval(id);
      window.removeEventListener(ANNOUNCEMENT_CHANGED_EVENT, load);
    };
  }, []);

  const unread = items.filter((a) => !read.has(a.Id));

  const openPopup = () => {
    setOpen(true);
    const next = new Set(read);
    items.forEach((a) => next.add(a.Id));
    setRead(next);
    localStorage.setItem(READ_KEY, JSON.stringify([...next]));
  };

  return (
    <>
      <button onClick={openPopup} title="Announcements"
              style={{ position: "relative", background: "none", border: "none", cursor: "pointer",
                       fontSize: 19, lineHeight: 1, padding: 6, color: "var(--text)" }}>
        <span style={{
          display: "inline-block", transformOrigin: "50% 0%",
          animation: unread.length > 0 ? "piips-bell-ring 2.4s ease-in-out infinite" : "none",
        }}>
          🔔
        </span>
        {unread.length > 0 && (
          <span style={{
            position: "absolute", top: 0, right: 0, minWidth: 15, height: 15, padding: "0 3px",
            borderRadius: 8, background: "var(--danger)", border: "2px solid var(--surface)",
            color: "#fff", fontSize: 10, fontWeight: 700, lineHeight: "11px", textAlign: "center",
            animation: "piips-badge-pulse 1.6s ease-in-out infinite",
          }}>
            {unread.length > 9 ? "9+" : unread.length}
          </span>
        )}
      </button>

      {open && (
        <Modal title="📣 Announcements" onClose={() => setOpen(false)} width={520}>
          {items.length === 0 ? (
            <div className="empty">No active announcements.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {items.map((a) => (
                <div key={a.Id} style={{ borderLeft: "4px solid #7c3aed", paddingLeft: 14 }}>
                  <div style={{ fontWeight: 700 }}>{a.Title}</div>
                  {a.BodyText && (
                    <div style={{ color: "var(--muted)", fontSize: 13.5, whiteSpace: "pre-wrap", marginTop: 4 }}>
                      {a.BodyText}
                    </div>
                  )}
                  {a.ImagePath && (
                    <img src={announcementImageUrl(a.ImagePath)} alt=""
                         style={{ maxWidth: "100%", maxHeight: 260, marginTop: 8, borderRadius: 8, display: "block" }} />
                  )}
                  {a.VideoUrl && (
                    <div style={{ marginTop: 8 }}>
                      <a href={a.VideoUrl} target="_blank" rel="noreferrer" className="btn-link">▶ Watch video</a>
                    </div>
                  )}
                  <div className="hint" style={{ marginTop: 6 }}>Until {a.EndDateTime}</div>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
    </>
  );
}
