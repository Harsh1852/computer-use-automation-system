"""The web surface against the running CoreBank app.

Marked `integration`: these need the target application reachable at
`APP_URL` and a browser. They are the evidence that the perception layer
handles the things the app was built to be hostile about — framesets,
inputs with no accessible name, hidden form state, and a modal that appears
without warning.
"""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

from cua.schema import (
    Action,
    ActionType,
    RecordedEvidence,
    SurfaceKind,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
)
from cua.surface.base import walk_ladder

pytestmark = pytest.mark.integration

APP = os.environ.get("APP_URL", "http://app:5000").rstrip("/")
CONTENT = ["contentFrame"]


def admin(path: str, payload: dict | None = None) -> None:
    request = urllib.request.Request(
        f"{APP}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()


@pytest.fixture
async def surface():
    pytest.importorskip("playwright")
    from cua.cli import sign_on
    from cua.surface import registry

    admin("/admin/reset")
    made = registry.create(SurfaceKind.LEGACY_WEB, entry_url=f"{APP}/app")
    await made.start()
    try:
        await sign_on(made, APP)
        yield made
    finally:
        await made.close()
        admin("/admin/reset")


async def goto(surface, path: str):
    """Navigate the *top* document. Nodes then live at frame_path []."""
    await surface.act(Action(type=ActionType.NAVIGATE, url=f"{APP}{path}"), None)
    return await surface.observe()


async def search(surface, member_id: str):
    """Reach the detail screen the way an operator does, inside the frameset.

    Everything it touches ends up at frame_path ['contentFrame'], which is the
    case that matters: a locator recorded here carries a frame path.
    """
    await goto(surface, "/app")
    field = (
        await surface.find(
            TargetCandidate(
                strategy=TargetStrategy.NEAR_TEXT,
                role="textbox",
                anchor="MEMBER NUMBER",
                index=0,
            ),
            CONTENT,
        )
    )[0]
    await surface.act(Action(type=ActionType.TYPE, text=member_id), field)

    await surface.observe()
    button = (
        await surface.find(
            TargetCandidate(
                strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="SEARCH"
            ),
            CONTENT,
        )
    )[0]
    await surface.act(Action(type=ActionType.CLICK), button)
    return await surface.observe()


def spec(primary: TargetCandidate, *fallbacks: TargetCandidate) -> TargetSpec:
    return TargetSpec(
        primary=primary,
        fallbacks=list(fallbacks),
        recorded=RecordedEvidence(winning_strategy=primary.strategy, candidates_seen=1),
    )


# ------------------------------------------------------------- perception


async def test_observation_spans_every_frame_with_correct_paths(surface):
    observation = await goto(surface, "/app")
    paths = {tuple(n.frame_path) for n in observation.nodes}

    assert ("navFrame",) in paths, "nav frame was not traversed"
    assert ("contentFrame",) in paths, "content frame was not traversed"
    assert observation.title == "MERIDIAN CoreBank - SERVICING CONSOLE"
    assert observation.http_status == 200


async def test_the_member_field_genuinely_has_no_accessible_name(surface):
    """The premise of the whole locator ladder, asserted rather than assumed."""
    observation = await goto(surface, "/app")
    textboxes = [
        n for n in observation.nodes if n.role == "textbox" and n.frame_path == CONTENT
    ]
    assert len(textboxes) == 1
    assert not textboxes[0].name

    labels = [n for n in observation.nodes if n.name == "MEMBER NUMBER"]
    assert labels, "the adjacent cell text must be observable, or near_text has no anchor"


async def test_hidden_form_state_never_reaches_the_observation(surface):
    """__VIEWSTATE is a textbox in the raw tree. Counting it would make an
    unambiguous target look ambiguous."""
    observation = await goto(surface, "/app")
    values = [n.value for n in observation.nodes if n.value]
    assert not any(v.startswith("/wEPDw") for v in values)
    assert all(n.visible for n in observation.nodes)


# ----------------------------------------------------------- locator ladder


async def test_near_text_resolves_the_unnamed_member_field(surface):
    await goto(surface, "/app")
    outcome = await walk_ladder(
        surface,
        spec(
            TargetCandidate(
                strategy=TargetStrategy.A11Y_ROLE_NAME,
                role="textbox",
                name="MEMBER NUMBER",
            ),
            TargetCandidate(
                strategy=TargetStrategy.NEAR_TEXT,
                role="textbox",
                anchor="MEMBER NUMBER",
                index=0,
            ),
        ),
        CONTENT,
    )

    # The preferred rung genuinely fails on this surface, and the ladder
    # absorbs it. That is drift working as designed, not a broken locator.
    assert outcome.winning_index == 1
    assert outcome.drifted is True
    assert outcome.describe_attempts() == "a11y_role_name->0; near_text->1"


async def test_near_text_reads_the_balance_cell_by_ordinal(surface):
    await search(surface, "10001")
    outcome = await walk_ladder(
        surface,
        spec(
            TargetCandidate(
                strategy=TargetStrategy.NEAR_TEXT,
                role="cell",
                anchor="SAVINGS",
                index=3,
            )
        ),
        CONTENT,
    )
    assert outcome.handle is not None

    result = await surface.act(
        Action(type=ActionType.READ, step_id="s7"), outcome.handle
    )
    assert result.ok
    assert result.read_value == "1,234.56"


async def test_ambiguity_is_a_miss_on_the_real_page(surface):
    """A row and its only cell carry identical text. Two matches, so the rung
    misses rather than guessing which one was meant."""
    await goto(surface, "/app")
    candidate = TargetCandidate(strategy=TargetStrategy.EXACT_TEXT, name="MEMBER SEARCH")

    matches = await surface.find(candidate, CONTENT)
    assert len(matches) == 2

    outcome = await walk_ladder(surface, spec(candidate), CONTENT)
    assert outcome.handle is None
    assert outcome.ambiguous is True


async def test_role_disambiguates_the_same_text(surface):
    await goto(surface, "/app")
    matches = await surface.find(
        TargetCandidate(
            strategy=TargetStrategy.EXACT_TEXT, role="cell", name="MEMBER SEARCH"
        ),
        CONTENT,
    )
    assert len(matches) == 1


async def test_frame_scoping_keeps_the_nav_link_out_of_content(surface):
    await goto(surface, "/app")
    candidate = TargetCandidate(
        strategy=TargetStrategy.A11Y_ROLE_NAME, role="link", name="MEMBER SEARCH"
    )
    assert len(await surface.find(candidate, ["navFrame"])) == 1
    assert len(await surface.find(candidate, CONTENT)) == 0


async def test_css_rung_reaches_the_driver_when_the_portable_rungs_cannot(surface):
    await goto(surface, "/app")
    matches = await surface.find(
        TargetCandidate(
            strategy=TargetStrategy.CSS, value="#ctl00_ContentPlaceHolder1_txtMbrNo"
        ),
        CONTENT,
    )
    assert len(matches) == 1


# ------------------------------------------------------------------ acting


async def test_typing_is_visible_in_the_next_observation(surface):
    await goto(surface, "/app")
    handle = (
        await surface.find(
            TargetCandidate(
                strategy=TargetStrategy.NEAR_TEXT,
                role="textbox",
                anchor="MEMBER NUMBER",
                index=0,
            ),
            CONTENT,
        )
    )[0]

    result = await surface.act(
        Action(type=ActionType.TYPE, text="10001", step_id="s5"), handle
    )
    assert result.ok

    observation = await surface.observe()
    typed = [n for n in observation.nodes if n.role == "textbox" and n.frame_path == CONTENT]
    assert typed[0].value == "10001"


async def test_clicking_search_crosses_into_the_detail_screen(surface):
    await goto(surface, "/app")
    field = (
        await surface.find(
            TargetCandidate(
                strategy=TargetStrategy.NEAR_TEXT,
                role="textbox",
                anchor="MEMBER NUMBER",
                index=0,
            ),
            CONTENT,
        )
    )[0]
    await surface.act(Action(type=ActionType.TYPE, text="10001"), field)

    await surface.observe()
    button = (
        await surface.find(
            TargetCandidate(
                strategy=TargetStrategy.A11Y_ROLE_NAME, role="button", name="SEARCH"
            ),
            CONTENT,
        )
    )[0]
    result = await surface.act(Action(type=ActionType.CLICK), button)
    assert result.ok

    observation = await surface.observe()
    names = {n.name for n in observation.nodes}
    assert "CURRENT BALANCE" in " ".join(n for n in names if n)


async def test_a_paused_surface_refuses_to_act(surface):
    """The surface-local half of the handoff: no racing a human operator."""
    from cua.surface.base import LeaseViolation

    await goto(surface, "/app")
    await surface.pause()
    with pytest.raises(LeaseViolation):
        await surface.act(Action(type=ActionType.NAVIGATE, url=f"{APP}/app"), None)

    await surface.resume()
    assert (await surface.act(Action(type=ActionType.NAVIGATE, url=f"{APP}/app"), None)).ok


# -------------------------------------------------------------- exceptional


async def test_an_unexpected_modal_is_observed_structurally(surface):
    await goto(surface, "/app")
    admin("/admin/inject", {"fault": "interstitial", "once": True})
    observation = await goto(surface, "/members/detail?mbr=10001")

    assert observation.dialogs, "the maintenance modal was not detected"
    dialog = observation.dialogs[0]
    assert "SYSTEM MAINTENANCE NOTICE" in dialog.text
    assert dialog.dismiss_ref is not None
    assert observation.by_ref(dialog.dismiss_ref).name == "DISMISS"


async def test_an_http_error_page_is_visible_as_a_status_code(surface):
    await goto(surface, "/app")
    admin("/admin/inject", {"fault": "app_error", "once": True})
    observation = await goto(surface, "/members/detail?mbr=10001")
    assert observation.http_status == 500


async def test_a_permission_denial_is_observable_as_text_and_status(surface):
    observation = await goto(surface, "/members/detail?mbr=10003")
    assert observation.http_status == 403
    text = " ".join(n.name for n in observation.nodes if n.name)
    assert "YOU ARE NOT AUTHORIZED TO VIEW THIS RECORD" in text
