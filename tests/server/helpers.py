"""Shared constants for server tests."""

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"


def order(oid, total=100.0, status="delivered", items=None):
    return {"order_id": oid, "placed_at": "2025-01-05T10:00:00", "total_amount": total, "currency": "INR",
            "status": status, "merchant": "Store", "items": items or [], "raw": {"id": oid}}
