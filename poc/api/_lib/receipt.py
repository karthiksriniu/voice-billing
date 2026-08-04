"""The document the customer takes away.

What it is allowed to be called depends on what the shop is registered as, and this is not
a cosmetic distinction — a composition dealer who issues a tax invoice with a tax breakup
has committed an offence, and so has an unregistered shop that prints a GST number.

So the rule here is to claim nothing that cannot be substantiated:

  no GSTIN            -> "Receipt". No tax words anywhere.
  GSTIN, no tax rates -> "Bill of Supply". Correct for a composition dealer and for
                         nil-rated or exempt goods, which is what every line currently is,
                         because the catalog holds no HSN code and no GST rate.

A tax invoice needs three things this app does not have yet: a per-SKU HSN code, a per-SKU
GST rate, and the shop's registration scheme. Until those exist, printing CGST and SGST
columns would mean inventing the numbers in them. A bill of supply that is true beats a
tax invoice that is fiction.

Rendering targets 58mm thermal paper, which is 32 characters at Font A on every ESC/POS
printer worth buying. Everything is laid out to that width so what the shopkeeper sees on
screen is what comes out of the roll.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WIDTH = 32
IST = timezone(timedelta(hours=5, minutes=30))


def financial_year(when: datetime) -> str:
    """India runs April to March. A receipt series belongs to one of those, not to a
    calendar year, which is why this is not just `when.year`."""
    y = when.year if when.month >= 4 else when.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def serial(fy: str, n: int) -> str:
    """Rule 46 allows sixteen characters; this uses eleven and stays sortable."""
    return f"{fy}/{n:05d}"


def rupees(n: float) -> str:
    return f"{n:,.2f}"


def build(shop: dict, bill: dict, number: str, when: datetime | None = None) -> dict:
    """The document, as issued. Stored rather than recomputed later — prices move, shops
    are renamed, and a reprint must say what it said on the day."""
    when = (when or datetime.now(IST)).astimezone(IST)
    gstin = (shop.get("gstin") or "").strip()
    items = [i for i in (bill.get("items") or []) if i.get("name")]
    total = round(sum(float(i.get("amount") or 0) for i in items), 2)
    return {
        "kind": "bill_of_supply" if gstin else "receipt",
        "title": "BILL OF SUPPLY" if gstin else "RECEIPT",
        "number": number,
        "issued_at": when.isoformat(),
        "shop": {"name": shop.get("name", ""), "gstin": gstin,
                 "upi": shop.get("upi_vpa", ""), "phone": shop.get("wa_number", "")},
        "customer": {"mobile": bill.get("customer_mobile", "")},
        "items": [{"name": i.get("name", ""), "qty": float(i.get("qty") or 0),
                   "unit": i.get("unit", ""), "rate": float(i.get("unit_price") or 0),
                   "amount": round(float(i.get("amount") or 0), 2)} for i in items],
        "total": total,
        "rounded_total": round(total),
        "payment": {"method": bill.get("payment_method", ""),
                    "state": bill.get("payment_state", "pending"),
                    "upi_ref": bill.get("upi_ref", "")},
        # Said plainly on the document itself. A shopkeeper who thinks this is a tax
        # invoice will hand it to an accountant who will tell them it is not.
        "note": ("No tax charged on this bill of supply." if gstin else ""),
    }


def _line(left: str, right: str) -> str:
    space = WIDTH - len(right)
    return f"{left[:max(space - 1, 0)]:<{max(space, 0)}}{right}"


def _centre(s: str) -> str:
    return s[:WIDTH].center(WIDTH)


def as_text(doc: dict) -> str:
    """58mm, 32 columns, no box drawing — cheap printers render dashes reliably and
    everything else at their own discretion."""
    out: list[str] = []
    shop = doc["shop"]
    out.append(_centre(shop["name"].upper()))
    if shop.get("phone"):
        out.append(_centre(shop["phone"]))
    if shop.get("gstin"):
        out.append(_centre(f"GSTIN {shop['gstin']}"))
    out.append("")
    out.append(_centre(doc["title"]))
    out.append("-" * WIDTH)

    when = datetime.fromisoformat(doc["issued_at"])
    out.append(_line("No", doc["number"]))
    out.append(_line("Date", when.strftime("%d-%m-%Y %H:%M")))
    if doc["customer"].get("mobile"):
        out.append(_line("Customer", doc["customer"]["mobile"]))
    out.append("-" * WIDTH)

    for it in doc["items"]:
        out.append(it["name"][:WIDTH])
        qty = f"{it['qty']:g} {it['unit']}".strip()
        rate = f"x {rupees(it['rate'])}" if it["rate"] else ""
        out.append(_line(f"  {qty} {rate}".rstrip(), rupees(it["amount"])))

    out.append("-" * WIDTH)
    out.append(_line("TOTAL", rupees(doc["total"])))
    if doc["rounded_total"] != doc["total"]:
        out.append(_line("Rounded", rupees(doc["rounded_total"])))

    pay = doc["payment"]
    if pay.get("state") == "confirmed":
        out.append(_line("Paid", (pay.get("method") or "").upper() or "YES"))
    elif pay.get("upi_ref"):
        out.append(_line("UPI ref", pay["upi_ref"]))
    if doc.get("note"):
        out.append("")
        for chunk in _wrap(doc["note"]):
            out.append(chunk)
    out.append("")
    out.append(_centre("Thank you, visit again"))
    return "\n".join(out)


def _wrap(text: str) -> list[str]:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > WIDTH:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def as_whatsapp(doc: dict) -> str:
    """The same document as a message. WhatsApp has no monospace in the compose field that
    survives every client, so this is written as prose rather than as columns that would
    fall apart on half of them."""
    when = datetime.fromisoformat(doc["issued_at"])
    lines = [f"*{doc['shop']['name']}*", f"{doc['title'].title()} {doc['number']}",
             when.strftime("%d-%m-%Y %H:%M"), ""]
    for it in doc["items"]:
        qty = f"{it['qty']:g} {it['unit']}".strip()
        lines.append(f"{it['name']} — {qty} — ₹{rupees(it['amount'])}")
    lines += ["", f"*Total ₹{rupees(doc['total'])}*"]
    if doc["shop"].get("gstin"):
        lines.append(f"GSTIN {doc['shop']['gstin']}")
    if doc.get("note"):
        lines.append(doc["note"])
    lines.append("Thank you, visit again.")
    return "\n".join(lines)
