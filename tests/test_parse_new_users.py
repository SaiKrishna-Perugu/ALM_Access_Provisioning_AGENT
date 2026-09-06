"""Golden cases for the New Users parser.

This is the function that decides who gets provisioned. It silently dropped any
row it could not parse, so a malformed entry meant a user quietly never got
access and nothing anywhere said so.
"""
from __future__ import annotations

import alm_access_requests as aar


def ids(field: str) -> list[str]:
    return [u["userid"] for u in aar.parse_new_users(field)]


def test_single_entry():
    parsed = aar.parse_new_users("LOVELACE,ADA,ada.lovelace@example.com,AB12345;")
    assert parsed == [{"userid": "AB12345", "email": "ada.lovelace@example.com",
                       "first_name": "ADA", "last_name": "LOVELACE"}]


def test_multiple_entries_and_trailing_semicolon():
    field = ("LOVELACE,ADA,ada@example.com,AB12345;"
             "HOPPER,GRACE,grace@example.com,CD67890;")
    assert ids(field) == ["AB12345", "CD67890"]


def test_entry_without_trailing_semicolon_is_still_parsed():
    assert ids("LOVELACE,ADA,ada@example.com,AB12345") == ["AB12345"]


def test_middle_name_with_extra_comma_keeps_the_user_id():
    # "FIRST, MIDDLE" produces five fields, not four; the id is still last.
    parsed = aar.parse_new_users("HOPPER,GRACE,BREWSTER,grace@example.com,CD67890;")
    assert parsed[0]["userid"] == "CD67890"
    assert parsed[0]["first_name"] == "GRACE,BREWSTER"


def test_row_without_an_email_is_dropped():
    assert ids("LOVELACE,ADA,not-an-email,AB12345;") == []


def test_row_missing_the_user_id_is_dropped():
    assert ids("LOVELACE,ADA,ada@example.com,;") == []


def test_too_few_fields_is_dropped():
    assert ids("LOVELACE,AB12345;") == []


def test_whitespace_around_fields_is_stripped():
    assert ids("  LOVELACE , ADA , ada@example.com , AB12345 ;") == ["AB12345"]


def test_empty_and_blank_fields():
    assert aar.parse_new_users("") == []
    assert aar.parse_new_users("   ") == []
    assert aar.parse_new_users(";;;") == []


def test_free_text_fallback_yields_no_users():
    """The '(from Summary)' fallback is prose, not a user list - it must parse to nothing."""
    assert aar.parse_new_users("(from Summary) Please add Ada to ALM, id AB12345") == []
    assert aar.parse_new_users("(from Justification) new starter") == []


def test_mixed_valid_and_invalid_rows_keeps_the_valid_ones():
    field = ("LOVELACE,ADA,ada@example.com,AB12345;"
             "BROKEN ROW;"
             "HOPPER,GRACE,grace@example.com,CD67890;")
    assert ids(field) == ["AB12345", "CD67890"]


def test_collect_users_deduplicates_and_keeps_every_source_work_item():
    rows = [
        {"ID": "4348411", "Summary": "First", "New Users":
            "LOVELACE,ADA,ada@example.com,AB12345;", "Access Type": "", "Domain": "",
         "Work Area(s)": ""},
        {"ID": "4348690", "Summary": "Second", "New Users":
            "LOVELACE,ADA,ada@example.com,AB12345;", "Access Type": "", "Domain": "",
         "Work Area(s)": ""},
    ]
    collected = aar.collect_users(rows)
    assert len(collected) == 1
    assert [s["work_item_id"] for s in collected[0]["source_work_items"]] == ["4348411", "4348690"]
