"""Local text extraction from contract documents.

Boundary: this module reads files that are ALREADY on local disk. It never
downloads anything, never opens a URL, never logs in to the source system, and
never runs OCR. Acquisition is a separate, separately authorized concern.

DOCX is handled with the standard library only (a .docx is a zip of XML), so it
adds no dependency and no supply-chain surface. PDF needs `pypdf` because the
content streams are compressed and CJK text requires font/CMap handling.

The critical contract of this module: when text cannot be extracted, say so.
An empty string must never be returned as if it were an empty document, because
downstream rules would then report "no problems found" on a document nobody
managed to read. That is the same silent-gap failure this project has already
been bitten by twice.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .text_redaction import redact_table

# Extraction guards. A contract that breaches these is reported, not truncated
# silently, so a partial read can never masquerade as a complete one.
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 200
# Below this, treat the document as having no usable text layer. A scanned PDF
# typically yields a handful of stray characters rather than nothing at all.
MIN_USABLE_CHARS = 50

SUPPORTED_SUFFIXES = (".docx", ".pdf", ".xlsx", ".doc")

# Signals. Every one of these means "a rule could not be evaluated properly",
# never "the document is fine".
SIGNAL_NO_TEXT_LAYER = "no_text_layer_needs_ocr"
# Distinct from the above on purpose. A scanned PDF has pages but no text layer,
# and OCR would recover it. A DOCX that parsed cleanly and simply holds almost
# nothing is an EMPTY document, and OCR would not help. Measured on 2026-08-12:
# four contract attachments in the source data are the same 10,127-byte .docx
# containing a single character. Telling the operator "needs OCR" there would
# send them down a pointless path.
SIGNAL_DOCUMENT_EMPTY = "document_has_no_content"
SIGNAL_UNSUPPORTED_FORMAT = "unsupported_document_format"
SIGNAL_FILE_TOO_LARGE = "document_over_size_limit"
SIGNAL_TOO_MANY_PAGES = "document_over_page_limit"
SIGNAL_EXTRACTION_FAILED = "text_extraction_failed"
SIGNAL_PDF_READER_MISSING = "pdf_reader_not_installed"
# An encrypted PDF is a different problem from a corrupt one: it may open
# once a crypto backend is available, or it may need a password nobody has.
# Measured 2026-08-13: two attachments are AES-encrypted with an empty user
# password, and both read fine once `cryptography` is installed.
SIGNAL_PDF_ENCRYPTED = "pdf_encrypted"
SIGNAL_TRUNCATED = "text_truncated"
# A PDF that yields far too little text per page is a scan with a thin text
# layer, not a short document. Measured 2026-08-13 on the largest real
# attachment: 42 pages producing 3,393 characters, about 80 per page, against
# 1,500-3,000 for genuine text PDFs.
#
# MIN_USABLE_CHARS alone passes it as usable, so analysis would cover roughly
# 2% of the document and then report nothing wrong. This is not a hard failure
# - the extracted text is still real - but the caller must know it is holding
# a fragment.
SIGNAL_SPARSE_TEXT_LAYER = "sparse_text_layer_possible_scan"
MIN_CHARS_PER_PAGE = 200

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t　]+")


@dataclass
class ExtractedDocument:
    """Result of a local extraction attempt.

    `usable` is the single thing callers must check. Text rules must refuse to
    run when it is False rather than scoring an empty string.
    """

    source_name: str
    suffix: str
    text: str = ""
    page_count: int = 0
    char_count: int = 0
    signals: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.text) and self.char_count >= MIN_USABLE_CHARS

    def to_dict(self) -> dict[str, Any]:
        """Metadata only. Never serialises the extracted body text, which may
        contain party names, addresses and other free text."""
        return {
            "source_name": self.source_name,
            "suffix": self.suffix,
            "page_count": self.page_count,
            "char_count": self.char_count,
            "usable": self.usable,
            "chars_per_page": self.chars_per_page,
            "signals": list(self.signals),
            "error": self.error,
        }

    @property
    def chars_per_page(self) -> float:
        """Text density. 0 for formats without pages, such as DOCX."""
        return round(self.char_count / self.page_count, 1) if self.page_count else 0.0


def extract_document(path: str | Path) -> ExtractedDocument:
    """Extract text from one local file, reporting why if it cannot."""
    source = Path(path)
    suffix = source.suffix.lower()
    result = ExtractedDocument(source_name=source.name, suffix=suffix)

    if suffix not in SUPPORTED_SUFFIXES:
        result.signals.append(SIGNAL_UNSUPPORTED_FORMAT)
        result.error = f"Unsupported format {suffix!r}; supported: {list(SUPPORTED_SUFFIXES)}"
        return result

    try:
        size = source.stat().st_size
    except OSError as exc:
        result.signals.append(SIGNAL_EXTRACTION_FAILED)
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    if size > MAX_FILE_BYTES:
        result.signals.append(SIGNAL_FILE_TOO_LARGE)
        result.error = f"{size} bytes exceeds the {MAX_FILE_BYTES} byte ceiling"
        return result

    try:
        if suffix == ".docx":
            text, pages = _extract_docx(source)
        elif suffix == ".xlsx":
            text, pages = _extract_xlsx_redacted(source, result)
        elif suffix == ".doc":
            text, pages = _extract_doc(source, result)
        else:
            text, pages = _extract_pdf(source, result)
    except _ExtractionAborted:
        return result
    except Exception as exc:  # noqa: BLE001 - any reader failure must be reported, not raised
        result.signals.append(SIGNAL_EXTRACTION_FAILED)
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.text = text
    result.page_count = pages
    result.char_count = len(text)
    if result.usable and pages > 0 and (len(text) / pages) < MIN_CHARS_PER_PAGE:
        result.signals.append(SIGNAL_SPARSE_TEXT_LAYER)
    if not result.usable:
        # Both are hard stops, but they mean different things to the operator:
        # a scanned PDF could be recovered with OCR, an empty DOCX could not.
        if suffix == ".pdf" and pages > 0:
            result.signals.append(SIGNAL_NO_TEXT_LAYER)
        else:
            result.signals.append(SIGNAL_DOCUMENT_EMPTY)
    return result


class _ExtractionAborted(Exception):
    """Raised once a guard has already recorded its signal on the result."""


def _extract_docx(source: Path) -> tuple[str, int]:
    """Read word/document.xml directly. A .docx is a zip of XML."""
    with zipfile.ZipFile(source) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    # Preserve structure before stripping tags: paragraph and row ends become
    # newlines, tabs and breaks become their characters. Without this every
    # clause would run together and clause-boundary rules would misfire.
    xml = xml.replace("</w:p>", "\n").replace("</w:tr>", "\n")
    xml = xml.replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    return _normalize(_TAG_RE.sub("", xml)), 0


# A legacy `.doc` is an OLE compound file, not a zip, and its text lives in the
# WordDocument stream interleaved with formatting structures. There is no
# faithful pure-standard-library reader.
#
# The danger here is specific and worse than having no reader at all: a
# half-correct parser returns text that LOOKS like prose but has runs missing
# or mojibake in the CJK sections, and that text then feeds contract rules
# which report findings with confident-sounding evidence. Silence is safer
# than plausible garbage.
#
# So this path extracts, then JUDGES what it extracted, and refuses anything
# that does not read as coherent text. Measured against the one `.doc` in the
# source data (9,216 bytes).
SIGNAL_DOC_READER_MISSING = "doc_reader_not_installed"
SIGNAL_DOC_UNRELIABLE = "doc_text_failed_quality_check"
# Below this share of printable CJK/ASCII characters, treat the extraction as
# failed. Legacy .doc mis-parses characteristically produce long runs of
# control bytes and replacement characters rather than a few stray ones.
MIN_DOC_PRINTABLE_RATIO = 0.80
MIN_DOC_CHARS = 40

_DOC_PRINTABLE_RE = re.compile(
    r"[0-9A-Za-z　-〿一-鿿＀-￯\s"
    r"\.,;:!\?\-\(\)\[\]/%¥$&+*=_'\"，。；：！？（）【】、《》…—～]"
)


def _doc_text_is_coherent(text: str) -> bool:
    """Whether extracted legacy-.doc text reads as text rather than as noise."""
    if len(text) < MIN_DOC_CHARS:
        return False
    printable = len(_DOC_PRINTABLE_RE.findall(text))
    return (printable / len(text)) >= MIN_DOC_PRINTABLE_RATIO


def _extract_doc(source: Path, result: ExtractedDocument) -> tuple[str, int]:
    """Legacy binary Word, via the OLE container, with a quality gate."""
    try:
        import olefile
    except ImportError:
        result.signals.append(SIGNAL_DOC_READER_MISSING)
        result.error = (
            "olefile is not installed; cannot read legacy .doc. Install it, or "
            "convert the document to .docx."
        )
        raise _ExtractionAborted from None

    if not olefile.isOleFile(str(source)):
        result.signals.append(SIGNAL_EXTRACTION_FAILED)
        result.error = "File has a .doc suffix but is not an OLE compound file"
        raise _ExtractionAborted from None

    with olefile.OleFileIO(str(source)) as ole:
        if not ole.exists("WordDocument"):
            result.signals.append(SIGNAL_EXTRACTION_FAILED)
            result.error = "OLE file has no WordDocument stream"
            raise _ExtractionAborted from None
        raw = ole.openstream("WordDocument").read()

    # Word stores runs as either CP1252 or UTF-16LE. Try both and keep whichever
    # yields more coherent text, rather than guessing from the FIB - which is
    # where a partial implementation usually goes wrong.
    candidates = []
    for encoding in ("utf-16-le", "cp1252", "gb18030"):
        decoded = raw.decode(encoding, errors="replace")
        # Keep only runs of real text; drop the binary structures around them.
        runs = [run for run in re.split(r"[\x00-\x08\x0b\x0c\x0e-\x1f]{2,}", decoded)
                if len(run.strip()) >= 8]
        candidate = _normalize("\n".join(runs))
        if candidate:
            candidates.append(candidate)

    best = max(candidates, key=lambda c: (_doc_text_is_coherent(c), len(c)), default="")
    if not _doc_text_is_coherent(best):
        # Refuse rather than return something that reads as prose but is not.
        result.signals.append(SIGNAL_DOC_UNRELIABLE)
        result.error = (
            "Legacy .doc text did not pass the coherence check, so it was "
            "discarded rather than analysed. Convert the document to .docx."
        )
        raise _ExtractionAborted from None
    return best, 0


def _extract_xlsx(source: Path) -> list[list[str]]:
    """Read an .xlsx the same way as a .docx: it is a zip of XML.

    No third-party dependency, for the same reason `_extract_docx` has none.

    Returns the CELL GRID, not text: the caller redacts on the grid before
    flattening, because the separators do not survive normalisation.

    Two things a spreadsheet needs that a document does not:

    - **Shared strings.** Cell text is usually not in the sheet. The sheet
      holds an index into `xl/sharedStrings.xml`, flagged by `t="s"`. Reading
      only the sheet yields a page of integers.
    - **Row structure.** A contract spreadsheet is a table, and rules that
      look for a term near an amount need the two to stay on one line. Cells
      are joined with tabs and rows with newlines, so a row reads as a row.
    """
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            xml = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            # One entry per <si>; a single <si> may hold several <t> runs when
            # part of the cell is styled differently, and they concatenate.
            for item in re.findall(r"<si>(.*?)</si>", xml, re.S):
                shared.append("".join(re.findall(r"<t[^>]*>(.*?)</t>", item, re.S)))

        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        grid: list[list[str]] = []
        for sheet in sheets:
            xml = archive.read(sheet).decode("utf-8", errors="replace")
            for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
                cells: list[str] = []
                for cell in re.findall(r"<c\b([^>]*)>(.*?)</c>", row, re.S):
                    attributes, body = cell
                    value_match = re.search(r"<v[^>]*>(.*?)</v>", body, re.S)
                    if 't="s"' in attributes and value_match:
                        index = int(value_match.group(1) or 0)
                        cells.append(shared[index] if 0 <= index < len(shared) else "")
                    elif 't="inlineStr"' in attributes:
                        cells.append("".join(re.findall(r"<t[^>]*>(.*?)</t>", body, re.S)))
                    elif value_match:
                        cells.append(value_match.group(1))
                if any(cell.strip() for cell in cells):
                    grid.append([_unescape_xml(cell) for cell in cells])
    return grid


def _extract_xlsx_redacted(source: Path, result: ExtractedDocument) -> tuple[str, int]:
    """Extract the grid, redact personal rows and columns, THEN flatten.

    Order matters and was wrong on the first attempt: `_normalize` collapses
    tabs into spaces, so redacting after flattening saw no columns at all and
    silently did nothing to the real roster. The grid is the only reliable way
    to know that a bare string is somebody's name - a Chinese name has no shape
    a regex can catch without also catching ordinary contract vocabulary.
    """
    grid = _extract_xlsx(source)
    redacted, signals = redact_table(grid)
    result.signals.extend(signals)
    text = _normalize("\n".join("\t".join(row) for row in redacted))
    return text, 0


def _unescape_xml(text: str) -> str:
    return (
        text.replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&apos;", "'")
        .replace("&amp;", "&")   # last, or an escaped &amp;lt; double-decodes
    )


def _extract_pdf(source: Path, result: ExtractedDocument) -> tuple[str, int]:
    try:
        import pypdf
    except ImportError:
        result.signals.append(SIGNAL_PDF_READER_MISSING)
        result.error = "pypdf is not installed; cannot read PDF text"
        raise _ExtractionAborted from None

    try:
        reader = pypdf.PdfReader(str(source))
        page_count = len(reader.pages)
    except Exception as exc:
        # Report encryption distinctly. "text_extraction_failed" sent the
        # operator looking for a corrupt file when the real answer was a
        # missing crypto backend or a password.
        if "encrypt" in str(exc).lower() or "aes" in str(exc).lower():
            result.signals.append(SIGNAL_PDF_ENCRYPTED)
            result.error = "PDF is encrypted and could not be opened"
            raise _ExtractionAborted from None
        raise
    if reader.is_encrypted:
        # Opened, but say so: the operator should know the source is protected.
        result.signals.append(SIGNAL_PDF_ENCRYPTED)
    if page_count > MAX_PDF_PAGES:
        result.signals.append(SIGNAL_TOO_MANY_PAGES)
        result.error = f"{page_count} pages exceeds the {MAX_PDF_PAGES} page ceiling"
        raise _ExtractionAborted
    parts = [page.extract_text() or "" for page in reader.pages]
    return _normalize("\n".join(parts)), page_count


def _normalize(raw: str) -> str:
    """Collapse horizontal whitespace and drop blank lines, keeping line breaks.

    Deliberately does NOT strip punctuation or dashes: identifier comparison
    normalisation is a rule-level concern, because it must be applied to both
    sides of a comparison, not baked into the stored text.
    """
    lines = (_WHITESPACE_RE.sub(" ", line).strip() for line in raw.splitlines())
    return "\n".join(line for line in lines if line)
