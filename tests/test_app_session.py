"""The session cookie: what it guarantees, and what it deliberately does not.

Worth being explicit about the claim under test, because it is easy to read this
file as testing authentication. It is not. Anybody may type any name. What is
tested is that the name the server reads back is the name the server issued, and
that a session does not outlive the process that created it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import iberian.app.session as session  # noqa: E402


# --- names --------------------------------------------------------------------


def test_a_missing_name_becomes_the_anonymous_one():
    assert session.clean_name(None) == "guest"
    assert session.clean_name("   ") == "guest"


def test_a_long_name_is_cut_rather_than_refused():
    assert len(session.clean_name("a" * 500)) == session.MAX_NAME


def test_control_characters_do_not_survive():
    """This name is written into a log line and rendered in a page."""
    assert session.clean_name("Ana\nadmin") == "Anaadmin"
    assert "\t" not in session.clean_name("A\tB")


def test_an_ordinary_name_is_left_alone():
    assert session.clean_name("  Ana Ferreira  ") == "Ana Ferreira"


# --- the round trip -----------------------------------------------------------


def test_a_signed_cookie_gives_back_what_went_in():
    cookie = session.sign("Ana Ferreira", "abc123")
    assert session.verify(cookie) == ("Ana Ferreira", "abc123")


def test_a_name_containing_the_separator_survives():
    """The name is encoded, so a dot in it cannot move the field boundaries."""
    cookie = session.sign("A.B.C", "xyz")
    assert session.verify(cookie) == ("A.B.C", "xyz")


def test_accented_and_non_latin_names_survive():
    for name in ["José Mourão", "Ana Söderberg", "李明"]:
        assert session.verify(session.sign(name, "s")) == (name, "s")


# --- what is refused ----------------------------------------------------------


def test_nothing_is_not_a_session():
    assert session.verify(None) is None
    assert session.verify("") is None


def test_a_cookie_that_is_not_ours_is_refused():
    assert session.verify("not-a-cookie") is None
    assert session.verify("a.b") is None
    assert session.verify("a.b.c.d") is None


def test_editing_the_name_invalidates_the_signature():
    """The point of signing. Renaming yourself in the browser must not work."""
    name, payload = "Ana", session.sign("Ana", "s1")
    forged = session.sign("Ana", "s1").replace(
        session._encode(name), session._encode("Zach")
    )
    assert session.verify(forged) is None


def test_editing_the_session_id_invalidates_the_signature():
    encoded_name, session_id, signature = session.sign("Ana", "s1").split(".")
    assert session.verify(f"{encoded_name}.s2.{signature}") is None


def test_a_cookie_signed_with_another_secret_is_refused():
    """Which is what a restart looks like from the browser's side."""
    cookie = session.sign("Ana", "s1")
    original = session.SECRET
    try:
        session.SECRET = b"\x00" * 32
        assert session.verify(cookie) is None
    finally:
        session.SECRET = original
    assert session.verify(cookie) == ("Ana", "s1")


def test_restarting_ends_every_session():
    """The behaviour the sign in page promises, stated as a test.

    Reimporting the module is the closest thing to a restart available here, and
    what it demonstrates is the only property that matters: the secret is
    generated per process and is not derived from anything stable.
    """
    import importlib

    before = session.sign("Ana", "s1")
    restarted = importlib.reload(session)
    assert restarted.verify(before) is None

    # And leave the module usable for whatever runs next.
    importlib.reload(session)


# --- session ids --------------------------------------------------------------


def test_session_ids_do_not_repeat():
    assert len({session.new_session_id() for _ in range(500)}) == 500


def test_a_session_id_has_no_separator_in_it():
    """token_urlsafe never emits one, and the parsing quietly depends on it."""
    assert all(session.SEPARATOR not in session.new_session_id() for _ in range(200))