"""Seed data for MERIDIAN CoreBank.

All values are obviously synthetic. No real names, no real account numbers, no
real balances. Member 10003 carries a ``restricted`` flag that produces a
permission denial on the detail screen without any fault injection — a
permanently unhappy path that exists in the data, not only in the fault store.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SubAccount:
    acct_type: str
    number: str
    balance: str
    nickname: str = ""


@dataclass
class Member:
    number: str
    name: str
    status: str
    branch: str
    restricted: bool = False
    subaccounts: list[SubAccount] = field(default_factory=list)


ACCOUNT_TYPES = ["SAVINGS", "CHECKING", "MONEY MARKET", "CERTIFICATE"]

BRANCHES = ["001 - MAIN", "014 - NORTHGATE", "027 - RIVERSIDE"]


def seed_members() -> dict[str, Member]:
    return {
        "10001": Member(
            number="10001",
            name="TESTER, ALEX Q",
            status="ACTIVE",
            branch="001 - MAIN",
            subaccounts=[
                SubAccount("SAVINGS", "10001-S01", "1,234.56", "PRIMARY SHARE"),
                SubAccount("CHECKING", "10001-C01", "402.10", "DRAFT"),
            ],
        ),
        "10002": Member(
            number="10002",
            name="SAMPLE, JORDAN B",
            status="ACTIVE",
            branch="014 - NORTHGATE",
            subaccounts=[
                SubAccount("SAVINGS", "10002-S01", "88.00", "PRIMARY SHARE"),
            ],
        ),
        "10003": Member(
            number="10003",
            name="EXAMPLE, RILEY C",
            status="ACTIVE",
            branch="001 - MAIN",
            restricted=True,
            subaccounts=[
                SubAccount("SAVINGS", "10003-S01", "5,000.00", "PRIMARY SHARE"),
            ],
        ),
        "10004": Member(
            number="10004",
            name="PLACEHOLDER, MORGAN D",
            status="DORMANT",
            branch="027 - RIVERSIDE",
            subaccounts=[
                SubAccount("SAVINGS", "10004-S01", "12.75", "PRIMARY SHARE"),
                SubAccount("MONEY MARKET", "10004-M01", "9,876.54", "MM TIER 1"),
            ],
        ),
        "10005": Member(
            number="10005",
            name="FIXTURE, CASEY E",
            status="ACTIVE",
            branch="014 - NORTHGATE",
            subaccounts=[
                SubAccount("CHECKING", "10005-C01", "310.00", "DRAFT"),
                SubAccount("CERTIFICATE", "10005-T01", "25,000.00", "12MO CD"),
            ],
        ),
    }
