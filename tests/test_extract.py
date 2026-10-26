import json
from datetime import date
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from opsflow.extract import ClaudeExtractor, PurchaseOrder, RuleExtractor, parse_email


def test_parse_email_headers_and_unicode(sample):
    e = parse_email(sample("02"))
    assert e.sender == "it@vitamed.md" and e.sender_domain == "vitamed.md"
    assert e.message_id == "vm0931@vitamed.md"
    assert e.received == date(2026, 10, 19)
    assert "Chișinău" in e.body


def test_rule_extractor_full_order(sample):
    po = RuleExtractor().extract(parse_email(sample("01")), [])
    assert po.customer_idno == "1003600012345"
    assert po.po_number == "PO-2026-118"
    assert {(l.sku, l.qty) for l in po.lines} == {("KIT-LOGI", 5), ("MON-DL27", 2)}
    assert po.delivery_city == "Bălți"
    assert po.requested_date == date(2026, 10, 23)


def test_rule_extractor_sku_colon_format(sample):
    po = RuleExtractor().extract(parse_email(sample("02")), [])
    assert {(l.sku, l.qty) for l in po.lines} == {("LAP-TP14", 6), ("KIT-LOGI", 6)}


def test_rule_extractor_does_not_invent_po_from_words(sample):
    po = RuleExtractor().extract(parse_email(sample("07")), [])
    assert po.po_number is None and po.customer_idno is None


def test_rule_extractor_cannot_read_natural_language(sample):
    assert RuleExtractor().extract(parse_email(sample("08")), []).lines == []


def test_idno_must_have_13_digits():
    with pytest.raises(ValidationError):
        PurchaseOrder(customer_idno="123")


def test_quantity_bounds():
    with pytest.raises(ValidationError):
        PurchaseOrder(lines=[{"sku": "KIT-LOGI", "qty": 0}])


class FakeClient:
    def __init__(self, payload):
        self.payload, self.params = payload, None
        self.messages = self

    def create(self, **params):
        self.params = params
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(self.payload))])


def test_claude_extractor_reads_natural_language(sample):
    client = FakeClient({"customer_idno": "1012600098765", "po_number": "VM-0952",
                         "lines": [{"sku": "PRN-HP4103", "qty": 3}, {"sku": "KIT-LOGI", "qty": 10}],
                         "delivery_city": "Chișinău", "requested_date": None, "notes": ""})
    catalog = [{"sku": "PRN-HP4103", "name": "Imprimantă HP"}, {"sku": "KIT-LOGI", "name": "Kit Logitech"}]
    po = ClaudeExtractor(client=client).extract(parse_email(sample("08")), catalog)
    assert {(l.sku, l.qty) for l in po.lines} == {("PRN-HP4103", 3), ("KIT-LOGI", 10)}
    system = client.params["system"][0]
    assert "PRN-HP4103" in system["text"] and system["cache_control"] == {"type": "ephemeral"}
    schema = client.params["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False


def test_claude_extractor_output_is_still_validated(sample):
    client = FakeClient({"customer_idno": "999", "po_number": None, "lines": [], "delivery_city": None,
                         "requested_date": None, "notes": ""})
    with pytest.raises(ValidationError):
        ClaudeExtractor(client=client).extract(parse_email(sample("08")), [])
