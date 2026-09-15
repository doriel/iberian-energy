"""Response unpacking.

ENTSO-E does not always answer with a single XML document. Unavailability
documents in particular come back as a ZIP holding many files, and decoding
those bytes to text corrupts the archive. The failure looks like a parse error
at line 1 column 2, which names nothing useful, so these tests keep the
handling honest.
"""

from __future__ import annotations

import gzip
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iberian.ingestion.entsoe import RawResponse  # noqa: E402

XML_ONE = b"<?xml version='1.0'?><Publication_MarketDocument><mRID>1</mRID></Publication_MarketDocument>"
XML_TWO = b"<?xml version='1.0'?><Publication_MarketDocument><mRID>2</mRID></Publication_MarketDocument>"


def make(content: bytes) -> RawResponse:
    return RawResponse(
        params={}, content=content, fetched_at_utc="2026-09-14T00:00:00Z", status_code=200
    )


def zipped(*payloads: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, payload in enumerate(payloads, start=1):
            archive.writestr(f"doc_{index}.xml", payload)
    return buffer.getvalue()


def test_plain_xml_is_one_document():
    response = make(XML_ONE)
    assert not response.is_zip
    assert response.documents() == [XML_ONE.decode()]
    assert response.suggested_extension == ".xml"


def test_zip_is_detected_and_every_document_returned():
    response = make(zipped(XML_ONE, XML_TWO))

    assert response.is_zip
    assert response.suggested_extension == ".zip"
    documents = response.documents()
    assert len(documents) == 2
    assert "<mRID>1</mRID>" in documents[0]
    assert "<mRID>2</mRID>" in documents[1]


def test_body_refuses_to_guess_on_a_zip():
    """Returning just the first document would lose the rest in silence."""
    response = make(zipped(XML_ONE, XML_TWO))

    with pytest.raises(ValueError, match="ZIP archive"):
        _ = response.body


def test_gzip_is_handled():
    response = make(gzip.compress(XML_ONE))
    assert response.documents() == [XML_ONE.decode()]


def test_byte_order_mark_does_not_break_parsing():
    response = make(b"\xef\xbb\xbf" + XML_ONE)
    assert response.documents()[0].startswith("<?xml")


def test_unknown_payload_says_what_it_actually_got():
    response = make(b"\x00\x01garbage")

    with pytest.raises(ValueError, match="neither XML nor a ZIP"):
        response.documents()


def test_acknowledgement_detected_on_bytes():
    ack = b"<Acknowledgement_MarketDocument><Reason/></Acknowledgement_MarketDocument>"
    assert make(ack).is_empty
    assert not make(XML_ONE).is_empty


def test_zip_is_never_reported_as_empty():
    """A ZIP holds real documents, whatever the Acknowledgement heuristic says."""
    assert not make(zipped(XML_ONE)).is_empty
