from __future__ import annotations

import hmac
import logging
import secrets
import signal
from functools import wraps
from typing import Callable

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from bot import ActivationController, Config


def create_app(
    config: Config,
    controller: ActivationController | None = None,
    start_controller: bool = True,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_urlsafe(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
    )
    app.controller = controller or ActivationController(config)  # type: ignore[attr-defined]
    if start_controller:
        app.controller.start()  # type: ignore[attr-defined]

    def authenticated(view: Callable[..., Response | str]):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("authenticated"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Authentication required"}), 401
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    def valid_csrf() -> bool:
        supplied = request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf_token", "")
        return bool(expected and hmac.compare_digest(supplied, expected))

    @app.get("/login")
    def login():
        if session.get("authenticated"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", error=None)

    @app.post("/login")
    def login_submit():
        password = request.form.get("password", "")
        if not config.ui_password or not hmac.compare_digest(password, config.ui_password):
            return render_template("login.html", error="Incorrect password"), 401
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(24)
        return redirect(url_for("dashboard"))

    @app.post("/logout")
    @authenticated
    def logout():
        if not valid_csrf():
            return jsonify({"error": "Invalid CSRF token"}), 403
        session.clear()
        return jsonify({"ok": True})

    @app.get("/")
    @authenticated
    def dashboard():
        return render_template("index.html", csrf_token=session["csrf_token"])

    @app.get("/api/status")
    @authenticated
    def api_status():
        return jsonify(app.controller.status())  # type: ignore[attr-defined]

    def action(method: str, confirmation_required: bool = False):
        if not valid_csrf():
            return jsonify({"error": "Invalid CSRF token"}), 403
        payload = request.get_json(silent=True) or {}
        if confirmation_required and payload.get("confirm") is not True:
            return jsonify({"error": "Confirmation is required"}), 400
        accepted, message = getattr(app.controller, method)()  # type: ignore[attr-defined]
        if not accepted:
            return jsonify({"error": message}), 409
        return jsonify({"ok": True, "message": message})

    @app.post("/api/actions/purchase")
    @authenticated
    def purchase():
        return action("start_purchase", confirmation_required=True)

    @app.post("/api/actions/cancel")
    @authenticated
    def cancel():
        return action("cancel_active_activation", confirmation_required=True)

    @app.post("/api/actions/retry")
    @authenticated
    def retry():
        return action("retry_pending_work")

    @app.post("/api/actions/auto-retry")
    @authenticated
    def set_auto_retry():
        if not valid_csrf():
            return jsonify({"error": "Invalid CSRF token"}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON object required"}), 400
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be true or false"}), 400
        accepted, message = app.controller.set_auto_retry(enabled)  # type: ignore[attr-defined]
        if not accepted:
            return jsonify({"error": message}), 409
        return jsonify({"ok": True, "message": message})

    @app.post("/api/actions/claim-retry-sound")
    @authenticated
    def claim_retry_sound():
        if not valid_csrf():
            return jsonify({"error": "Invalid CSRF token"}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON object required"}), 400
        signal = payload.get("signal")
        if type(signal) is not int:
            return jsonify({"error": "signal must be an integer"}), 400
        accepted, message = app.controller.claim_auto_retry_sound(signal)  # type: ignore[attr-defined]
        if not accepted:
            return jsonify({"error": message}), 409
        return jsonify({"ok": True})

    @app.post("/api/actions/ack-retry-sound")
    @authenticated
    def acknowledge_retry_sound():
        if not valid_csrf():
            return jsonify({"error": "Invalid CSRF token"}), 403
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON object required"}), 400
        signal = payload.get("signal")
        if type(signal) is not int:
            return jsonify({"error": "signal must be an integer"}), 400
        accepted, message = app.controller.acknowledge_auto_retry_sound(signal)  # type: ignore[attr-defined]
        if not accepted:
            return jsonify({"error": message}), 409
        return jsonify({"ok": True})

    return app


def run_web_ui(config: Config) -> int:
    logging.getLogger("werkzeug").setLevel(
        logging.INFO if config.web_request_logs else logging.WARNING
    )
    controller = ActivationController(config)
    app = create_app(config, controller)

    def shutdown(_signal: int, _frame: object) -> None:
        controller.shutdown()
        raise SystemExit(0)

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        app.run(host=config.web_ui_host, port=config.web_ui_port, threaded=False)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        controller.shutdown()
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    return 0
