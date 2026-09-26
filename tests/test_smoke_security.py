"""Tests for alm_core.smoke: TLS certificate verification modes."""
from __future__ import annotations

import ssl
from unittest.mock import MagicMock, patch

from alm_core.smoke import check_dns, check_tls


def test_check_tls_uses_cert_none_only_when_ca_bundle_is_false():
    fake_socket = MagicMock()
    fake_tls = MagicMock()
    fake_tls.getpeercert.return_value = {"subject": [[("commonName", "test-server")]]}
    fake_tls.__enter__.return_value = fake_tls
    fake_socket.__enter__.return_value = fake_socket

    with (
        patch("socket.create_connection", return_value=fake_socket),
        patch("ssl.create_default_context") as mock_create_ctx,
    ):
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = fake_tls
        mock_create_ctx.return_value = mock_ctx

        # 1. ca_bundle is False -> explicitly disabled verification
        check_tls("https://example.intra:443/jts", ca_bundle=False)
        assert mock_ctx.check_hostname is False
        assert mock_ctx.verify_mode == ssl.CERT_NONE

        # 2. ca_bundle is a path -> verification enabled with cafile
        mock_ctx.reset_mock()
        mock_ctx.check_hostname = True
        mock_ctx.verify_mode = ssl.CERT_REQUIRED
        check_tls("https://example.intra:443/jts", ca_bundle="/certs/corp-ca.pem")
        mock_create_ctx.assert_called_with(cafile="/certs/corp-ca.pem")
        assert mock_ctx.check_hostname is True
        assert mock_ctx.verify_mode == ssl.CERT_REQUIRED

        # 3. ca_bundle is True (system store) -> verification enabled
        mock_ctx.reset_mock()
        mock_ctx.check_hostname = True
        mock_ctx.verify_mode = ssl.CERT_REQUIRED
        check_tls("https://example.intra:443/jts", ca_bundle=True)
        mock_create_ctx.assert_called_with(cafile=None)
        assert mock_ctx.check_hostname is True
        assert mock_ctx.verify_mode == ssl.CERT_REQUIRED


def test_check_tls_no_host_configured():
    chk = check_tls("", ca_bundle=True)
    assert chk.ok is False
    assert "no host" in chk.detail


def test_check_dns_no_host():
    chk = check_dns("")
    assert chk.ok is False
    assert "no host" in chk.detail
