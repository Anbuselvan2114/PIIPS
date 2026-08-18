// PIIPS API client
//
// In production the React build is served behind the same IIS site that
// reverse-proxies to the FastAPI backend, so a relative base ("") works.
// For local development (e.g. Vite/CRA on :3000) point this at the backend,
// e.g. "http://localhost:8000".

const API_BASE = import.meta?.env?.VITE_API_BASE ?? "";

async function request(path, options = {}) {
  const res = await fetch(API_BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = res.statusText;
    }
    const message =
      typeof detail === "string"
        ? detail
        : (detail && detail.message) || JSON.stringify(detail);
    throw new Error(message || `Request failed (${res.status})`);
  }

  return res.json();
}

export const getConfig = () => request("/api/config");

export const saveConfig = (folderPath) =>
  request("/api/config", {
    method: "POST",
    body: JSON.stringify({ folder_path: folderPath }),
  });

export const getApiConfig = () => request("/api/api-config");

export const saveApiConfig = (sfApiUrl) =>
  request("/api/api-config", {
    method: "POST",
    body: JSON.stringify({ sf_api_url: sfApiUrl }),
  });

export const getStatusCounts = () => request("/api/stats/status-counts");

export const getInvoicesByStatus = (statusId) =>
  request(`/api/invoices/by-status?status_id=${encodeURIComponent(statusId)}`);

export const getInvoicesByBatch = (batch) =>
  request(`/api/invoices/by-batch?batch=${encodeURIComponent(batch)}`);

export const getInvoiceFieldCheck = (headerId) =>
  request(`/api/invoices/${encodeURIComponent(headerId)}/fields`);

export const invoicePdfUrl = (file) =>
  `${API_BASE}/api/invoices/pdf?file=${encodeURIComponent(file)}`;

export const setInvoiceExcluded = (header_id, exclude, user_id) =>
  request("/api/invoices/exclude", {
    method: "POST",
    body: JSON.stringify({ header_id, exclude, user_id }),
  });

export const getBuyerOrderMissing = () =>
  request("/api/invoices/buyer-order-missing");

export const setBuyerOrder = (header_id, buyer_order_no, user_id) =>
  request("/api/invoices/buyer-order", {
    method: "POST",
    body: JSON.stringify({ header_id, buyer_order_no, user_id }),
  });

export const getLifecycleInvoices = (stage) =>
  request(`/api/lifecycle/invoices?stage=${encodeURIComponent(stage)}`);

export const advanceLifecycle = (stage, header_ids, user_id) =>
  request("/api/lifecycle/advance", {
    method: "POST",
    body: JSON.stringify({ stage, header_ids, user_id }),
  });

export const getDbConfig = (user_id) =>
  request(`/api/db-config${user_id != null ? `?user_id=${user_id}` : ""}`);

export const saveDbConfig = (payload) =>
  request("/api/db-config", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const startProcessing = (user_id) =>
  request("/api/process/start", {
    method: "POST",
    body: JSON.stringify({ user_id }),
  });

export const startTraining = () =>
  request("/api/train", { method: "POST" });

export const getTrainFiles = () => request("/api/train/files");

export const trainFileUrl = (name) =>
  `${API_BASE}/api/train/file?name=${encodeURIComponent(name)}`;

export const getActiveJob = (mode) =>
  request(`/api/job/active${mode ? `?mode=${mode}` : ""}`);

export const getStatus = (jobId) =>
  request(`/api/process/status/${jobId}`);

export const getResult = (jobId) =>
  request(`/api/process/result/${jobId}`);

export const getBatches = () => request("/api/batches");

export const manualDownloadUrl = (userId, kind = "user") =>
  `${API_BASE}/api/manual/download?user_id=${encodeURIComponent(userId)}&kind=${kind}`;

export const batchDownloadUrl = (batch, docNo, entryNo) => {
  let url = `${API_BASE}/api/batches/download?batch=${encodeURIComponent(batch)}`;
  if (docNo) url += `&doc_no=${encodeURIComponent(docNo)}`;
  if (entryNo) url += `&entry_no=${encodeURIComponent(entryNo)}`;
  return url;
};

export const getFormats = () => request("/api/formats");

export const getMapping = () => request("/api/mapping");

export const saveMapping = (mapping, user_id) =>
  request("/api/mapping", {
    method: "POST",
    body: JSON.stringify({ mapping, user_id }),
  });

export const getFields = () => request("/api/fields");

export const saveFields = (columns, user_id) =>
  request("/api/fields", {
    method: "POST",
    body: JSON.stringify({ columns, user_id }),
  });

export const getTemplates = () => request("/api/templates");

export const saveTemplate = (payload) =>
  request("/api/templates", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const deleteTemplate = (key, user_id) =>
  request("/api/templates/delete", {
    method: "POST",
    body: JSON.stringify({ key, user_id }),
  });

export const login = (username, password) =>
  request("/api/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });

export const forgotPassword = (username_or_email) =>
  request("/api/forgot-password", {
    method: "POST",
    body: JSON.stringify({ username_or_email }),
  });

export const changePassword = (user_id, current_password, new_password) =>
  request("/api/change-password", {
    method: "POST",
    body: JSON.stringify({ user_id, current_password, new_password }),
  });

export const getUsers = () => request("/api/users");

export const createUser = (username, email, user_type_id, created_by) =>
  request("/api/users", {
    method: "POST",
    body: JSON.stringify({ username, email, user_type_id, created_by }),
  });

export const setUserActive = (user_id, is_active, modified_by) =>
  request("/api/users/active", {
    method: "POST",
    body: JSON.stringify({ user_id, is_active, modified_by }),
  });

export const adminResetPassword = (user_id, target_user_id) =>
  request("/api/users/reset-password", {
    method: "POST",
    body: JSON.stringify({ user_id, target_user_id }),
  });

export const getMailSettings = (user_id) =>
  request(`/api/mail-settings${user_id != null ? `?user_id=${user_id}` : ""}`);

export const saveMailSettings = (payload) =>
  request("/api/mail-settings", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const clearFormats = () =>
  request("/api/formats", { method: "DELETE" });

const sub = (subpath) =>
  subpath ? `?subpath=${encodeURIComponent(subpath)}` : "";

export const getInputFiles = (subpath = "") =>
  request(`/api/input/files${sub(subpath)}`);

export const uploadInputFiles = async (fileList, subpath = "", user_id) => {
  const form = new FormData();
  for (const f of fileList) form.append("files", f);

  const q = sub(subpath);
  const url =
    `/api/input/upload${q}` +
    (user_id != null ? `${q ? "&" : "?"}user_id=${user_id}` : "");

  const res = await fetch(API_BASE + url, {
    method: "POST",
    body: form, // let the browser set the multipart Content-Type/boundary
  });

  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = res.statusText;
    }
    throw new Error(
      (typeof detail === "string" ? detail : detail?.message) ||
        `Upload failed (${res.status})`
    );
  }
  return res.json();
};

export const getPublishConfig = (user_id) =>
  request(`/api/publish-config${user_id != null ? `?user_id=${user_id}` : ""}`);

export const savePublishConfig = (publish_root, user_id) =>
  request("/api/publish-config", {
    method: "POST",
    body: JSON.stringify({ publish_root, user_id }),
  });

export const publishDeploy = (environment, user_id) =>
  request("/api/deploy/publish", {
    method: "POST",
    body: JSON.stringify({ environment, user_id }),
  });

export const getBackups = () => request("/api/backups");

export const restoreBackup = (name) =>
  request("/api/backups/restore", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
