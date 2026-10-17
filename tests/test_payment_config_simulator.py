"""Payment simulation config (fail closed), deterministic simulator and strict Decimal parsing."""
import ast
import hashlib
import logging
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.payments.config import load_settings, validate_at_startup
from app.payments.schemas import PaymentSimulationIn, parse_money
from app.payments.simulator import simulate

PAYMENTS_DIR = Path(__file__).resolve().parent.parent / "app" / "payments"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAYMENT_MODE", "simulation")
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")
    monkeypatch.setenv("PAYMENT_ALLOWED_CURRENCIES", "INR")
    monkeypatch.setenv("PAYMENT_SIMULATOR_PROFILE", "normal")
    for var in ("PAYMENT_INITIATOR_ROLES", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)


# ---------- config ----------

def test_valid_demo_config_is_available():
    s = load_settings()
    assert s.available and s.mode == "simulation" and s.enabled
    assert s.allowed_currencies == frozenset({"INR"})
    assert s.profile == "normal" and s.initiator_roles == frozenset({"admin"}) and s.errors == ()


def test_mode_unset_fails_closed(monkeypatch):
    monkeypatch.delenv("PAYMENT_MODE")
    s = load_settings()
    assert not s.available and s.mode is None and s.errors


@pytest.mark.parametrize("mode", ["live", "real", "production", "sandbox", ""])
def test_invalid_mode_fails_closed(monkeypatch, mode):
    monkeypatch.setenv("PAYMENT_MODE", mode)
    s = load_settings()
    assert not s.available and s.mode is None and s.errors


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_disabled_is_unavailable(monkeypatch, value):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", value)
    s = load_settings()
    assert not s.enabled and not s.available


def test_enabled_default_is_false(monkeypatch):
    monkeypatch.delenv("PAYMENT_SIMULATION_ENABLED")
    assert not load_settings().available


def test_invalid_enabled_value_is_error(monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "maybe")
    s = load_settings()
    assert not s.available and s.errors


@pytest.mark.parametrize("var,value", [("APP_ENV", "production"), ("APP_ENV", "prod"), ("ENVIRONMENT", "Production")])
def test_production_env_disables(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    s = load_settings()
    assert not s.available
    assert "simulation disabled in production" in s.errors


def test_invalid_profile_fails_closed(monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATOR_PROFILE", "always_pay")
    s = load_settings()
    assert not s.available and s.profile == "normal" and s.errors


@pytest.mark.parametrize("profile", ["normal", "transient_failure", "processing"])
def test_valid_profiles(monkeypatch, profile):
    monkeypatch.setenv("PAYMENT_SIMULATOR_PROFILE", profile)
    s = load_settings()
    assert s.available and s.profile == profile


def test_currencies_parsed_and_invalid_rejected(monkeypatch):
    monkeypatch.setenv("PAYMENT_ALLOWED_CURRENCIES", "inr, usd")
    assert load_settings().allowed_currencies == frozenset({"INR", "USD"})
    monkeypatch.setenv("PAYMENT_ALLOWED_CURRENCIES", "INR,RUPEES")
    assert not load_settings().available
    monkeypatch.setenv("PAYMENT_ALLOWED_CURRENCIES", " , ")
    assert not load_settings().available


def test_user_role_never_initiator(monkeypatch):
    monkeypatch.setenv("PAYMENT_INITIATOR_ROLES", "admin,user")
    s = load_settings()
    assert "user" not in s.initiator_roles and not s.available
    monkeypatch.setenv("PAYMENT_INITIATOR_ROLES", "superuser")
    assert not load_settings().available
    monkeypatch.setenv("PAYMENT_INITIATOR_ROLES", "admin,approver")
    s = load_settings()
    assert s.available and s.initiator_roles == frozenset({"admin", "approver"})


def test_validate_at_startup_logs_and_never_raises(monkeypatch, caplog):
    monkeypatch.setenv("PAYMENT_MODE", "live")
    monkeypatch.setenv("SECRET_TOKEN_SHOULD_NOT_LOG", "s3cr3t-value")
    with caplog.at_level(logging.INFO):
        s = validate_at_startup()
    assert not s.available
    assert "s3cr3t-value" not in caplog.text and "live" not in caplog.text
    assert "UNAVAILABLE" in caplog.text


def test_validate_at_startup_survives_internal_error(monkeypatch):
    import app.payments.config as cfg

    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(cfg, "load_settings", boom)
    s = cfg.validate_at_startup()
    assert not s.available and s.errors


# ---------- simulator ----------

def _ref(pid, attempt):
    return "PAY-SIM-" + hashlib.sha256(f"{pid}:{attempt}".encode()).hexdigest()[:6].upper()


def test_normal_profile_accepts_deterministically():
    a = simulate("psim_abc", Decimal("76125.00"), "INR", 1, "normal")
    b = simulate("psim_abc", Decimal("76125.00"), "INR", 1, "normal")
    assert a == b
    assert a.status == "accepted_by_simulator" and a.failure_code is None
    assert a.provider_reference == _ref("psim_abc", 1)
    assert simulate("psim_xyz", Decimal("1.00"), "INR", 1, "normal").provider_reference != a.provider_reference


def test_processing_profile():
    o = simulate("psim_abc", Decimal("10.00"), "INR", 1, "processing")
    assert o.status == "processing_simulated" and o.provider_reference == _ref("psim_abc", 1)


def test_transient_failure_then_accept_on_retry():
    o1 = simulate("psim_abc", Decimal("10.00"), "INR", 1, "transient_failure")
    assert o1.status == "failed_simulated" and o1.failure_code == "SIMULATED_PROVIDER_UNAVAILABLE"
    assert o1.provider_reference is None
    o2 = simulate("psim_abc", Decimal("10.00"), "INR", 2, "transient_failure")
    assert o2.status == "accepted_by_simulator" and o2.provider_reference == _ref("psim_abc", 2)
    assert simulate("psim_abc", Decimal("10.00"), "INR", 3, "transient_failure").status == "accepted_by_simulator"


@pytest.mark.parametrize("args", [
    ("", Decimal("1.00"), "INR", 1, "normal"),
    ("p", Decimal("0"), "INR", 1, "normal"),
    ("p", Decimal("-1"), "INR", 1, "normal"),
    ("p", Decimal("NaN"), "INR", 1, "normal"),
    ("p", Decimal("Infinity"), "INR", 1, "normal"),
    ("p", 10.0, "INR", 1, "normal"),
    ("p", Decimal("1.00"), "inr", 1, "normal"),
    ("p", Decimal("1.00"), "INR", 0, "normal"),
    ("p", Decimal("1.00"), "INR", True, "normal"),
    ("p", Decimal("1.00"), "INR", 1, "live"),
])
def test_simulator_rejects_invalid_input(args):
    with pytest.raises(ValueError):
        simulate(*args)


def test_payments_package_has_no_network_or_provider_imports():
    forbidden = {"requests", "httpx", "urllib.request", "http.client", "socket", "smtplib", "aiohttp",
                 "stripe", "razorpay", "paypal", "braintree", "adyen", "random", "app.agents.billing"}
    for path in PAYMENTS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for n in names:
                assert not any(n == f or n.startswith(f + ".") for f in forbidden), (path.name, n)


# ---------- Decimal parsing ----------

@pytest.mark.parametrize("value", ["NaN", "nan", "inf", "Infinity", "-Infinity", "-1", "-0.01", "0", "0.00",
                                   "1.234", "abc", ""])
def test_parse_money_rejects(value):
    with pytest.raises(ValueError):
        parse_money(value)


@pytest.mark.parametrize("value", [1.5, True, float("nan")])
def test_parse_money_rejects_float_and_bool(value):
    with pytest.raises(ValueError):
        parse_money(value)


@pytest.mark.parametrize("value,expected", [("76125", "76125.00"), ("76125.5", "76125.50"), ("0.01", "0.01"),
                                            (" 10.10 ", "10.10"), (5, "5.00")])
def test_parse_money_accepts(value, expected):
    assert format(parse_money(value), "f") == expected


def _body(**over):
    b = {"ticket_id": "REQ-1", "invoice_id": "INV-5001", "purchase_order_id": "DRAFT-PO-1", "amount": "100.00",
         "currency": "INR", "simulation_only": True, "confirmed_by_user": True, "idempotency_key": "key-12345678"}
    b.update(over)
    return b


@pytest.mark.parametrize("amount", ["NaN", "inf", "-5", "0", "1.001", 12.5])
def test_request_rejects_bad_amounts(amount):
    with pytest.raises(ValidationError):
        PaymentSimulationIn(**_body(amount=amount))


@pytest.mark.parametrize("extra", [{"mode": "LIVE"}, {"money_transferred": True}, {"provider": "stripe"},
                                   {"simulation_only": False}])
def test_request_rejects_client_overrides(extra):
    with pytest.raises(ValidationError):
        PaymentSimulationIn(**_body(**extra))
