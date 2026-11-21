"""Entry point: wires the store into the invoice service and runs one demo command."""
import sys

from app.invoices.service import InvoiceService
from app.invoices.store import MemoryStore


def build_service():
    return InvoiceService(MemoryStore())


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    svc = build_service()
    inv = svc.create(customer=argv[0] if argv else "demo", cents=1200)
    print(svc.total_due())
    return inv


if __name__ == "__main__":
    main()
