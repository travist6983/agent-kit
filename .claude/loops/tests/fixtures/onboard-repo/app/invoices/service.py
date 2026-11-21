"""Invoice rules. The service owns every state change; the store only persists."""


class InvoiceError(ValueError):
    pass


class InvoiceService:
    def __init__(self, store):
        self.store = store

    def create(self, customer, cents):
        if cents <= 0:
            raise InvoiceError("amount must be positive")
        return self.store.insert({"customer": customer, "cents": cents, "status": "open"})

    def pay(self, invoice_id):
        inv = self.store.get(invoice_id)
        if inv["status"] != "open":
            raise InvoiceError("only an open invoice can be paid")
        inv["status"] = "paid"
        self.store.update(invoice_id, inv)
        return inv

    def total_due(self):
        return sum(i["cents"] for i in self.store.all() if i["status"] == "open")
