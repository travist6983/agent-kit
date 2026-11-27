const db = require("../lib/db");

async function findInvoice(workspaceId, invoiceId) {
  const rows = await db.query(
    "SELECT * FROM invoices WHERE workspace_id = $1 AND id = $2",
    [workspaceId, invoiceId]
  );
  return rows[0] || null;
}

module.exports = { findInvoice };
