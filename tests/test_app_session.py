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


# --- the store --------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def a_store(clock=None, **kwargs):
    return session.Sessions(clock=clock or Clock(), **kwargs)


def test_a_session_gives_back_the_identity_it_was_opened_with():
    store = a_store()
    session_id = store.open("ana@example.com", "db-credential")
    identity = store.get(session_id)

    assert identity.email == "ana@example.com"
    assert identity.database_credential == "db-credential"


def test_an_unknown_session_is_nothing():
    assert a_store().get("never-existed") is None
    assert a_store().get(None) is None


def test_a_session_ends_when_its_credential_would():
    """The whole reason the lifetime is shorter than the credential's hour."""
    clock = Clock()
    store = a_store(clock, lifetime=100)
    session_id = store.open("ana@example.com", "db")

    clock.advance(99)
    assert store.get(session_id) is not None

    clock.advance(2)
    assert store.get(session_id) is None


def test_an_expired_session_is_not_kept_around():
    clock = Clock()
    store = a_store(clock, lifetime=10)
    store.open("ana@example.com", "db")

    clock.advance(20)
    store.get("anything")  # a read of something else must not keep it alive
    store.open("other@example.com", "db")
    assert len(store) == 1


def test_closing_a_session_removes_the_credential():
    store = a_store()
    session_id = store.open("ana@example.com", "db")
    store.close(session_id)
    assert store.get(session_id) is None


def test_closing_something_that_is_not_a_session_is_harmless():
    a_store().close(None)
    a_store().close("nonsense")


def test_the_store_does_not_grow_without_limit():
    """An open sign in page on a small instance is an open invitation."""
    store = a_store(limit=5)
    for index in range(20):
        store.open(f"person{index}@example.com", "db")
    assert len(store) <= 5


def test_the_oldest_session_is_the_one_dropped():
    clock = Clock()
    store = a_store(clock, limit=2)
    first = store.open("first@example.com", "db")
    clock.advance(1)
    second = store.open("second@example.com", "db")
    clock.advance(1)
    store.open("third@example.com", "db")

    assert store.get(first) is None
    assert store.get(second) is not None


def test_two_people_get_two_sessions():
    store = a_store()
    ana = store.open("ana@example.com", "ana-credential")
    rui = store.open("rui@example.com", "rui-credential")

    assert ana != rui
    assert store.get(ana).database_credential == "ana-credential"
    assert store.get(rui).database_credential == "rui-credential"