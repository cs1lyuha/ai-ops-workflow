"""E-mail → date structurate.

Două extractoare cu aceeași interfață:
- `RuleExtractor`: expresii regulate, determinist, fără cost. Înțelege doar
  e-mailurile care folosesc coduri SKU („5 x KIT-LOGI”).
- `ClaudeExtractor`: înțelege și limbaj natural („trei imprimante HP”),
  primind catalogul în prompt. Output-ul e JSON validat cu Pydantic.

Extractorul NU decide nimic: doar citește. Validarea, creditul și stocul se
verifică în cod, în nodurile următoare.
"""

from __future__ import annotations

import json
import re
from datetime import date
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator


class Email(BaseModel):
    message_id: str
    sender: str
    subject: str
    body: str
    received: date | None = None

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower()


class OrderLine(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=1000)

    @field_validator("sku")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper()


class PurchaseOrder(BaseModel):
    customer_idno: str | None = None
    po_number: str | None = None
    lines: list[OrderLine] = Field(default_factory=list)
    delivery_city: str | None = None
    requested_date: date | None = None
    notes: str = ""

    @field_validator("customer_idno")
    @classmethod
    def idno_format(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"\d{13}", v):
            raise ValueError("IDNO trebuie să aibă 13 cifre")
        return v


def parse_email(raw: str | bytes) -> Email:
    # parsăm din bytes: așa charset-ul din antet decodează corect diacriticele
    msg = message_from_bytes(raw.encode("utf-8") if isinstance(raw, str) else raw, policy=policy.default)
    body_part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
    body = body_part.get_content() if body_part is not None else ""
    return Email(
        message_id=(msg.get("Message-ID") or "").strip("<> ") or "fara-id",
        sender=parseaddr(msg.get("From", ""))[1].lower(),
        subject=str(msg.get("Subject", "")),
        body=body.strip(),
        received=_received(msg.get("Date")),
    )


def _received(header: str | None) -> date | None:
    if not header:
        return None
    try:
        return parsedate_to_datetime(header).date()
    except (TypeError, ValueError):
        return None


class Extractor(Protocol):
    name: str

    def extract(self, email: Email, catalog: list[dict[str, Any]]) -> PurchaseOrder: ...


LINE_RE = re.compile(r"(\d{1,4})\s*(?:x|×|buc\.?|bucăți)\s*([A-Z]{3}-[A-Z0-9]{2,10})", re.I)
LINE_RE_REV = re.compile(r"([A-Z]{3}-[A-Z0-9]{2,10})[ \t]*[:–-][ \t]*(\d{1,4})", re.I)
IDNO_RE = re.compile(r"\b(\d{13})\b")
PO_RE = re.compile(r"\b(?:PO|comanda(?: de achiziție)?(?: nr\.?)?)[ \t:#-]*([A-Z0-9][A-Z0-9/-]{2,20})", re.I)
CITY_RE = re.compile(r"livrare[ \t]*(?:la|în|:)[ \t]*((?-i:[A-ZĂÂÎȘȚ][\w-]+(?: [A-ZĂÂÎȘȚ][\w-]+)?))", re.I)
DATE_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")


class RuleExtractor:
    name = "rules"

    def extract(self, email: Email, catalog: list[dict[str, Any]]) -> PurchaseOrder:
        text = f"{email.subject}\n{email.body}"
        lines: dict[str, int] = {}
        for qty, sku in LINE_RE.findall(text):
            lines[sku.upper()] = lines.get(sku.upper(), 0) + int(qty)
        for sku, qty in LINE_RE_REV.findall(text):
            if sku.upper() not in lines:
                lines[sku.upper()] = int(qty)
        idno = IDNO_RE.search(text)
        po = next((m for m in PO_RE.finditer(text) if any(ch.isdigit() for ch in m.group(1))), None)
        city = CITY_RE.search(text)
        when = None
        if (m := DATE_RE.search(text)):
            try:
                when = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                when = None
        return PurchaseOrder(
            customer_idno=idno.group(1) if idno else None,
            po_number=po.group(1).upper() if po else None,
            lines=[OrderLine(sku=s, qty=q) for s, q in lines.items()],
            delivery_city=city.group(1) if city else None,
            requested_date=when,
        )


SYSTEM = """Extragi comenzi B2B din e-mailuri pentru TechNova.
Reguli:
- Folosește DOAR codurile SKU din catalog. Dacă un produs cerut nu se potrivește clar cu un SKU, nu-l inventa:
  pune-l în `notes`.
- Nu completa câmpurile care lipsesc din e-mail (IDNO, număr PO, dată): lasă-le null.
- Data în format ISO (YYYY-MM-DD)."""


class ClaudeExtractor:
    name = "claude"

    def __init__(self, model: str = "claude-opus-5-5", client: Any = None) -> None:
        if client is None:  # pragma: no cover - doar live
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.model = model

    def extract(self, email: Email, catalog: list[dict[str, Any]]) -> PurchaseOrder:
        catalog_text = "\n".join(f"- {p['sku']}: {p['name']}" for p in catalog)
        schema = PurchaseOrder.model_json_schema()
        message = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=[{"type": "text", "text": f"{SYSTEM}\n\nCatalog:\n{catalog_text}",
                     "cache_control": {"type": "ephemeral"}}],
            output_config={"format": {"type": "json_schema", "schema": _close(schema)}, "effort": "low"},
            messages=[{"role": "user", "content": f"De la: {email.sender}\nSubiect: {email.subject}\n\n{email.body}"}],
        )
        if message.stop_reason == "refusal":
            return PurchaseOrder(notes="extragere refuzată de model")
        text = next(b.text for b in message.content if b.type == "text")
        return PurchaseOrder.model_validate(json.loads(text))


def _close(schema: dict) -> dict:
    """Structured outputs stricte cer additionalProperties: false pe fiecare obiect."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            schema.setdefault("additionalProperties", False)
            schema["required"] = list(schema.get("properties", {}))
        for v in schema.values():
            _close(v)
    elif isinstance(schema, list):
        for v in schema:
            _close(v)
    return schema
