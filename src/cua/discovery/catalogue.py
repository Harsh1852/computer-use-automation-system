"""Per-product knowledge the recorder attaches to every capability.

A happy-path discovery run cannot, by construction, discover the unhappy
paths: you do not observe "no such member" while successfully finding a
member. So the outcome taxonomy and the known transient conditions for a
vendor product are curated once, here, and attached to every capability
recorded against that product — rather than being rediscovered, badly, per
capability.

This is configuration, not discovery, and it is stated as such. In a real
deployment it would live beside the product definition and be reviewed when
the vendor ships a new version.
"""

from __future__ import annotations

from ..schema import (
    BusinessOutcome,
    RecordedEvidence,
    RecoveryAction,
    RecoveryRule,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
    TextPresent,
)


def outcomes_for(vendor_product: str, content_frame: str) -> list[BusinessOutcome]:
    if vendor_product != "MERIDIAN CoreBank":
        return []
    scope = [content_frame] if content_frame else []
    return [
        BusinessOutcome(
            code="MEMBER_NOT_FOUND",
            description="No member exists with that number at this institution.",
            detect=TextPresent(
                text="NO MATCHING MEMBER RECORD FOUND", frame_path=scope
            ),
        ),
        BusinessOutcome(
            code="PERMISSION_DENIED",
            description="The signed-on operator is not entitled to view this record.",
            detect=TextPresent(
                text="YOU ARE NOT AUTHORIZED TO VIEW THIS RECORD", frame_path=scope
            ),
        ),
        BusinessOutcome(
            code="VALIDATION_REJECTED",
            description="The core rejected the submitted values.",
            detect=TextPresent(
                text="INITIAL DEPOSIT MUST BE AT LEAST $25.00", frame_path=scope
            ),
        ),
    ]


def recoveries_for(vendor_product: str) -> list[RecoveryRule]:
    if vendor_product != "MERIDIAN CoreBank":
        return []
    return [
        RecoveryRule(
            id="dismiss_maintenance_notice",
            description=(
                "The console interposes a maintenance banner at unpredictable "
                "times. Known, harmless, dismissable without changing state."
            ),
            when=TextPresent(text="SYSTEM MAINTENANCE NOTICE"),
            do=RecoveryAction.DISMISS,
            max_attempts=1,
            target=TargetSpec(
                primary=TargetCandidate(
                    strategy=TargetStrategy.A11Y_ROLE_NAME,
                    role="button",
                    name="DISMISS",
                    matches_at_record=1,
                ),
                recorded=RecordedEvidence(
                    winning_strategy=TargetStrategy.A11Y_ROLE_NAME, candidates_seen=1
                ),
            ),
        )
    ]
