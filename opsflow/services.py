"""Serviciile externe: curier (cotație de livrare), CRM, notificări, e-mail.

Fiecare serviciu are o variantă „fake” (în memorie, folosită în teste și
în demo) și, unde are sens, o variantă reală prin HTTP. Toate primesc o
cheie de idempotență: dacă același apel vine de două ori cu aceeași cheie,
serviciul nu repetă efectul.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol

# ---------- curier ----------

ZONES = {"Chișinău": (0, 1), "Bălți": (0, 1)}  # oraș: (cost extra, zile)


class ShippingAPI(Protocol):
    def quote(self, city: str, items: int, idempotency_key: str) -> dict[str, Any]: ...


@dataclass
class FakeShippingAPI:
    calls: list[str] = field(default_factory=list)
    fail_times: int = 0  # câte apeluri să pice înainte să meargă (pentru teste de retry/crash)

    def quote(self, city: str, items: int, idempotency_key: str) -> dict[str, Any]:
        self.calls.append(idempotency_key)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("serviciul de curierat nu răspunde")
        extra, days = ZONES.get(city, (60, 2))
        return {"carrier": "TechNova Express" if city in ZONES else "Nova Poșta",
                "price": 50 + extra + 5 * max(0, items - 5), "eta_days": days}


@dataclass
class HttpShippingAPI:  # pragma: no cover - cere un serviciu real
    base_url: str
    timeout: float = 10.0

    def quote(self, city: str, items: int, idempotency_key: str) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/quotes", method="POST",
            data=json.dumps({"city": city, "items": items}).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())


# ---------- CRM ----------

@dataclass
class FakeCRM:
    """CRM în memorie care respectă Idempotency-Key, ca un API serios."""

    activities: list[dict[str, Any]] = field(default_factory=list)
    seen_keys: set[str] = field(default_factory=set)
    crash_after_write: int = 0  # simulează: CRM-ul a scris, dar procesul nostru a căzut înainte de răspuns

    def log_activity(self, customer: str, kind: str, summary: str, idempotency_key: str) -> dict[str, Any]:
        if idempotency_key not in self.seen_keys:
            self.seen_keys.add(idempotency_key)
            self.activities.append({"customer": customer, "kind": kind, "summary": summary})
        if self.crash_after_write > 0:
            self.crash_after_write -= 1
            raise TimeoutError("conexiunea a căzut după scriere")
        return {"activity_id": len(self.activities)}


# ---------- notificări ----------

class Notifier(Protocol):
    def send(self, text: str, idempotency_key: str) -> None: ...


@dataclass
class MemoryNotifier:
    messages: list[str] = field(default_factory=list)

    def send(self, text: str, idempotency_key: str) -> None:
        self.messages.append(text)


@dataclass
class ConsoleNotifier:
    def send(self, text: str, idempotency_key: str) -> None:
        print(f"🔔 {text}")


@dataclass
class TelegramNotifier:  # pragma: no cover - cere token real
    token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))

    def send(self, text: str, idempotency_key: str) -> None:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", method="POST",
            data=json.dumps({"chat_id": self.chat_id, "text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()


# ---------- e-mail către client ----------

@dataclass
class OutboxMailer:
    """Scrie răspunsurile ca fișiere .eml într-un dosar (în producție: SMTP / API de e-mail)."""

    folder: Path | None = None
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, to: str, subject: str, body: str, idempotency_key: str, in_reply_to: str = "") -> None:
        msg = EmailMessage()
        msg["From"] = "comenzi@technova.md"
        msg["To"] = to
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = f"<{in_reply_to}>"
        msg["X-Idempotency-Key"] = idempotency_key
        msg.set_content(body)
        self.sent.append(msg)
        if self.folder is not None:
            self.folder.mkdir(parents=True, exist_ok=True)
            safe = idempotency_key.replace(":", "_").replace("/", "_")
            (self.folder / f"{safe}.eml").write_bytes(bytes(msg))
