"""What the model sees, and what it is told.

Two decisions live here and both are load-bearing.

**The model never sees markup.** It gets a list of nodes with a role, an
accessible name, a value and a frame — the same `Observation` a desktop
surface would produce. Give a model HTML and it will reach for
``#ctl00_ContentPlaceHolder1_txtMbrNo``, because that is the most convenient
handle on the page; those ids are generated from the ASP.NET control
hierarchy and are renamed by any server-side refactor. Worse, a flow recorded
against markup stops being portable the moment the surface has none.

**The model never sees credentials.** It is told to type the literal tokens
``{{APP_USER}}`` and ``{{APP_PASSWORD}}``; the tool layer substitutes them
from the environment on the way to the browser. The transcript therefore
contains the placeholder, and the recorder emits ``{"secret_ref": ...}``
without having to recognise a password after the fact.
"""

from __future__ import annotations

from ..schema import Observation, UiNode

SECRET_TOKENS = {"{{APP_USER}}": "APP_USER", "{{APP_PASSWORD}}": "APP_PASSWORD"}

MAX_RENDERED_NODES = 140

SYSTEM_PROMPT = """\
You operate a legacy back-office banking application the way a human operator \
would. You see the screen as a list of accessibility nodes and you act by \
referring to a node's number.

Rules you must follow:

1. Refer to elements ONLY by their [ref] number. Never write a CSS selector, \
an XPath, or an element id. You are describing what an operator sees, not how \
the page is built.
2. Many fields in this application have NO accessible name, because their \
label is plain table text beside them. Those nodes show `near="SOME TEXT"` — \
use that to tell them apart.
3. Call `observe` after any action that might change the screen. Refs are only \
valid for the observation that produced them.
4. To sign on, type the literal text {{APP_USER}} into the user id field and \
{{APP_PASSWORD}} into the password field. Never type a credential you made \
up: you do not know these values, the runtime substitutes them, and a guess \
is refused rather than submitted.
5. Use `read` for every value the caller asked you to retrieve. A value you \
only looked at is not an output.
6. {irreversible_rule}
7. When the goal is met, call `done` and declare the capability's contract: \
which inputs a caller must supply, and which outputs they get back.

Be economical. Every action is recorded into a reusable capability, so take \
the path an operator would take, not an exploratory one.\
"""

UNATTENDED_RULE = (
    "Do NOT perform irreversible actions. If a screen says a button will "
    "confirm, submit, transfer or delete something, stop and call `stuck` "
    "describing what needs approval."
)

SUPERVISED_RULE = (
    "This is a SUPERVISED recording session: a human operator is present and "
    "has approved recording this flow end to end, including its final "
    "irreversible confirmation. Complete it so that step is captured. Still "
    "call `stuck` for anything else that looks unsafe or unexpected."
)


def system_prompt(supervised: bool = False) -> str:
    """The prompt, with the irreversible-action rule chosen by mode.

    Unattended discovery may never execute an irreversible action: the model
    is exploratory and fallible, and an account opened by mistake cannot be
    un-opened. But a capability whose entire purpose *is* the irreversible
    step has to be recorded once, and that first recording is a supervised
    activity with a person watching. The mode is explicit and off by default,
    so the permissive path cannot be reached by accident — and once the policy
    gate exists, it enforces the same asymmetry rather than trusting this
    prompt to hold.
    """
    return SYSTEM_PROMPT.format(
        irreversible_rule=SUPERVISED_RULE if supervised else UNATTENDED_RULE
    )


def goal_prompt(goal: str, entry_url: str) -> str:
    return (
        f"GOAL: {goal}\n"
        f"ENTRY POINT: {entry_url}\n\n"
        "Start by navigating to the entry point, then observe."
    )


def near_hint(observation: Observation, node: UiNode) -> str | None:
    """The nearest named text to the left of / above an unnamed control.

    This is deliberately the same notion of proximity that the `near_text`
    locator uses, so what the model reads and what the recorder writes down
    cannot disagree.
    """
    if node.name:
        return None
    siblings = [
        n
        for n in observation.nodes
        if n.frame_path == node.frame_path
        and n.name
        and n.order < node.order
        and (node.container_ref is None or n.container_ref == node.container_ref)
    ]
    if not siblings:
        siblings = [
            n
            for n in observation.nodes
            if n.frame_path == node.frame_path and n.name and n.order < node.order
        ]
    return siblings[-1].name if siblings else None


def render_for_model(observation: Observation) -> str:
    """Compact, role/name/value text. No markup, no coordinates, no ids."""
    lines = [
        f"URL: {observation.url}",
        f"TITLE: {observation.title}",
    ]
    if observation.http_status and observation.http_status >= 400:
        lines.append(f"HTTP STATUS: {observation.http_status}")
    for path, url in sorted(observation.frame_urls.items()):
        if path:
            lines.append(f"FRAME {path}: {url}")
    if observation.dialogs:
        for dialog in observation.dialogs:
            lines.append(f"DIALOG: {dialog.text[:200]}")
    lines.append("")
    lines.append("SCREEN:")

    shown = observation.nodes[:MAX_RENDERED_NODES]
    for node in shown:
        frame = "/".join(node.frame_path) or "top"
        bits = [f"[{node.ref}]", f"{node.role:<12}"]
        bits.append(f'"{node.name}"' if node.name else "(no name)")
        if node.value:
            bits.append(f"value={node.value!r}")
        if hint := near_hint(observation, node):
            bits.append(f'near="{hint}"')
        bits.append(f"frame={frame}")
        if not node.enabled:
            bits.append("DISABLED")
        lines.append("  " + " ".join(bits))

    if len(observation.nodes) > MAX_RENDERED_NODES:
        lines.append(f"  ... {len(observation.nodes) - MAX_RENDERED_NODES} more nodes")
    return "\n".join(lines)


def substitute_secrets(text: str, secrets: dict[str, str]) -> tuple[str, str | None]:
    """Replace a credential placeholder with its value.

    Returns the text to type and the environment variable it came from, so the
    recorder can write ``{"secret_ref": ...}`` and the log can write nothing.
    """
    for token, env_var in SECRET_TOKENS.items():
        if token in text:
            value = secrets.get(env_var)
            if value is None:
                raise KeyError(f"{env_var} is not set but the agent asked for {token}")
            return text.replace(token, value), env_var
    return text, None
