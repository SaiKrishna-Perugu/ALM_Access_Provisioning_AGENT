"""Tests for alm_core.oslc: defusedxml security, hostile payloads, and error handling."""
from __future__ import annotations

import pytest

# alm_core needs pydantic: these run in the agents CI job, and skip in the
# CLI-only test job that installs requirements.txt alone.
pytest.importorskip("pydantic")

from alm_core.errors import AuthorizationError, ParseError  # noqa: E402
from alm_core.oslc import parse_xml  # noqa: E402


def test_parse_xml_valid():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
        <oslc:WorkItem xmlns:oslc="http://open-services.net/ns/core#">
            <dc:title xmlns:dc="http://purl.org/dc/terms/">Test Task</dc:title>
        </oslc:WorkItem>
    </rdf:RDF>
    """
    root = parse_xml(xml)
    assert root is not None
    assert "RDF" in root.tag


def test_parse_xml_billion_laughs_entity_bomb_rejected():
    bomb = """<?xml version="1.0"?>
    <!DOCTYPE lolz [
     <!ENTITY lol "lol">
     <!ELEMENT lolz (#PCDATA)>
     <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
     <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
    ]>
    <lolz>&lol2;</lolz>
    """
    with pytest.raises(ParseError) as exc_info:
        parse_xml(bomb)
    assert "hostile XML payload rejected" in str(exc_info.value)


def test_parse_xml_html_login_redirect_raises_authorization_error():
    html_page = """<!DOCTYPE html>
    <html>
      <head><title>Jazz Form Login</title></head>
      <body>Please log in</body>
    </html>
    """
    with pytest.raises(AuthorizationError) as exc_info:
        parse_xml(html_page)
    assert "Jazz web UI, not OSLC XML" in str(exc_info.value)


def test_parse_xml_malformed_xml_raises_parse_error():
    malformed = "<root><unclosedTag>test</root>"
    with pytest.raises(ParseError) as exc_info:
        parse_xml(malformed)
    assert "unparseable OSLC XML" in str(exc_info.value)
