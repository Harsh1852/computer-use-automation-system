"""The one module allowed to know what a browser is.

Everything a driver type touches is inside this file. Above it, callers speak
`Observation`, `Action` and `Handle`, which is what makes a desktop surface an
implementation rather than a rewrite.

Two things are worth reading closely:

* **Perception walks every frame.** A real ``<frameset>`` defeats tooling that
  assumes a single document, and the target application is built that way on
  purpose. Each frame is scanned separately and every node is tagged with the
  frame names from the top document down.
* **The locator ladder is implemented off the observation, not off the DOM.**
  `a11y_role_name`, `label_text`, `near_text` and `exact_text` are all resolved
  by filtering `UiNode`s in Python — no selector involved. Only `css` and
  `xpath`, the two rungs marked brittle by construction, reach for the driver.
  A surface with no DOM simply offers no brittle rungs; the other four work
  unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Error as PlaywrightError,
    Frame,
    Page,
    async_playwright,
)

from ..schema import (
    Action,
    ActionResult,
    ActionType,
    Banner,
    Dialog,
    ExtractFrom,
    NameSource,
    Observation,
    Snapshot,
    SurfaceKind,
    TargetCandidate,
    TargetSpec,
    TargetStrategy,
    UiNode,
)
from .base import (
    Gate,
    Handle,
    Lease,
    LeaseViolation,
    OpenGate,
    SurfaceError,
    UnheldLease,
    render_table,
    walk_ladder,
)

_SCANNER = (Path(__file__).parent / "scan.js").read_text(encoding="utf-8")
_HUMAN_EVENTS = (Path(__file__).parent / "human_events.js").read_text(encoding="utf-8")

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--window-position=0,0",
]


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


@dataclass(frozen=True, kw_only=True)
class WebHandle(Handle):
    """A resolved element, plus the driver reference only this module may use."""

    element: ElementHandle


class WebPlaywrightSurface:
    """A headed Chromium surface over a legacy, frame-based web application."""

    def __init__(
        self,
        *,
        entry_url: str,
        gate: Gate | None = None,
        lease: Lease | None = None,
        run_dir: str | Path | None = None,
        viewport: tuple[int, int] = (1280, 900),
        headless: bool = False,
        actor: str = "automation",
        kind: SurfaceKind = SurfaceKind.LEGACY_WEB,
    ) -> None:
        self.kind = kind
        self.entry_url = entry_url
        self._gate: Gate = gate or OpenGate()
        self._lease: Lease = lease or UnheldLease()
        self._actor = actor
        self._viewport = viewport
        self._headless = headless
        self._run_dir = Path(run_dir) if run_dir else None

        self._pw: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        self._handles: dict[int, ElementHandle] = {}
        self._ids: dict[int, str] = {}
        self._observation: Observation | None = None
        self._stale = True
        self._paused = False
        self._watching = False
        self._status_by_url: dict[str, int] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless, args=_LAUNCH_ARGS
        )
        self._context = await self._browser.new_context(
            viewport={"width": self._viewport[0], "height": self._viewport[1]},
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()
        self._page.on("response", self._note_response)

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except PlaywrightError:
                    pass
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self._browser = self._context = self._page = None

    async def __aenter__(self) -> "WebPlaywrightSurface":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def page(self) -> Page:
        if self._page is None:
            raise SurfaceError("surface is not started")
        return self._page

    def _note_response(self, response: Any) -> None:
        """Remember each document response *by URL*, not just the latest.

        A frameset loads several documents at once, so "the last document
        response" is a race: whether an error page is visible would depend on
        which frame happened to finish second. Keyed by URL, each frame's
        status can be looked up for the document it is actually showing.
        """
        try:
            if response.request.resource_type == "document":
                self._status_by_url[response.url] = response.status
        except PlaywrightError:
            pass

    # ----------------------------------------------------------- perception

    @staticmethod
    def _frame_path(frame: Frame) -> list[str]:
        path: list[str] = []
        current: Frame | None = frame
        while current is not None and current.parent_frame is not None:
            path.append(current.name or "<unnamed>")
            current = current.parent_frame
        return list(reversed(path))

    async def observe(self) -> Observation:
        page = self.page
        nodes: list[UiNode] = []
        dialogs: list[Dialog] = []
        banners: list[Banner] = []
        handles: dict[int, ElementHandle] = {}
        ids: dict[int, str] = {}
        title = ""
        ref = 0

        frame_urls: dict[str, str] = {}
        frame_statuses: dict[str, int] = {}
        statuses: list[int] = []
        for frame in page.frames:
            frame_path = self._frame_path(frame)
            frame_urls["/".join(frame_path)] = frame.url
            if (status := self._status_by_url.get(frame.url)) is not None:
                frame_statuses["/".join(frame_path)] = status
                statuses.append(status)
            try:
                scanned = await frame.evaluate_handle(_SCANNER)
            except PlaywrightError:
                continue  # frame navigated or detached mid-scan; skip it

            try:
                payload = await (await scanned.get_property("nodes")).json_value()
                element_props = await (await scanned.get_property("elements")).get_properties()
                frame_dialogs = await (await scanned.get_property("dialogs")).json_value()
                frame_banners = await (await scanned.get_property("banners")).json_value()
                if not frame_path:
                    title = await (await scanned.get_property("title")).json_value() or ""
            except PlaywrightError:
                continue
            finally:
                await scanned.dispose()

            local_to_ref: dict[int, int] = {}
            first_ref = ref
            for local, descriptor in enumerate(payload):
                element = element_props.get(str(local))
                as_element = element.as_element() if element is not None else None
                if as_element is None:
                    continue
                local_to_ref[local] = ref
                handles[ref] = as_element
                if descriptor.get("dom_id"):
                    ids[ref] = descriptor["dom_id"]
                nodes.append(
                    UiNode(
                        ref=ref,
                        role=descriptor["role"],
                        name=descriptor["name"],
                        value=descriptor["value"],
                        name_source=NameSource(descriptor["name_source"]),
                        enabled=descriptor["enabled"],
                        focused=descriptor["focused"],
                        visible=True,
                        bbox=tuple(descriptor["bbox"]),
                        frame_path=frame_path,
                        order=first_ref + descriptor["order"],
                    )
                )
                ref += 1

            # Resolve container references now that local -> global is known.
            for node, descriptor in zip(nodes[first_ref:], payload):
                container = descriptor.get("container")
                if container is not None:
                    node.container_ref = local_to_ref.get(container)

            for item in frame_dialogs:
                dismiss = item.get("dismiss")
                dialogs.append(
                    Dialog(
                        title=item.get("title"),
                        text=item.get("text", ""),
                        frame_path=frame_path,
                        dismiss_ref=local_to_ref.get(dismiss) if dismiss is not None else None,
                    )
                )
            for item in frame_banners:
                banners.append(
                    Banner(text=item["text"], frame_path=frame_path, region=item.get("region"))
                )

        self._handles = handles
        self._ids = ids
        self._observation = Observation(
            url=page.url,
            title=title or (await page.title() if page else ""),
            surface_kind=self.kind,
            # The worst status among the documents currently on screen: an
            # error page in the content frame is an error page, whatever the
            # shell around it returned.
            http_status=(max(statuses) if statuses else None),
            frame_statuses=frame_statuses,
            frame_urls=frame_urls,
            nodes=nodes,
            banners=banners,
            dialogs=dialogs,
            captured_at=datetime.now(timezone.utc),
        )
        self._stale = False
        return self._observation

    async def _current(self) -> Observation:
        """Reuse the last observation unless an action has invalidated it."""
        if self._stale or self._observation is None:
            return await self.observe()
        return self._observation

    # -------------------------------------------------------------- finding

    async def find(
        self, candidate: TargetCandidate, frame_path: list[str]
    ) -> list[Handle]:
        if candidate.strategy in (TargetStrategy.CSS, TargetStrategy.XPATH):
            return await self._find_by_selector(candidate, frame_path)

        observation = await self._current()
        scoped = [n for n in observation.nodes if n.frame_path == list(frame_path)]

        if candidate.strategy is TargetStrategy.A11Y_ROLE_NAME:
            matches = [
                n
                for n in scoped
                if n.role == candidate.role and _norm(n.name) == _norm(candidate.name)
            ]
        elif candidate.strategy is TargetStrategy.LABEL_TEXT:
            matches = [
                n
                for n in scoped
                if n.name_source is NameSource.LABEL
                and _norm(n.name) == _norm(candidate.name)
                and (candidate.role is None or n.role == candidate.role)
            ]
        elif candidate.strategy is TargetStrategy.EXACT_TEXT:
            matches = [
                n
                for n in scoped
                if _norm(n.name) == _norm(candidate.name)
                and (candidate.role is None or n.role == candidate.role)
            ]
        elif candidate.strategy is TargetStrategy.NEAR_TEXT:
            matches = self._near_text(scoped, candidate)
        else:  # pragma: no cover - the enum is closed
            raise SurfaceError(f"unsupported strategy {candidate.strategy}")

        return [self._handle_for(n, candidate.strategy, len(matches)) for n in matches]

    def _near_text(
        self, scoped: list[UiNode], candidate: TargetCandidate
    ) -> list[UiNode]:
        """Anchor text, plus a role, plus an ordinal.

        Scoped to the anchor's container — the table row on the web, a pane on
        a desktop surface — and falling back to a short document-order window
        when the anchor is not inside one. This is the rung that carries the
        legacy case, where an input has no accessible name at all and the only
        thing identifying it is the text in the neighbouring cell.
        """
        anchor_text = _norm(candidate.anchor)
        anchors = [n for n in scoped if _norm(n.name) == anchor_text]
        found: list[UiNode] = []
        seen: set[int] = set()

        for anchor in anchors:
            if anchor.container_ref is not None:
                siblings = [
                    n for n in scoped if n.container_ref == anchor.container_ref
                ]
            else:
                window = anchor.order + 8
                siblings = [n for n in scoped if anchor.order <= n.order <= window]

            ordered = sorted(
                (n for n in siblings if n.role == candidate.role),
                key=lambda n: n.order,
            )
            if not ordered:
                continue
            if candidate.index is None:
                picked = ordered if len(ordered) == 1 else []
            else:
                picked = [ordered[candidate.index]] if candidate.index < len(ordered) else []

            for node in picked:
                if node.ref not in seen:
                    seen.add(node.ref)
                    found.append(node)
        return found

    async def _find_by_selector(
        self, candidate: TargetCandidate, frame_path: list[str]
    ) -> list[Handle]:
        frame = self._frame_at(frame_path)
        if frame is None:
            return []
        selector = (
            candidate.value
            if candidate.strategy is TargetStrategy.CSS
            else f"xpath={candidate.value}"
        )
        try:
            elements = await frame.query_selector_all(selector)
        except PlaywrightError as exc:
            raise SurfaceError(f"bad {candidate.strategy.value} selector: {exc}") from exc

        visible_elements = [e for e in elements if await e.is_visible()]
        return [
            WebHandle(
                strategy=candidate.strategy,
                matched=len(visible_elements),
                description=f"{candidate.strategy.value}={candidate.value}",
                frame_path=tuple(frame_path),
                element=element,
            )
            for element in visible_elements
        ]

    def _frame_at(self, frame_path: list[str]) -> Frame | None:
        for frame in self.page.frames:
            if self._frame_path(frame) == list(frame_path):
                return frame
        return None

    def _handle_for(
        self, node: UiNode, strategy: TargetStrategy, matched: int
    ) -> WebHandle:
        element = self._handles.get(node.ref)
        if element is None:
            raise SurfaceError(f"no live element for ref {node.ref}")
        label = node.name or f"<{node.role}>"
        return WebHandle(
            strategy=strategy,
            matched=matched,
            description=f"{node.role} {label!r}",
            frame_path=tuple(node.frame_path),
            ref=node.ref,
            element=element,
        )

    async def resolve(self, spec: TargetSpec, frame_path: list[str]) -> Handle | None:
        return (await walk_ladder(self, spec, frame_path)).handle

    async def handle_for(self, ref: int) -> Handle | None:
        observation = await self._current()
        node = observation.by_ref(ref)
        if node is None or ref not in self._handles:
            return None
        return self._handle_for(node, TargetStrategy.A11Y_ROLE_NAME, 1)

    def native_locator(self, ref: int) -> TargetCandidate | None:
        """`#id` when the element has one. Recorded as the brittle last rung.

        These are ASP.NET generated ids: unique today, and renamed by any
        server-side change to the control hierarchy. That is exactly what
        `brittle` means, and why this rung sits at the bottom of the ladder
        rather than the top where its convenience would put it.
        """
        element = self._handles.get(ref)
        if element is None:
            return None
        element_id = self._ids.get(ref)
        if not element_id:
            return None
        return TargetCandidate(
            strategy=TargetStrategy.CSS, value=f'[id="{element_id}"]'
        )

    # ---------------------------------------------------------------- acting

    async def act(self, action: Action, handle: Handle | None) -> ActionResult:
        """The single chokepoint.

        Who may act is checked first, then what may be done. Nothing else in
        the system enforces either, which is the whole point: there is exactly
        one place to read, and exactly one place to get it wrong.
        """
        self._lease.assert_holder(self._actor)
        if self._paused:
            raise LeaseViolation(
                f"{self._actor} tried to act while the surface is paused for handoff"
            )
        context: Mapping[str, Any] = {
            "surface": self.kind.value,
            "url": self.page.url,
            "actor": self._actor,
            "frame_path": list(handle.frame_path) if handle else [],
        }
        self._gate.check(action, context)

        started = time.perf_counter()
        try:
            read_value, url_after = await self._dispatch(action, handle)
            ok, detail = True, None
        except PlaywrightError as exc:
            read_value, url_after = None, self.page.url
            ok, detail = False, f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

        self._stale = True
        return ActionResult(
            ok=ok,
            action=action.type,
            step_id=action.step_id,
            duration_ms=int((time.perf_counter() - started) * 1000),
            url_after=url_after,
            read_value=read_value,
            detail=detail,
        )

    async def _dispatch(
        self, action: Action, handle: Handle | None
    ) -> tuple[str | None, str]:
        page = self.page
        timeout = float(action.timeout_ms)

        if action.type is ActionType.NAVIGATE:
            if not action.url:
                raise SurfaceError("navigate without a url")
            response = await page.goto(
                action.url, wait_until="domcontentloaded", timeout=timeout
            )
            if response is not None:
                self._status_by_url[response.url] = response.status
            await self._settle(timeout)
            return None, page.url

        if action.type in (ActionType.WAIT, ActionType.ASSERT):
            # Both are resolved by the executor evaluating the postcondition.
            # The surface has nothing to do, and crucially there is no
            # duration to honour: a wait waits on an observable condition.
            return None, page.url

        element = self._element_of(handle, action)

        if action.type is ActionType.CLICK:
            await element.click(timeout=timeout)
            await self._settle(timeout)
        elif action.type is ActionType.TYPE:
            await element.fill(action.text or "", timeout=timeout)
        elif action.type is ActionType.SELECT:
            await self._select(element, action, timeout)
        elif action.type is ActionType.READ:
            return await self._read(element, action), page.url
        else:  # pragma: no cover - the enum is closed
            raise SurfaceError(f"unsupported action {action.type}")

        return None, page.url

    async def _settle(self, timeout: float) -> None:
        """Return only once the surface has stopped changing on its own.

        An action that navigates leaves subframes in flight. Returning before
        they land means the next observation races the browser — and, worse,
        that a request issued by the *previous* action can still be arriving
        while the next one is being decided. The `load` event fires after
        subframes load, which is precisely the frameset condition we need.
        Observable condition, not a duration.
        """
        try:
            await self.page.wait_for_load_state("load", timeout=min(timeout, 15_000))
        except PlaywrightError:
            pass  # no navigation happened, or it already completed

    @staticmethod
    def _element_of(handle: Handle | None, action: Action) -> ElementHandle:
        if not isinstance(handle, WebHandle):
            raise SurfaceError(f"{action.type.value} needs a resolved element")
        return handle.element

    @staticmethod
    async def _select(element: ElementHandle, action: Action, timeout: float) -> None:
        wanted = action.option or action.text or ""
        try:
            await element.select_option(value=wanted, timeout=timeout)
        except PlaywrightError:
            # Legacy forms routinely differ between an option's value and its
            # visible label; try the label the operator would have read.
            await element.select_option(label=wanted, timeout=timeout)

    @staticmethod
    async def _read(element: ElementHandle, action: Action) -> str | None:
        extract = action.extract or ExtractFrom.TEXT
        if extract is ExtractFrom.VALUE:
            return await element.input_value()
        if extract is ExtractFrom.URL:
            return await element.get_attribute("href")
        return _norm(await element.inner_text())

    # -------------------------------------------------------------- evidence

    async def snapshot(self, label: str = "snapshot") -> Snapshot:
        page = self.page
        screenshot_ref = a11y_ref = None

        if self._run_dir is not None:
            shots = self._run_dir / "screenshots"
            trees = self._run_dir / "a11y"
            shots.mkdir(parents=True, exist_ok=True)
            trees.mkdir(parents=True, exist_ok=True)

            shot = shots / f"{label}.png"
            await page.screenshot(path=str(shot))
            screenshot_ref = str(shot.relative_to(self._run_dir))

            observation = await self.observe()
            tree = trees / f"{label}.txt"
            tree.write_text(render_table(observation), encoding="utf-8")
            a11y_ref = str(tree.relative_to(self._run_dir))

        return Snapshot(
            url=page.url,
            title=await page.title(),
            captured_at=datetime.now(timezone.utc),
            screenshot_ref=screenshot_ref,
            a11y_ref=a11y_ref,
        )

    # ------------------------------------------------------- control handoff

    async def watch_human_actions(self, sink: Any) -> None:
        """Install capture-phase listeners in every frame, now and later.

        `add_init_script` covers frames the operator navigates to after taking
        control; the explicit evaluate covers the frames already on screen,
        which is the interesting case because the handoff happens mid-flow.
        """
        if self._watching:
            return
        self._watching = True

        def relay(source: dict, payload: dict) -> None:
            # The page cannot know its own frame path; the surface can.
            payload["frame_path"] = self._frame_path(source["frame"])
            sink(payload)

        await self._context.expose_binding("__cuaHumanEvent", relay)
        await self._context.add_init_script(_HUMAN_EVENTS)
        for frame in self.page.frames:
            try:
                await frame.evaluate(_HUMAN_EVENTS)
            except PlaywrightError:
                continue

    async def act_as_operator(self, token: str, action: Action, handle: Handle | None):
        """Perform an action on behalf of a lease-holding operator.

        In production a person drives this session through VNC, which bypasses
        this API entirely — bypassing it is what VNC *is*. This exists so a
        scripted operator can exercise the same handoff without a human in a
        test, and it is gated by the identical lease check: the token must be
        the current holder, or it raises like anything else would.
        """
        self._lease.assert_holder(token)
        was_paused, actor = self._paused, self._actor
        self._paused, self._actor = False, token
        try:
            return await self.act(action, handle)
        finally:
            self._paused, self._actor = was_paused, actor

    async def pause(self) -> None:
        """Stop accepting automation actions on this session.

        The surface-local half of the handoff. The lease is the authoritative
        single-writer mechanism; this flag makes a violation fail loudly at the
        one place that touches the browser, instead of racing a human.
        """
        self._paused = True
        self._stale = True

    async def resume(self) -> None:
        self._paused = False
        self._stale = True  # a human was driving; nothing cached is trustworthy

    @property
    def paused(self) -> bool:
        return self._paused

