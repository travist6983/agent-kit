// Total of an Invoice in cents: line items, minus the discount, plus tax on the rest.
export function invoiceTotal(invoice) {
  const subtotal = invoice.lines.reduce((sum, line) => sum + line.qty * line.unitCents, 0);
  const discount = invoice.discountCents || 0;
  const tax = Math.round(subtotal * invoice.taxRate);
  return subtotal - discount + tax;
}
