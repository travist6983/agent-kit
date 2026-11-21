"""Member invites. Validation happens here, before anything reaches a store."""
import re

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def invite(email):
    if not EMAIL.match(email or ""):
        raise ValueError("malformed email")
    return {"email": email.lower(), "status": "invited"}
