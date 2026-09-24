"""InvoiceMailbox must address messages by UID and never run a bare EXPUNGE.

Sequence numbers shift as soon as a message leaves the folder. The old code
searched by sequence number and expunged after every move, so the second
"invoice" it moved was a different email, and a bare EXPUNGE also purged
whatever the user had flagged \\Deleted themselves.
"""

from __future__ import annotations

import ssl
from email.message import EmailMessage

import pytest

from finops.connectors.invoice import parser as invoice_parser
from finops.connectors.invoice.parser import InvoiceMailbox


def _raw(subject: str, body: str) -> bytes:
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "billing@vendor.example"
    m["Date"] = "Mon, 01 Sep 2026 10:00:00 +0000"
    m.set_content(body)
    return m.as_bytes()


class FakeImap:
    """A folder of (uid, subject, raw, flags). Plain commands use sequence numbers."""

    def __init__(self, messages, capabilities=("IMAP4REV1",)):
        self.inbox = [
            {"uid": uid, "subject": subj, "raw": raw, "flags": set(flags)}
            for uid, subj, raw, flags in messages
        ]
        self.moved: list[str] = []
        self.capabilities = capabilities
        self.bare_expunges = 0

    def create(self, _folder):
        return "OK", [b""]

    def select(self, _folder):
        return "OK", [str(len(self.inbox)).encode()]

    def search(self, *_a):  # sequence numbers: must not be used
        raise AssertionError("sequence-number SEARCH used")

    def fetch(self, *_a):
        raise AssertionError("sequence-number FETCH used")

    def expunge(self):
        self.bare_expunges += 1
        self.inbox = [m for m in self.inbox if "\\Deleted" not in m["flags"]]
        return "OK", []

    def _by_uids(self, uid_set):
        wanted = {int(u) for u in uid_set.decode().split(",")}
        return [m for m in self.inbox if m["uid"] in wanted]

    def uid(self, command, *args):
        command = command.upper()
        if command == "SEARCH":
            hits = [str(m["uid"]).encode() for m in self.inbox
                    if "invoice" in m["subject"].lower()]
            return "OK", [b" ".join(hits)]
        if command == "FETCH":
            msgs = self._by_uids(args[0])
            return "OK", [(b"1 (RFC822)", msgs[0]["raw"])] if msgs else [None]
        if command == "COPY":
            self.moved += [m["subject"] for m in self._by_uids(args[0])]
            return "OK", []
        if command == "MOVE":
            targets = self._by_uids(args[0])
            self.moved += [m["subject"] for m in targets]
            self.inbox = [m for m in self.inbox if m not in targets]
            return "OK", []
        if command == "STORE":
            flag = args[2].strip("()")
            for m in self._by_uids(args[0]):
                m["flags"].add(flag)
            return "OK", []
        if command == "EXPUNGE":
            targets = self._by_uids(args[0])
            self.inbox = [m for m in self.inbox
                          if not (m in targets and "\\Deleted" in m["flags"])]
            return "OK", []
        raise AssertionError(command)


def _mailbox(fake) -> InvoiceMailbox:
    box = InvoiceMailbox("imap.example", "u", "p")
    box._conn = fake
    return box


def _messages():
    return [
        (11, "invoice #1", _raw("invoice #1", "Total: $100.00"), ()),
        (12, "Re: lunch", _raw("Re: lunch", "I owe you $12.50"), ()),
        (13, "invoice #2", _raw("invoice #2", "Total: $200.00"), ()),
        (14, "old newsletter", _raw("old newsletter", "bye"), ("\\Deleted",)),
    ]


@pytest.mark.parametrize("caps", [("IMAP4REV1", "MOVE"), ("IMAP4REV1", "UIDPLUS"), ("IMAP4REV1",)])
def test_only_matching_invoices_are_processed_and_nothing_else_is_purged(caps):
    fake = FakeImap(_messages(), capabilities=caps)
    parsed = _mailbox(fake).fetch_invoices()

    assert sorted(p.amount_usd for p in parsed) == [100.0, 200.0]
    assert sorted(fake.moved) == ["invoice #1", "invoice #2"]
    remaining = {m["subject"] for m in fake.inbox}
    # The unrelated email and the user's own \Deleted message are untouched.
    assert {"Re: lunch", "old newsletter"} <= remaining
    assert fake.bare_expunges == 0


def test_imap_connection_verifies_the_server_certificate(monkeypatch):
    seen = {}

    class _Conn:
        def __init__(self, host, port, ssl_context=None):
            seen["ctx"] = ssl_context

        def login(self, *_a):
            return "OK", []

    monkeypatch.setattr(invoice_parser.imaplib, "IMAP4_SSL", _Conn)
    InvoiceMailbox("imap.example", "u", "p").connect()
    ctx = seen["ctx"]
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
