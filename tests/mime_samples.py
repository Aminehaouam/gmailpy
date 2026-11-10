"""
Realistic raw drafts in the formats Gmail produces (CRLF line endings,
Gmail-style boundaries, quoted-printable/base64 bodies, X-Attachment-Id ...).
"""

from __future__ import annotations

import base64
import os
import quopri
from email.header import Header

CRLF = "\r\n"


def _qp(text: str) -> str:
    """Quoted-printable encode unicode text (UTF-8) with CRLF line breaks."""
    encoded = quopri.encodestring(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    return encoded.decode("ascii").replace("\r\n", "\n").replace("\n", CRLF)


def _encode_header(value: str) -> str:
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()


def _b64_lines(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return CRLF.join(encoded[i:i + 76] for i in range(0, len(encoded), 76))


def make_pdf_bytes(label: str = "x", size: int = 4000) -> bytes:
    """A small but structurally complete PDF with unique content."""
    filler = (label.encode("utf-8") * (size // max(1, len(label)) + 1))[:size]
    return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Label (" + label.encode() + b") >>\nendobj\n" + \
        filler + b"\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def _headers(subject: str, to: str, cc: str | None, bcc: str | None, extra: list[str] | None) -> list[str]:
    lines = [
        "MIME-Version: 1.0",
        "Date: Mon, 9 Nov 2026 10:00:00 +0100",
        "Message-ID: <CAJx3Lp7" + os.urandom(4).hex() + "@mail.gmail.com>",
        f"Subject: {_encode_header(subject)}",
        "From: Amine Haouam <amine.test@gmail.com>",
        f"To: {to}",
    ]
    if cc:
        lines.append(f"Cc: {cc}")
    if bcc:
        lines.append(f"Bcc: {bcc}")
    lines += extra or []
    return lines


def _alternative_block(boundary: str, plain: str, html: str) -> list[str]:
    plain_qp, html_qp = _qp(plain), _qp(html)
    return [
        f"--{boundary}",
        'Content-Type: text/plain; charset="UTF-8"',
        "Content-Transfer-Encoding: quoted-printable",
        "",
        plain_qp,
        "",
        f"--{boundary}",
        'Content-Type: text/html; charset="UTF-8"',
        "Content-Transfer-Encoding: quoted-printable",
        "",
        html_qp,
        "",
        f"--{boundary}--",
    ]


def alternative_draft(subject: str, to: str, *, cc: str | None = None, bcc: str | None = None,
                      body_plain: str = "Sehr geehrte Damen und Herren,", body_html: str | None = None,
                      extra_headers: list[str] | None = None) -> bytes:
    """Gmail web draft without attachments: multipart/alternative."""
    boundary = "000000000000" + os.urandom(6).hex()
    html = body_html or f'<div dir="ltr">{body_plain}<br><br><b>Mit freundlichen Grüßen</b><br>Amine Haouam</div>'
    lines = _headers(subject, to, cc, bcc, extra_headers)
    lines.append(f'Content-Type: multipart/alternative; boundary="{boundary}"')
    lines.append("")
    lines += _alternative_block(boundary, body_plain + "\n\nMit freundlichen Grüßen\nAmine Haouam", html)
    lines.append("")
    return CRLF.join(lines).encode("ascii")


def mixed_draft_with_attachment(subject: str, to: str, attachment: bytes, attachment_name: str, *,
                                body_plain: str = "Anbei meine Unterlagen.",
                                attachment_type: str = "application/pdf") -> bytes:
    """Gmail draft that already has an attachment: multipart/mixed."""
    outer = "000000000000" + os.urandom(6).hex()
    inner = "000000000000" + os.urandom(6).hex()
    lines = _headers(subject, to, None, None, None)
    lines.append(f'Content-Type: multipart/mixed; boundary="{outer}"')
    lines += ["", f"--{outer}", f'Content-Type: multipart/alternative; boundary="{inner}"', ""]
    lines += _alternative_block(inner, body_plain, f'<div dir="ltr">{body_plain}</div>')
    lines += [
        "",
        f"--{outer}",
        f'Content-Type: {attachment_type}; name="{attachment_name}"',
        f'Content-Disposition: attachment; filename="{attachment_name}"',
        "Content-Transfer-Encoding: base64",
        "Content-ID: <f_lw3k9s0d0>",
        "X-Attachment-Id: f_lw3k9s0d0",
        "",
        _b64_lines(attachment),
        f"--{outer}--",
        "",
    ]
    return CRLF.join(lines).encode("ascii")


def related_draft_with_inline_image(subject: str, to: str, image: bytes) -> bytes:
    """HTML draft with an inline image (signature logo): multipart/related."""
    related = "000000000000" + os.urandom(6).hex()
    alt = "000000000000" + os.urandom(6).hex()
    lines = _headers(subject, to, None, None, None)
    lines.append(f'Content-Type: multipart/related; boundary="{related}"')
    lines += ["", f"--{related}", f'Content-Type: multipart/alternative; boundary="{alt}"', ""]
    lines += _alternative_block(
        alt, "Sehr geehrte Damen und Herren,",
        '<div dir="ltr">Sehr geehrte Damen und Herren,<br><img src="cid:ii_logo1" alt="logo"></div>')
    lines += [
        "",
        f"--{related}",
        'Content-Type: image/png; name="logo.png"',
        'Content-Disposition: inline; filename="logo.png"',
        "Content-Transfer-Encoding: base64",
        "Content-ID: <ii_logo1>",
        "X-Attachment-Id: ii_logo1",
        "",
        _b64_lines(image),
        f"--{related}--",
        "",
    ]
    return CRLF.join(lines).encode("ascii")


def plain_text_draft(subject: str, to: str, body: str) -> bytes:
    """Single-part text/plain draft (as created by some API tools), QP-encoded UTF-8."""
    lines = _headers(subject, to, None, None, None)
    lines += ['Content-Type: text/plain; charset="UTF-8"', "Content-Transfer-Encoding: quoted-printable",
              "", _qp(body), ""]
    return CRLF.join(lines).encode("ascii")


def eight_bit_draft(subject: str, to: str, body: str) -> bytes:
    """Single-part text/plain with raw 8-bit UTF-8 bytes in the body."""
    head = CRLF.join(_headers(subject, to, None, None, None) + [
        'Content-Type: text/plain; charset="UTF-8"', "Content-Transfer-Encoding: 8bit", "", ""])
    return head.encode("ascii") + body.replace("\n", "\r\n").encode("utf-8") + b"\r\n"


def html_only_draft(subject: str, to: str, html: str) -> bytes:
    head = CRLF.join(_headers(subject, to, None, None, None) + [
        'Content-Type: text/html; charset="UTF-8"', "Content-Transfer-Encoding: base64", "", ""])
    return head.encode("ascii") + _b64_lines(html.encode("utf-8")).encode("ascii") + b"\r\n"
