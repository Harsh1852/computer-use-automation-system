"""Surface lookup by kind.

Small on purpose. Its job is to be the one place that knows which concrete
surface serves which `SurfaceKind`, so an artifact's `target.surface_kind`
selects an implementation without any caller importing a driver.

`desktop` is deliberately unregistered rather than stubbed. Asking for it
raises an error that names exactly what an implementation would have to
supply — which is a more honest statement of the seam than a class full of
`NotImplementedError`.
"""

from __future__ import annotations

from typing import Any, Callable

from ..schema import SurfaceKind
from .base import Surface

Factory = Callable[..., Surface]

_FACTORIES: dict[SurfaceKind, Factory] = {}

_UNIMPLEMENTED = {
    SurfaceKind.DESKTOP: (
        "no desktop surface is implemented. One would supply observe() from a "
        "UIA or AX tree, find() over the same four portable rungs (a11y_role_name, "
        "label_text, near_text, exact_text) and no brittle ones, act() through "
        "the platform's invoke/value patterns, and snapshot() from a window "
        "capture. The locator ladder, detectors, executor and artifact schema "
        "would be unchanged."
    )
}


def register(kind: SurfaceKind, factory: Factory) -> None:
    _FACTORIES[kind] = factory


def available() -> list[SurfaceKind]:
    return sorted(_FACTORIES, key=lambda k: k.value)


def create(kind: SurfaceKind, **kwargs: Any) -> Surface:
    factory = _FACTORIES.get(kind)
    if factory is None:
        raise NotImplementedError(
            _UNIMPLEMENTED.get(kind, f"no surface registered for {kind.value}")
        )
    return factory(kind=kind, **kwargs)


def _register_defaults() -> None:
    from .web_playwright import WebPlaywrightSurface

    register(SurfaceKind.WEB, WebPlaywrightSurface)
    register(SurfaceKind.LEGACY_WEB, WebPlaywrightSurface)


_register_defaults()
