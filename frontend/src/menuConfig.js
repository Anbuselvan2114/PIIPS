// Every menu key/label/icon/nav-group in the app, in sidebar order. The
// single source of truth for both App.jsx (which menus exist, in what
// order/group) and RoleMenuAccess.jsx (the Screen Access matrix, which
// needs this same list without importing App.jsx itself - keeping it here
// avoids a circular import between the two).
export const MENU = [
  ["dashboard", "Dashboard", "▤", "Main"],
  ["input", "File Explorer", "🗂", "Main"],
  ["manual", "Manual", "📖", "Main"],
  ["buyerorder", "Buyer Order Entry", "✎", "Review & Update"],
  ["partdescupdate", "Part Description Mapping", "📝", "Review & Update"],
  ["load", "Load", "📥", "Review & Update"],
  ["post", "Post", "📮", "Accounts"],
  ["complete", "Complete", "✅", "Accounts"],
  ["configuration", "Folder Configuration", "⚙", "Setup"],
  ["dbconfig", "Database Configuration", "🗄", "Setup"],
  ["apiconfig", "API Configuration", "🔌", "Setup"],
  ["template", "Template", "🧩", "Setup"],
  ["createfield", "Create Field", "✚", "Mapping"],
  ["mapping", "Field Mapping", "🔗", "Mapping"],
  ["training", "Model Training", "🧠", "Admin"],
  ["users", "User Management", "👤", "Admin"],
  ["mailsettings", "Mail Server Setting", "✉", "Admin"],
  ["announcement", "Announcement", "📣", "Admin"],
  ["publish", "Publish", "🚀", "Admin"],
  ["rolemenus", "Screen Access", "🔐", "Admin"],
];

// Menu keys that are never configurable on the Screen Access matrix - a
// Super Admin/Developer always sees every menu (App.jsx enforces this
// regardless of tbl_RoleMenu's contents), so offering rows for those would
// be meaningless. "rolemenus" itself is excluded too: it must never be
// grantable to a lower-privileged role, since it controls who can reach
// every other screen (including itself).
export const ROLE_MENU_EXCLUDED_KEYS = ["rolemenus"];

// The four roles a Super Admin can configure. Order shown on the matrix.
export const CONFIGURABLE_ROLES = [
  { key: "admin", label: "Admin" },
  { key: "user", label: "User" },
  { key: "accounts", label: "Accounts" },
  { key: "viewer", label: "Viewer" },
];
