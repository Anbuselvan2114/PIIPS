import { manualDownloadUrl } from "./api";

export default function Manuals({ user }) {
  const isSuperAdmin = ["super admin", "developer"].includes(
    (user?.user_type || "").toLowerCase()
  );

  return (
    <div className="page">
      <div className="card" style={{ maxWidth: 640 }}>
        <h3>Manuals</h3>
        <p className="hint">
          Downloaded PDFs are stamped with your username, role and the download date.
        </p>

        <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 0", borderTop: "1px solid var(--border)" }}>
          <span className="ico" style={{ fontSize: 26 }}>📖</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 600 }}>User Manual</div>
            <div className="hint" style={{ margin: 0 }}>
              A screen-by-screen guide to the screens available to your role ({user?.user_type}).
            </div>
          </div>
          <a className="btn btn-primary btn-sm" href={manualDownloadUrl(user?.user_id, "user")}>
            Download
          </a>
        </div>

        {isSuperAdmin && (
          <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 0", borderTop: "1px solid var(--border)" }}>
            <span className="ico" style={{ fontSize: 26 }}>🛠</span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600 }}>Developer Manual</div>
              <div className="hint" style={{ margin: 0 }}>
                Full source code, architecture, data flow and database schema. Super Admin only.
              </div>
            </div>
            <a className="btn btn-primary btn-sm" href={manualDownloadUrl(user?.user_id, "developer")}>
              Download
            </a>
          </div>
        )}

        {isSuperAdmin && (
          <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "14px 0", borderTop: "1px solid var(--border)" }}>
            <span className="ico" style={{ fontSize: 26 }}>🚀</span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600 }}>Deployment Guide</div>
              <div className="hint" style={{ margin: 0 }}>
                Manual IIS/Windows Service hosting steps for a UAT or production server. Super Admin only.
              </div>
            </div>
            <a className="btn btn-primary btn-sm" href={manualDownloadUrl(user?.user_id, "deployment")}>
              Download
            </a>
          </div>
        )}
      </div>
    </div>
  );
}
