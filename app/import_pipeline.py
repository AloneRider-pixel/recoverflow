from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from frictionless import Resource


ALIASES = {
    "invoice no": "invoice_number",
    "invoice no.": "invoice_number",
    "invoice number": "invoice_number",
    "invoice_number": "invoice_number",
    "invoice #": "invoice_number",
    "customer": "customer_name",
    "customer name": "customer_name",
    "customer_name": "customer_name",
    "party name": "customer_name",
    "amount": "amount",
    "invoice amount": "amount",
    "total": "amount",
    "total amount": "amount",
    "issue date": "issue_date",
    "invoice date": "issue_date",
    "due date": "due_date",
    "due_date": "due_date",
    "phone": "phone",
    "mobile": "phone",
    "contact": "phone",
    "email": "email",
    "email address": "email",
    "paid": "paid_amount",
    "paid amount": "paid_amount",
    "paid_amount": "paid_amount",
    "gstin": "gstin",
    "gst number": "gstin",
    "place of supply": "place_of_supply",
    "tax rate": "tax_rate",
    "tax amount": "tax_amount",
    "tds": "tds_amount",
    "tds amount": "tds_amount",
}

REQUIRED = {"invoice_number", "customer_name", "amount", "issue_date", "due_date"}


def _key(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"invalid date '{raw}'")


def _decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    raw = str(value if value is not None else "").strip().replace(",", "").replace("₹", "")
    if not raw:
        return default
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount '{raw}'") from exc


def read_tabular_upload(filename: str, payload: bytes) -> tuple[list[dict], list[str]]:
    suffix = os.path.splitext(filename.lower())[1]
    if suffix not in {".csv", ".xlsx", ".xls"}:
        raise ValueError("Upload a CSV or Excel workbook (.csv, .xlsx, .xls).")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(payload)
        path = tmp.name

    try:
        resource = Resource(path=path)
        report = resource.validate()
        structural_errors = []
        for task in report.tasks or []:
            for error in task.get("errors", [])[:20]:
                structural_errors.append(str(error.get("note") or error.get("message") or error))

        if not report.valid:
            raise ValueError(
                "Import validation failed. "
                + ("; ".join(structural_errors[:5]) if structural_errors else "Please check the file structure.")
            )

        raw_rows = list(resource.read_rows())
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if not raw_rows:
        raise ValueError("The uploaded file has no data rows.")

    rows: list[dict] = []
    source_keys = set()
    for raw in raw_rows:
        normalized = {}
        for key, value in raw.items():
            canon = ALIASES.get(_key(key))
            if canon:
                normalized[canon] = value
                source_keys.add(canon)

        if not any(str(v or "").strip() for v in normalized.values()):
            continue

        rows.append(normalized)

    missing = REQUIRED - source_keys
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(sorted(missing)))

    clean: list[dict] = []
    errors: list[str] = []
    for idx, row in enumerate(rows, start=2):
        try:
            invoice_number = str(row.get("invoice_number") or "").strip()
            customer_name = str(row.get("customer_name") or "").strip()
            amount = _decimal(row.get("amount"))
            paid = _decimal(row.get("paid_amount"))
            issue_date = _date(row.get("issue_date"))
            due_date = _date(row.get("due_date"))

            if not invoice_number:
                raise ValueError("invoice number is blank")
            if not customer_name:
                raise ValueError("customer name is blank")
            if amount <= 0:
                raise ValueError("amount must be positive")
            if paid < 0 or paid > amount:
                raise ValueError("paid amount must be between 0 and amount")

            clean.append(
                {
                    "invoice_number": invoice_number,
                    "customer_name": customer_name,
                    "amount": amount,
                    "paid_amount": paid,
                    "issue_date": issue_date,
                    "due_date": due_date,
                    "phone": str(row.get("phone") or "").strip() or None,
                    "email": str(row.get("email") or "").strip() or None,
                    "gstin": str(row.get("gstin") or "").strip() or None,
                    "place_of_supply": str(row.get("place_of_supply") or "").strip() or None,
                    "tax_rate": _decimal(row.get("tax_rate"), None) if row.get("tax_rate") not in (None, "") else None,
                    "tax_amount": _decimal(row.get("tax_amount"), None) if row.get("tax_amount") not in (None, "") else None,
                    "tds_amount": _decimal(row.get("tds_amount"), None) if row.get("tds_amount") not in (None, "") else None,
                }
            )
        except (ValueError, InvalidOperation) as exc:
            errors.append(f"Row {idx}: {exc}")

    return clean, errors
