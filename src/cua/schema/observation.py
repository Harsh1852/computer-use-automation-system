"""What a surface perceives, expressed without reference to any surface.

``Observation`` is the portability boundary. A Chromium frameset, a WinForms
window read through UIA and a macOS app read through AX all reduce to the same
thing: a list of nodes with a role, an accessible name, a value, a state and a
position in a containment path. Everything above this module — the resolver,
the detectors, the discovery agent's prompt — is written against these types
and never learns which surface produced them.

``frame_path`` is the generalised containment path. On the web it is frame
names from the top document down; on a desktop surface it would be the window
and pane hierarchy. Same field, same meaning: "where in the tree of documents
does this node live".
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .artifact import SurfaceKind

__all__ = [
    "Banner",
    "Dialog",
    "NameSource",
    "Observation",
    "Snapshot",
    "SurfaceKind",
    "UiNode",
]


class NameSource(str, Enum):
    """How a node's accessible name was derived.

    Lets ``label_text`` be a genuinely distinct rung of the locator ladder
    rather than a duplicate of ``a11y_role_name``: a name that came from a
    real label element survives different drift than one scraped from the
    node's own text. Every one of these has a UIA/AX analogue.
    """

    ARIA = "aria"
    LABEL = "label"
    VALUE = "value"
    ALT = "alt"
    TITLE = "title"
    TEXT = "text"
    NONE = "none"


class UiNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: int = Field(ge=0)
    """Stable index within this observation. The only handle the model ever sees.

    The discovery agent refers to elements by ``ref``, never by selector, which
    is what keeps a recorded flow portable to a surface that has no DOM.
    """

    role: str
    name: str | None = None
    value: str | None = None
    name_source: NameSource = NameSource.NONE

    enabled: bool = True
    focused: bool = False
    visible: bool = True

    bbox: tuple[int, int, int, int] | None = None
    """``(x, y, width, height)`` in the coordinate space of this node's frame."""

    frame_path: list[str] = []

    order: int = 0
    """Position in the surface's traversal: document order on the web, tree
    order under UIA or AX. Gives ``near_text`` a notion of "what comes next"
    without anything above this layer knowing about a DOM."""

    container_ref: int | None = None
    """Nearest emitted grouping ancestor — a table row on the web, a pane or
    group on a desktop surface. This is what scopes proximity for
    ``near_text``, so the locator ladder stays implementable off the
    observation alone."""

    a11y_visible: bool = True
    """False when this node came from the DOM fallback because the
    accessibility tree omitted it. Recorded so a locator built on a degraded
    perception path is visible in review rather than silently equivalent."""

    @property
    def interactable(self) -> bool:
        return self.enabled and self.visible


class Banner(BaseModel):
    """Page-level message text in a known region: errors, warnings, status."""

    model_config = ConfigDict(extra="forbid")

    text: str
    frame_path: list[str] = []
    region: str | None = None


class Dialog(BaseModel):
    """A modal or overlay that is sitting on top of the content."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    text: str = ""
    frame_path: list[str] = []
    dismiss_ref: int | None = None
    """Ref of a plausible dismiss control, when the surface can spot one."""


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str = ""
    surface_kind: SurfaceKind = SurfaceKind.WEB
    http_status: int | None = None

    frame_urls: dict[str, str] = {}
    """Location of each frame, keyed by ``"/".join(frame_path)`` with ``""``
    for the top document.

    A frameset navigates a child frame without changing the top URL, so
    ``page.url`` alone cannot express "we reached the detail screen". A
    checkpoint has to be able to name *which* document it means. The desktop
    analogue is the document or record identifier of a given pane."""

    frame_statuses: dict[str, int] = {}
    """HTTP status of the document each frame is currently showing, keyed the
    same way as ``frame_urls``. ``http_status`` is the worst of these; this
    map is what lets a failure name *which* document errored."""

    nodes: list[UiNode] = []
    banners: list[Banner] = []
    dialogs: list[Dialog] = []

    captured_at: datetime | None = None

    def by_ref(self, ref: int) -> UiNode | None:
        return next((n for n in self.nodes if n.ref == ref), None)

    def by_role(self, role: str) -> list[UiNode]:
        return [n for n in self.nodes if n.role == role]

    def digest(self) -> str:
        """Content hash over the stable parts of the observation.

        Excludes timestamps, bounding boxes and focus, so a page that merely
        re-rendered hashes the same. This is what the discovery loop compares
        to notice it has made three moves without changing anything.
        """
        payload = {
            "url": self.url,
            "title": self.title,
            "nodes": [
                [n.role, n.name, n.value, n.enabled, n.frame_path] for n in self.nodes
            ],
            "status": self.http_status,
            "frame_statuses": self.frame_statuses,
            "frames": self.frame_urls,
            "banners": sorted(b.text for b in self.banners),
            "dialogs": sorted((d.title or "") + d.text for d in self.dialogs),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode()).hexdigest()


class Snapshot(BaseModel):
    """A richer, on-disk capture taken at a decision point or on failure.

    Paths rather than bytes: evidence belongs on the filesystem with a
    manifest, not inlined into a log line or a result payload.
    """

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str = ""
    captured_at: datetime
    screenshot_ref: str | None = None
    a11y_ref: str | None = None
    dom_ref: str | None = None
    """DOM dump, written on failure only. The model never reads it; it exists
    so a human debugging a TARGET_NOT_FOUND can see what was actually there."""

    contains_pii: bool = False
    """Set when the captured page was flagged as showing regulated data.
    Such captures are written to the run directory but excluded from committed
    evidence."""
