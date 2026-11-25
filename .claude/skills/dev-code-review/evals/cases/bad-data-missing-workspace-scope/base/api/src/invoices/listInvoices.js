const db = require("../lib/db");

// Lists a Workspace's Invoices, newest first.
async function listInvoices(workspaceId, { limit = 50 } = {}) {
  return db.query(
    "SELECT id, number, status, issued_at FROM invoices WHERE workspace_id = $1 ORDER BY id DESC LIMIT $2",
    [workspaceId, limit]
  );
}

module.exports = { listInvoices };
