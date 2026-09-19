"""Typed view of `policy.yaml`.

Policy is configuration, not code. Everything the gate decides is expressed
here as data a reviewer can read in one screen and diff in a pull request —
which is also why the rules are validated on load rather than interpreted
loosely at decision time.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..schema import ActionType

DEFAULT_POLICY_PATH = Path("policy.yaml")

Disposition = Literal["allow", "block", "require_approved_artifact"]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Allowlist(Schema):
    url_patterns: list[str] = []
    denied_patterns: list[str] = []
    action_types: list[ActionType] = []


class RiskRules(Schema):
    irreversible_routes: list[str] = []
    irreversible_labels: list[str] = []

    @property
    def labels(self) -> set[str]:
        return {label.strip().upper() for label in self.irreversible_labels}


class ModeRules(Schema):
    irreversible: Disposition = "block"
    state_changing: Disposition = "allow"
    safe_reversible: Disposition = "allow"


class Enforcement(Schema):
    discovery: ModeRules = Field(default_factory=ModeRules)
    replay: ModeRules = Field(default_factory=ModeRules)
    reauth_allowed: bool = False


class Redaction(Schema):
    patterns: dict[str, str] = {}
    secret_env: list[str] = []

    def secret_values(self, environ: dict[str, str] | None = None) -> list[str]:
        """The actual values to scrub, read from the environment at use time.

        Never stored in the policy file, obviously — the file names the
        variables, the process supplies the values, and neither ends up in
        the repository.
        """
        source = environ if environ is not None else os.environ
        return [v for name in self.secret_env if (v := source.get(name))]


class Policy(Schema):
    allowlist: Allowlist = Field(default_factory=Allowlist)
    risk_rules: RiskRules = Field(default_factory=RiskRules)
    enforcement: Enforcement = Field(default_factory=Enforcement)
    redaction: Redaction = Field(default_factory=Redaction)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        resolved = Path(path or os.environ.get("CUA_POLICY", DEFAULT_POLICY_PATH))
        if not resolved.is_file():
            raise FileNotFoundError(
                f"no policy at {resolved}. The gate refuses to run without one: "
                "an absent policy must not read as an empty allowlist."
            )
        return cls.model_validate(yaml.safe_load(resolved.read_text(encoding="utf-8")))


@lru_cache(maxsize=4)
def load_policy(path: str | None = None) -> Policy:
    return Policy.load(path)
