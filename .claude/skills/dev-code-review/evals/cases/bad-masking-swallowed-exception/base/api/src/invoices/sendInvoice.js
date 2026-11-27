const { mailer } = require("../lib/mailer");
const { findInvoice } = require("./invoiceStore");

// Sends an Invoice to the billing contact of the Member who owns it.
async function sendInvoice(workspaceId, invoiceId) {
  const invoice = await findInvoice(workspaceId, invoiceId);
  if (!invoice) {
    return { ok: false, error: "not-found" };
  }
  await mailer.send({
    to: invoice.billingEmail,
    subject: `Invoice ${invoice.number}`,
    template: "invoice",
    data: { invoice },
  });
  return { ok: true };
}

module.exports = { sendInvoice };
