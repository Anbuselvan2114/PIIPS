import { useEffect, useState } from "react";
import { getAnnouncements, createAnnouncement, stopAnnouncement, announcementImageUrl } from "./api";
import { DataTable, confirmDialog, ANNOUNCEMENT_CHANGED_EVENT } from "./components";

const notifyBell = () => window.dispatchEvent(new CustomEvent(ANNOUNCEMENT_CHANGED_EVENT));

const EMPTY_FORM = { title: "", body_text: "", video_url: "", end_datetime: "", image: null };

export default function Announcement({ user }) {
  const [items, setItems] = useState([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [message, setMessage] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const refresh = () =>
    getAnnouncements(user?.user_id)
      .then((r) => setItems(r.announcements || []))
      .catch((e) => setMessage({ ok: false, text: e.message }))
      .finally(() => setLoading(false));

  useEffect(() => { refresh(); }, []);

  const statusOf = (a) => {
    if (!a.IsActive) return a.StoppedDatetime ? "Stopped" : "Inactive";
    return new Date(a.EndDateTime) > new Date() ? "Active" : "Expired";
  };

  const onCreate = async () => {
    setMessage(null);
    if (!form.title.trim() || !form.end_datetime) {
      setMessage({ ok: false, text: "Title and end date/time are required." });
      return;
    }
    setSaving(true);
    try {
      await createAnnouncement({ ...form, user_id: user?.user_id });
      setMessage({ ok: true, text: "Announcement published to all users." });
      setForm(EMPTY_FORM);
      refresh();
      notifyBell();
    } catch (e) {
      setMessage({ ok: false, text: e.message });
    } finally {
      setSaving(false);
    }
  };

  const onStop = async (a) => {
    if (!(await confirmDialog(`Stop the announcement "${a.Title}" now?`, { confirmLabel: "Stop", danger: true }))) return;
    try {
      await stopAnnouncement(a.Id, user?.user_id);
      refresh();
      notifyBell();
    } catch (e) { setMessage({ ok: false, text: e.message }); }
  };

  if (loading) return <div className="page"><div className="card">Loading…</div></div>;

  return (
    <div className="page">
      <div className="card">
        <h3>New announcement</h3>
        <p className="hint">
          Shown to every logged-in user as a notification banner until the end
          date/time passes, or you stop it early below. Content can be plain
          text, an image (a screenshot, a QR code, whatever), a video link, or
          any combination.
        </p>
        <div className="row" style={{ flexDirection: "column", alignItems: "stretch", maxWidth: 480, gap: 14 }}>
          <div className="field">
            <label className="label">Title</label>
            <input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })}
                   placeholder="Scheduled maintenance" />
          </div>
          <div className="field">
            <label className="label">Message</label>
            <textarea value={form.body_text} onChange={(e) => setForm({ ...form, body_text: e.target.value })}
                      placeholder="PIIPS will be unavailable tonight from 10 PM to 11 PM for maintenance." />
          </div>
          <div className="field">
            <label className="label">Image (optional)</label>
            <input type="file" accept="image/png,image/jpeg,image/gif,image/webp"
                   onChange={(e) => setForm({ ...form, image: e.target.files?.[0] || null })} />
          </div>
          <div className="field">
            <label className="label">Video URL (optional)</label>
            <input value={form.video_url} onChange={(e) => setForm({ ...form, video_url: e.target.value })}
                   placeholder="https://..." />
          </div>
          <div className="field">
            <label className="label">Show until</label>
            <input type="datetime-local" value={form.end_datetime}
                   onChange={(e) => setForm({ ...form, end_datetime: e.target.value })} />
          </div>
          <button className="btn btn-primary" onClick={onCreate} disabled={saving} style={{ alignSelf: "flex-start" }}>
            {saving ? "Publishing…" : "📣 Publish announcement"}
          </button>
        </div>
        {message && <div className={`alert ${message.ok ? "alert-success" : "alert-danger"}`}>{message.text}</div>}
      </div>

      <div className="card">
        <h3>All announcements ({items.length})</h3>
        <DataTable
          rows={items.map((a) => ({
            _key: a.Id, title: a.Title, until: a.EndDateTime, status: statusOf(a),
            created: a.CreatedDatetime, _a: a,
          }))}
          searchKeys={["title", "status"]}
          empty="No announcements yet."
          columns={[
            { key: "title", label: "Title",
              render: (r) => (
                <div>
                  <div style={{ fontWeight: 600 }}>{r.title}</div>
                  {r._a.ImagePath && (
                    <img src={announcementImageUrl(r._a.ImagePath)} alt="" style={{ height: 28, marginTop: 4, borderRadius: 4 }} />
                  )}
                </div>
              ) },
            { key: "status", label: "Status",
              render: (r) => (
                <span className={`badge ${r.status === "Active" ? "badge-success" : "badge-danger"}`}>{r.status}</span>
              ) },
            { key: "until", label: "Shows until" },
            { key: "created", label: "Created" },
            { key: "_action", label: "Action", sortable: false,
              render: (r) => r.status === "Active" ? (
                <button className="btn btn-sm btn-danger" onClick={() => onStop(r._a)}>Stop</button>
              ) : null },
          ]} />
      </div>
    </div>
  );
}
