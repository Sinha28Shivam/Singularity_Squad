"""Tiny dependency-free PDF generator for tests (shared by policy RAG test suites).

``make_pdf([["line 1", "line 2"], ["page 2 line"]])`` returns the bytes of a valid PDF 1.4 with one page per
inner list, each line drawn in Helvetica. An empty inner list produces a blank (low-text, image-like) page.
Characters are encoded with WinAnsi; the rupee sign (U+20B9) is supported through a custom /Differences
glyph mapping so rate-card fixtures can say "₹18,000 / event". Other non-WinAnsi characters become "?".
``encrypt=True`` adds an /Encrypt trailer entry, which pypdf reports as an encrypted document.
"""
from __future__ import annotations

_RUPEE_CODE = 0x81  # unused WinAnsi slot remapped to the rupee glyph


def _build_cmap() -> bytes:
    entries = []
    for code in range(0x20, 0x100):
        if code == _RUPEE_CODE:
            uni = 0x20B9
        else:
            try:
                uni = ord(bytes([code]).decode("cp1252"))
            except UnicodeDecodeError:
                continue
        entries.append(b"<%02X> <%04X>" % (code, uni))
    body = b""
    for i in range(0, len(entries), 100):
        chunk = entries[i:i + 100]
        body += b"%d beginbfchar\n" % len(chunk) + b"\n".join(chunk) + b"\nendbfchar\n"
    return (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            b"1 begincodespacerange\n<00> <FF>\nendcodespacerange\n" + body +
            b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")


_TO_UNICODE_CMAP = _build_cmap()


def _encode_line(text: str) -> bytes:
    out = bytearray()
    for ch in text:
        if ch == "\u20b9":
            out.append(_RUPEE_CODE)
            continue
        try:
            b = ch.encode("cp1252")
        except UnicodeEncodeError:
            b = b"?"
        out += b
    escaped = bytearray()
    for byte in out:
        if byte in (0x28, 0x29, 0x5C):  # ( ) \
            escaped += b"\\" + bytes([byte])
        elif byte < 0x20 or byte > 0x7E:
            escaped += b"\\%03o" % byte
        else:
            escaped.append(byte)
    return bytes(escaped)


def _content_stream(lines: list[str], font_size: int = 11, leading: int = 14) -> bytes:
    if not lines:
        return b"q 0.9 g 72 72 468 648 re f Q\n"  # a filled rectangle, no text: image-only/blank page
    parts = [b"BT", b"/F1 %d Tf" % font_size, b"%d TL" % leading, b"72 770 Td"]
    for line in lines:
        parts.append(b"(" + _encode_line(line) + b") Tj T*")
    parts.append(b"ET")
    return b"\n".join(parts) + b"\n"


def make_pdf(pages: list[list[str]], *, encrypt: bool = False, title: str | None = None) -> bytes:
    """Build a minimal multi-page PDF. ``pages`` is a list of pages, each a list of text lines."""
    if not pages:
        raise ValueError("at least one page is required")
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id = add(b"")  # placeholder, filled below
    pages_id = add(b"")
    cmap = _TO_UNICODE_CMAP
    to_unicode_id = add(b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"endstream")
    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /Type /Encoding "
                  b"/BaseEncoding /WinAnsiEncoding /Differences [129 /uni20B9] >> /ToUnicode %d 0 R >>"
                  % to_unicode_id)
    page_ids = []
    for lines in pages:
        stream = _content_stream(list(lines))
        content_id = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_id, font_id, content_id)))
    objects[catalog_id - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    info_id = add(b"<< /Title (" + _encode_line(title or "Synthetic test document") + b") /Producer (pdf_factory) >>")
    encrypt_id = None
    if encrypt:
        encrypt_id = add(b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 "
                         b"/O <" + b"00" * 32 + b"> /U <" + b"00" * 32 + b"> >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = b"<< /Size %d /Root %d 0 R /Info %d 0 R" % (len(objects) + 1, catalog_id, info_id)
    if encrypt_id:
        trailer += b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (encrypt_id, b"ab" * 16, b"ab" * 16)
    trailer += b" >>"
    out += b"trailer\n" + trailer + b"\nstartxref\n%d\n%%%%EOF\n" % xref_at
    return bytes(out)


def make_corrupt_pdf() -> bytes:
    """Bytes with a PDF signature but no parsable document structure."""
    return b"%PDF-1.4\n" + b"\x00garbage-not-a-pdf-object\x13" * 40 + b"\n%%EOF\n"


def text_page(title: str, *paragraphs: str, words_per_line: int = 12) -> list[str]:
    """Convenience: a page with a heading line followed by paragraphs wrapped to short lines."""
    lines = [title]
    for para in paragraphs:
        words = para.split()
        for i in range(0, len(words), words_per_line):
            lines.append(" ".join(words[i:i + words_per_line]))
    return lines
