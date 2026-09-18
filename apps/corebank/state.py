"""In-memory application state for the CoreBank demo app.

Deliberately a process-global singleton guarded by a lock: this is a fixture,
not a bank. Nothing here is persisted, so ``POST /admin/reset`` genuinely
returns the app to a known state between demo runs.
"""

from __future__ import annotations

import threading

from .seed import Member, SubAccount, seed_members


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._members: dict[str, Member] = {}
        self._seq = 0
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._members = seed_members()
            self._seq = 0

    def get(self, number: str) -> Member | None:
        return self._members.get((number or "").strip())

    def all(self) -> list[Member]:
        return sorted(self._members.values(), key=lambda m: m.number)

    def open_subaccount(
        self, member_number: str, acct_type: str, nickname: str, deposit: str
    ) -> str:
        """Create a sub-account and return its new account number."""
        with self._lock:
            member = self._members[member_number]
            self._seq += 1
            prefix = {
                "SAVINGS": "S",
                "CHECKING": "C",
                "MONEY MARKET": "M",
                "CERTIFICATE": "T",
            }.get(acct_type, "X")
            number = f"{member_number}-{prefix}{90 + self._seq:02d}"
            member.subaccounts.append(
                SubAccount(
                    acct_type=acct_type,
                    number=number,
                    balance=_fmt_money(deposit),
                    nickname=nickname,
                )
            )
            return number


def _fmt_money(raw: str) -> str:
    try:
        return f"{float(str(raw).replace(',', '').replace('$', '')):,.2f}"
    except ValueError:
        return "0.00"


STORE = Store()
