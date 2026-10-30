"""تست‌های اعتبارسنجیِ نوعِ واقعی، سقفِ حجم و استخراجِ متنِ پیوست‌ها."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from src.task_sources import (
    ATTACHMENT_FAILED,
    ATTACHMENT_PROCESSED,
    ATTACHMENT_TOO_LARGE,
    ATTACHMENT_UNSUPPORTED,
)
from src.task_sources.attachments import (
    AttachmentError,
    AttachmentTooLarge,
    extract_text,
    is_supported,
    process_attachment,
)


def text_pdf(*pages: str) -> bytes:
    """یک PDFِ کوچکِ معتبر با متن — بدونِ هیچ فایلِ بیرونی."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    font = 3 + len(pages) * 2
    for index, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + index * 2} 0 R >>"
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content.decode()}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return output.getvalue()


def blank_pdf(*, password: str = "") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _process(name: str, data: bytes = b"", *, size: int | None = None, **limits):
    return process_attachment(
        filename=name,
        media_type="x",
        size=len(data) if size is None else size,
        download=lambda limit: data,
        max_bytes=limits.get("max_bytes", 1000),
        max_chars=limits.get("max_chars", 1000),
    )


class TestSupportedTypes:
    @pytest.mark.parametrize("name", ["a.txt", "a.md", "a.json", "a.yaml", "A.YML", "a.pdf"])
    def test_supported(self, name):
        assert is_supported(name)

    @pytest.mark.parametrize("name", ["a.png", "a.docx", "a.xlsx", "a", "a.txt.exe", "a.sh"])
    def test_unsupported_types_are_reported_and_never_downloaded(self, name):
        downloads = []
        result = process_attachment(
            filename=name,
            media_type="",
            size=1,
            download=lambda limit: downloads.append(limit) or b"x",
            max_bytes=10,
            max_chars=10,
        )
        assert result.status == ATTACHMENT_UNSUPPORTED
        assert downloads == []


class TestSizeLimits:
    def test_a_file_over_the_limit_is_not_downloaded(self):
        downloads = []
        result = process_attachment(
            filename="big.txt",
            media_type="",
            size=11,
            download=lambda limit: downloads.append(limit) or b"",
            max_bytes=10,
            max_chars=10,
        )
        assert result.status == ATTACHMENT_TOO_LARGE
        assert downloads == []

    def test_a_download_that_exceeds_the_limit_is_too_large(self):
        def download(limit):
            raise AttachmentTooLarge

        result = process_attachment(
            filename="a.txt", media_type="", size=1, download=download, max_bytes=10, max_chars=10
        )
        assert result.status == ATTACHMENT_TOO_LARGE

    def test_more_bytes_than_allowed_is_too_large(self):
        assert _process("a.txt", b"x" * 20, size=5, max_bytes=10).status == ATTACHMENT_TOO_LARGE

    def test_long_text_is_truncated_and_says_so(self):
        result = _process("a.txt", b"x" * 50, max_chars=10)
        assert result.status == ATTACHMENT_PROCESSED
        assert result.content == "x" * 10
        assert result.truncated
        assert "first 10 of 50 characters" in result.detail


class TestTextFiles:
    def test_utf8_text_is_normalised(self):
        result = _process("notes.txt", "سلام\r\nدنیا !".encode())
        assert result.status == ATTACHMENT_PROCESSED
        assert result.content == "سلام\nدنیا !"

    def test_utf8_with_bom_and_utf16_with_bom(self):
        assert _process("a.md", b"\xef\xbb\xbfhello").content == "hello"
        assert _process("a.md", "hello".encode("utf-16")).content == "hello"

    @pytest.mark.parametrize(
        "data",
        [
            b"EPFS\x00\x00\x00\x0f\x02",  # binary behind .txt (like a real Jira attachment)
            b"%PDF-1.4 ...",
            b"PK\x03\x04zip",
            b"\x89PNG\r\n",
            b"MZ\x90\x00",
        ],
    )
    def test_binary_content_behind_a_text_extension_fails(self, data):
        result = _process("ExcelType.txt", data)
        assert result.status == ATTACHMENT_FAILED
        assert "not text" in result.detail
        assert result.content == ""

    def test_invalid_utf8_fails(self):
        assert _process("a.txt", b"\xff\xfe\xfa\x80abc"[2:]).status == ATTACHMENT_FAILED

    def test_valid_json(self):
        result = _process("a.json", b'{"id": 1}')
        assert (result.status, result.detail) == (ATTACHMENT_PROCESSED, "")

    def test_invalid_json_is_kept_with_a_note(self):
        result = _process("a.json", b'{"id": ')
        assert result.status == ATTACHMENT_PROCESSED
        assert result.content == '{"id":'
        assert "not valid JSON" in result.detail

    def test_yaml_is_read_as_text_and_never_parsed(self):
        result = _process("a.yaml", b"!!python/object/apply:os.system ['x']\n")
        assert result.status == ATTACHMENT_PROCESSED
        assert result.content.startswith("!!python/object")

    def test_a_failed_download_is_reported(self):
        def download(limit):
            raise ConnectionError("reset")

        result = process_attachment(
            filename="a.txt", media_type="", size=1, download=download, max_bytes=10, max_chars=10
        )
        assert result.status == ATTACHMENT_FAILED
        assert result.detail == "download failed: reset"


class TestPdf:
    def test_text_is_extracted_per_page(self):
        result = _process("spec.pdf", text_pdf("GET /api/admin/types", "Second page"))
        assert result.status == ATTACHMENT_PROCESSED
        assert "[page 1]\nGET /api/admin/types" in result.content
        assert "[page 2]\nSecond page" in result.content

    def test_a_pdf_extension_without_a_pdf_signature_fails(self):
        result = _process("spec.pdf", b"just text")
        assert result.status == ATTACHMENT_FAILED
        assert "not a PDF" in result.detail

    def test_a_corrupted_pdf_fails(self):
        assert _process("spec.pdf", b"%PDF-1.4\n\x00garbage").status == ATTACHMENT_FAILED

    def test_a_pdf_without_text_fails_with_a_reason(self):
        result = _process("scan.pdf", blank_pdf())
        assert result.status == ATTACHMENT_FAILED
        assert "no extractable text" in result.detail

    def test_an_encrypted_pdf_fails(self):
        result = _process("secret.pdf", blank_pdf(password="pw"))
        assert result.status == ATTACHMENT_FAILED
        assert "encrypted" in result.detail


def test_extract_text_rejects_unknown_types():
    with pytest.raises(AttachmentError, match="unsupported"):
        extract_text("a.exe", b"x")
