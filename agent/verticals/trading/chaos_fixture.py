"""The chaos suite's fixture for the TRADING vertical.

`core/chaos.py` injects failures — a severed database, a revoked model, a FloodWait — and
asserts the system survives them. The message and outcome it pushes through only have to
be REALISTIC, and what realistic means is this vertical's to say. Keeping them here is
what lets `core/chaos.py` contain no market vocabulary at all.

The company name is a fixture name that appears nowhere in the archive, so a chaos run
can never be confused with a real alert.
"""

CO = "Zylphara Speciality"

TEXT = (f"{CO} IPO\nOpening Date : 13 Feb 2024\nClosing Date : 15 Feb 2024\n"
        "Price Band : 141 to 151\nLot Size : 99")

OUTCOME = {
    "action": "notify",
    "agent": "ipo",
    "headline": f"IPO: {CO}",
    "fields": {"company": CO, "price_band": "141 to 151", "lot_size": "99"},
    "facts": ["The issue opens on 13 Feb and closes on 15 Feb."],
    "caveats": ["You could lose money."],
    "confidence": 0.9,
    "reason": "chaos fixture",
}
