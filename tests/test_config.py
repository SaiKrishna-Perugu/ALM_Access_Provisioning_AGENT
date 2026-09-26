"""Environment identity, TLS policy and the production write gate.

Nothing used to distinguish a PROD run from a TEST run except one hand-edited
line in .env, which was toggled at least four times in a single day.
"""
from __future__ import annotations

import pytest

import alm_config


def test_prod_is_inferred_from_the_server_names(monkeypatch):
    monkeypatch.setenv("EWM_SERVER", "https://prsse.example.intra/ccm")
    monkeypatch.setenv("JTS_SERVER", "https://prsse.example.intra/jts")
    assert alm_config.alm_env() == "PROD"


def test_test_is_inferred_from_the_tst_suffix(monkeypatch):
    monkeypatch.setenv("EWM_SERVER", "https://prssetst.example.intra/ccm")
    monkeypatch.setenv("JTS_SERVER", "https://prssetst.example.intra/jts")
    assert alm_config.alm_env() == "TEST"


def test_explicit_alm_env_wins(monkeypatch):
    monkeypatch.setenv("EWM_SERVER", "https://prsse.example.intra/ccm")
    monkeypatch.setenv("ALM_ENV", "TEST")
    assert alm_config.alm_env() == "TEST"


def test_unconfigured_is_unknown_not_prod():
    assert alm_config.alm_env() == "UNKNOWN"


def test_split_environment_is_detected(monkeypatch):
    """EWM on PROD with JTS still on TEST comments about test users on live work items."""
    monkeypatch.setenv("EWM_SERVER", "https://prsse.example.intra/ccm")
    monkeypatch.setenv("JTS_SERVER", "https://prssetst.example.intra/jts")
    assert alm_config.env_mismatch() == "EWM_SERVER is PROD but JTS_SERVER is TEST"


def test_consistent_environment_reports_no_mismatch(monkeypatch):
    monkeypatch.setenv("EWM_SERVER", "https://prssetst.example.intra/ccm")
    monkeypatch.setenv("JTS_SERVER", "https://prssetst.example.intra/jts")
    assert alm_config.env_mismatch() is None


def test_banner_shouts_about_production(monkeypatch):
    monkeypatch.setenv("EWM_SERVER", "https://prsse.example.intra/ccm")
    banner = alm_config.banner("attach", commit=True)
    assert "!! PRODUCTION !!" in banner
    assert "COMMIT (writes)" in banner


def test_banner_marks_a_dry_run(monkeypatch):
    monkeypatch.setenv("EWM_SERVER", "https://prssetst.example.intra/ccm")
    assert "DRY RUN" in alm_config.banner()


# --------------------------------------------------------------------- TLS

def test_ca_bundle_is_used_when_it_exists(monkeypatch, tmp_path):
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
    monkeypatch.setenv("ALM_CA_BUNDLE", str(bundle))
    assert alm_config.tls_verify() == str(bundle)
    assert alm_config.tls_is_verified()


def test_a_missing_ca_bundle_is_a_hard_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ALM_CA_BUNDLE", str(tmp_path / "absent.pem"))
    with pytest.raises(SystemExit):
        alm_config.tls_verify()


def test_tls_verify_true_uses_the_system_store(monkeypatch):
    monkeypatch.setenv("ALM_TLS_VERIFY", "true")
    assert alm_config.tls_verify() is True


def test_strict_refuses_to_run_without_a_bundle(monkeypatch):
    monkeypatch.setenv("ALM_TLS_VERIFY", "strict")
    with pytest.raises(SystemExit):
        alm_config.tls_verify()


def test_unconfigured_tls_is_unverified_and_warns(monkeypatch, capsys):
    monkeypatch.delenv("ALM_TLS_VERIFY", raising=False)
    monkeypatch.delenv("ALM_CA_BUNDLE", raising=False)
    monkeypatch.setenv("ALM_ENV", "TEST")
    monkeypatch.setattr(alm_config, "_warned", False)
    assert alm_config.tls_verify() is False
    assert "verification is DISABLED" in capsys.readouterr().err


def test_unconfigured_tls_refuses_in_production(monkeypatch):
    monkeypatch.delenv("ALM_TLS_VERIFY", raising=False)
    monkeypatch.delenv("ALM_CA_BUNDLE", raising=False)
    monkeypatch.setenv("ALM_ENV", "PROD")
    with pytest.raises(SystemExit) as exc_info:
        alm_config.tls_verify()
    assert "Refusing to run over unverified TLS in PRODUCTION" in str(exc_info.value)


# ------------------------------------------------------------- PROD gate

def test_non_production_needs_no_confirmation(monkeypatch):
    monkeypatch.setenv("ALM_ENV", "TEST")
    assert alm_config.confirm_prod_write("importing users")


def test_production_passes_with_a_preapproved_confirmation(monkeypatch):
    monkeypatch.setenv("ALM_ENV", "PROD")
    monkeypatch.setenv(alm_config.CONFIRM_ENV, alm_config.CONFIRM_PHRASE)
    assert alm_config.confirm_prod_write("importing users")


def test_production_is_refused_when_the_phrase_is_wrong(monkeypatch, capsys):
    monkeypatch.setenv("ALM_ENV", "PROD")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
    assert not alm_config.confirm_prod_write("importing users")
    assert "Not confirmed" in capsys.readouterr().out


def test_production_proceeds_when_the_phrase_is_typed(monkeypatch):
    monkeypatch.setenv("ALM_ENV", "PROD")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "prod")
    assert alm_config.confirm_prod_write("importing users")


def test_non_interactive_production_write_is_refused(monkeypatch, capsys):
    """A scheduled or piped run must not be able to write to PROD by accident."""
    monkeypatch.setenv("ALM_ENV", "PROD")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert not alm_config.confirm_prod_write("attaching evidence")
    assert "no terminal is available to confirm" in capsys.readouterr().out
