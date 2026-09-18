"""MERIDIAN CoreBank — Servicing Console.

A deliberately hostile stand-in for a legacy core-banking servicing screen:
a real ``<frameset>``, table-based layout nested three deep, ASP.NET-style
generated ids, ``__doPostBack`` handlers, ``<td>`` text where a ``<label for>``
should be, a ``<font>`` tag and a spacer GIF. There are no ``data-testid``
attributes anywhere and adding one would defeat the purpose of the exercise.

The same blueprint is mounted twice — once at the root as tenant ``meridian``
and once under ``/t/summit-cu/`` as tenant ``summit-cu`` — to stand in for two
institutions running the same vendor product with different branding, labels,
frame names and one extra required field.
"""

from __future__ import annotations

import os
import time

from flask import (
    Blueprint,
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .inject import FAULTS, SLOW_SECONDS, SUPPORTED_FAULTS
from .seed import ACCOUNT_TYPES, BRANCHES
from .state import STORE

TENANTS: dict[str, dict] = {
    "meridian": {
        "id": "meridian",
        "bp": "meridian",
        "prefix": "",
        "product": "MERIDIAN CoreBank",
        "name": "MERIDIAN CoreBank &mdash; Servicing Console",
        "short": "MERIDIAN CoreBank",
        "member_label": "MEMBER NUMBER",
        "member_word": "MEMBER",
        "nav_frame": "navFrame",
        "content_frame": "contentFrame",
        "accent": "#000080",
        "accent2": "#6b7fd7",
        "bg": "#c0c0c0",
        "panel": "#e4e4e4",
        "require_branch": False,
    },
    "summit-cu": {
        "id": "summit-cu",
        "bp": "summit_cu",
        "prefix": "/t/summit-cu",
        "product": "MERIDIAN CoreBank",
        "name": "SUMMIT CREDIT UNION &mdash; Account Servicing",
        "short": "SUMMIT CU",
        "member_label": "ACCOUNT NUMBER",
        "member_word": "ACCOUNT",
        "nav_frame": "sidebar",
        "content_frame": "main",
        "accent": "#1f5d3a",
        "accent2": "#7fb08f",
        "bg": "#e9efe9",
        "panel": "#f4f8f4",
        "require_branch": True,
    },
}

# ASP.NET-style generated control ids. Kept in one place so templates and the
# artifacts that (deliberately) do *not* depend on them stay readable.
CTL = {
    "mbr_no": "ctl00_ContentPlaceHolder1_txtMbrNo",
    "search": "ctl00_ContentPlaceHolder1_btnSearch",
    "acct_type": "ctl00_ContentPlaceHolder1_ddlAcctType",
    "nickname": "ctl00_ContentPlaceHolder1_txtNickname",
    "deposit": "ctl00_ContentPlaceHolder1_txtInitDep",
    "funding": "ctl00_ContentPlaceHolder1_ddlFundSrc",
    "branch": "ctl00_ContentPlaceHolder1_ddlBranch",
    "continue": "ctl00_ContentPlaceHolder1_btnContinue",
    "confirm": "ctl00_ContentPlaceHolder1_btnConfirm",
    "user": "ctl00_txtUserId",
    "pwd": "ctl00_txtPassword",
    "signon": "ctl00_btnSignOn",
}

MIN_DEPOSIT = 25.00


def _user_key(tenant_id: str) -> str:
    return f"user:{tenant_id}"


def _draft_key(tenant_id: str) -> str:
    return f"draft:{tenant_id}"


def make_blueprint(tenant: dict) -> Blueprint:
    bp = Blueprint(tenant["bp"], __name__)
    tid = tenant["id"]

    # ---------------------------------------------------------------- helpers

    @bp.context_processor
    def _inject():
        def u(endpoint: str, **kw):
            return url_for(f"{tenant['bp']}.{endpoint}", **kw)

        return {
            "t": tenant,
            "ctl": CTL,
            "u": u,
            "interstitial": getattr(g, "interstitial", False),
            "acct_types": ACCOUNT_TYPES,
            "branches": BRANCHES,
        }

    def logged_in() -> bool:
        return bool(session.get(_user_key(tid)))

    def login_redirect(msg: str | None = None):
        return redirect(url_for(f"{tenant['bp']}.login", **({"msg": msg} if msg else {})))

    # --------------------------------------------------- generic fault hook
    # Faults apply only to servicing pages (``/members/**``). The frameset
    # shell, the nav frame and the sign-on page stay stable so a fault lands
    # on the screen the demo is actually exercising.

    @bp.before_request
    def _apply_generic_faults():
        if "/members/" not in request.path:
            return None

        if FAULTS.consume("session_expired"):
            session.pop(_user_key(tid), None)
            return login_redirect("timeout")

        if FAULTS.consume("app_error"):
            return render_template("error500.html", ref=f"CB-{int(time.time())}"), 500

        if FAULTS.consume("slow"):
            time.sleep(SLOW_SECONDS)

        if FAULTS.consume("interstitial"):
            g.interstitial = True

        return None

    # ------------------------------------------------------------- sign-on

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        msg = None
        if request.args.get("msg") == "timeout":
            msg = "YOUR SESSION HAS TIMED OUT"
        if request.method == "POST":
            user = (request.form.get("ctl00$txtUserId") or "").strip()
            pwd = request.form.get("ctl00$txtPassword") or ""
            if not user:
                msg = "USER ID IS REQUIRED"
            elif pwd != current_password():
                msg = "SIGN-ON FAILED - INVALID CREDENTIALS"
            else:
                session[_user_key(tid)] = user
                return redirect(url_for(f"{tenant['bp']}.app_shell"))
        return render_template("login.html", msg=msg)

    @bp.route("/logout")
    def logout():
        session.pop(_user_key(tid), None)
        session.pop(_draft_key(tid), None)
        return login_redirect()

    # ------------------------------------------------------- frameset shell

    @bp.route("/app")
    def app_shell():
        if not logged_in():
            return login_redirect()
        return render_template("frameset.html")

    @bp.route("/nav")
    def nav():
        return render_template("nav.html")

    @bp.route("/nav/postback", methods=["POST"])
    def nav_postback():
        """Server side of ``__doPostBack`` from the nav frame."""
        target = request.form.get("__EVENTTARGET", "")
        if target == "navSignOff":
            return redirect(url_for(f"{tenant['bp']}.logout"))
        return redirect(url_for(f"{tenant['bp']}.member_search"))

    # ------------------------------------------------------- member search

    @bp.route("/members/search", methods=["GET", "POST"])
    def member_search():
        if not logged_in():
            return login_redirect()
        error = None
        entered = ""
        if request.method == "POST":
            entered = (request.form.get("ctl00$ContentPlaceHolder1$txtMbrNo") or "").strip()
            if FAULTS.consume("not_found") or not STORE.get(entered):
                error = "NO MATCHING MEMBER RECORD FOUND"
            else:
                return redirect(
                    url_for(f"{tenant['bp']}.member_detail", mbr=entered)
                )
        return render_template("search.html", error=error, entered=entered)

    # ------------------------------------------------------- member detail

    @bp.route("/members/detail")
    def member_detail():
        if not logged_in():
            return login_redirect()
        mbr = (request.args.get("mbr") or "").strip()
        member = STORE.get(mbr)
        if not member:
            return render_template("search.html", error="NO MATCHING MEMBER RECORD FOUND", entered=mbr)
        if member.restricted or FAULTS.consume("permission_denied"):
            return render_template("denied.html", mbr=mbr), 403
        return render_template("detail.html", m=member)

    # ---------------------------------------------------- new sub-account

    @bp.route("/members/subaccount/new", methods=["GET", "POST"])
    def subaccount_new():
        if not logged_in():
            return login_redirect()
        mbr = (request.values.get("mbr") or "").strip()
        member = STORE.get(mbr)
        if not member:
            return render_template("search.html", error="NO MATCHING MEMBER RECORD FOUND", entered=mbr)

        error = None
        form = {
            "acct_type": request.form.get("ctl00$ContentPlaceHolder1$ddlAcctType", ""),
            "nickname": request.form.get("ctl00$ContentPlaceHolder1$txtNickname", ""),
            "deposit": request.form.get("ctl00$ContentPlaceHolder1$txtInitDep", ""),
            "funding": request.form.get("ctl00$ContentPlaceHolder1$ddlFundSrc", ""),
            "branch": request.form.get("ctl00$ContentPlaceHolder1$ddlBranch", ""),
        }

        if request.method == "POST":
            error = _validate_subaccount(form, tenant)
            if error is None:
                session[_draft_key(tid)] = {"mbr": mbr, **form}
                return redirect(url_for(f"{tenant['bp']}.subaccount_review"))

        return render_template("subaccount_new.html", m=member, error=error, form=form)

    @bp.route("/members/subaccount/review")
    def subaccount_review():
        if not logged_in():
            return login_redirect()
        draft = session.get(_draft_key(tid))
        if not draft:
            return redirect(url_for(f"{tenant['bp']}.member_search"))
        member = STORE.get(draft["mbr"])
        return render_template("subaccount_review.html", m=member, d=draft)

    @bp.route("/members/subaccount/confirm", methods=["GET", "POST"])
    def subaccount_confirm():
        if not logged_in():
            return login_redirect()
        draft = session.get(_draft_key(tid))
        if not draft:
            return redirect(url_for(f"{tenant['bp']}.member_search"))
        if request.method == "GET":
            # A refresh of the confirmation screen must not open a second
            # account; the posted draft is cleared on success.
            return redirect(url_for(f"{tenant['bp']}.subaccount_review"))
        number = STORE.open_subaccount(
            draft["mbr"], draft["acct_type"], draft["nickname"], draft["deposit"]
        )
        session.pop(_draft_key(tid), None)
        member = STORE.get(draft["mbr"])
        return render_template("subaccount_confirm.html", m=member, d=draft, number=number)

    return bp


def _validate_subaccount(form: dict, tenant: dict) -> str | None:
    if FAULTS.consume("validation"):
        return "INITIAL DEPOSIT MUST BE AT LEAST $25.00"
    if not form["acct_type"]:
        return "ACCOUNT TYPE IS REQUIRED"
    if tenant["require_branch"] and not form["branch"]:
        return "BRANCH IS REQUIRED"
    if not form["funding"]:
        return "FUNDING SOURCE IS REQUIRED"
    raw = (form["deposit"] or "").replace("$", "").replace(",", "").strip()
    try:
        amount = float(raw)
    except ValueError:
        return "INITIAL DEPOSIT MUST BE A NUMERIC AMOUNT"
    if amount < MIN_DEPOSIT:
        return "INITIAL DEPOSIT MUST BE AT LEAST $25.00"
    return None


def current_password() -> str:
    return os.environ.get("APP_PASSWORD", "demo1234")


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("APP_SECRET_KEY", "dev-only-not-a-real-secret")

    for tenant in TENANTS.values():
        app.register_blueprint(make_blueprint(tenant), url_prefix=tenant["prefix"] or None)

    # ------------------------------------------------------ fault injection
    # Mounted at the application root, outside every tenant prefix, so the
    # policy allowlist can deny ``**/admin/**`` in one line.

    @app.post("/admin/inject")
    def admin_inject():
        payload = request.get_json(silent=True) or {}
        fault = payload.get("fault")
        once = bool(payload.get("once", True))
        if fault not in SUPPORTED_FAULTS:
            return (
                jsonify(error="unsupported fault", supported=SUPPORTED_FAULTS),
                400,
            )
        FAULTS.arm(fault, once=once)
        return jsonify(armed=FAULTS.armed())

    @app.post("/admin/reset")
    def admin_reset():
        FAULTS.clear()
        STORE.reset()
        session.clear()
        return jsonify(ok=True, armed=FAULTS.armed())

    @app.get("/admin/status")
    def admin_status():
        return jsonify(armed=FAULTS.armed(), members=[m.number for m in STORE.all()])

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    @app.get("/")
    def root():
        return redirect(url_for("meridian.login"))

    return app


application = create_app()

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=5000, threaded=True)
