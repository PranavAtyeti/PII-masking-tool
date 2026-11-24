"""Bounded, non-executing DOCX extraction into ordered document sections."""

from __future__ import annotations

import hashlib
import io
import os
import re
import zipfile
from xml.etree import ElementTree

from fastapi import HTTPException

DOCX_MAX_ZIP_ENTRIES = int(os.getenv("PRIVY_MAX_DOCX_ZIP_ENTRIES", "5000"))
DOCX_MAX_UNCOMPRESSED_BYTES = int(os.getenv("PRIVY_MAX_DOCX_UNCOMPRESSED_BYTES", str(50 * 1024 * 1024)))
DOCX_MAX_ENTRY_BYTES = int(os.getenv("PRIVY_MAX_DOCX_ENTRY_BYTES", str(10 * 1024 * 1024)))
DOCX_MAX_COMPRESSION_RATIO = float(os.getenv("PRIVY_MAX_DOCX_COMPRESSION_RATIO", "200"))
DOCX_MAX_XML_BYTES = int(os.getenv("PRIVY_MAX_DOCX_XML_BYTES", str(10 * 1024 * 1024)))
DOCX_MAX_SECTIONS = int(os.getenv("PRIVY_MAX_DOCX_SECTIONS", "100000"))
DOCX_MAX_TEXT_CHARS = int(os.getenv("PRIVY_MAX_DOCX_TEXT_CHARS", "10000000"))

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS = {"w": _W_NS, "r": _R_NS}
_UNSAFE_XML_DECLARATION = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _bad_docx(detail: str = "The DOCX file is malformed or could not be safely parsed.") -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _validate_archive(raw_bytes: bytes) -> zipfile.ZipFile:
    archive = None
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_bytes))
        entries = archive.infolist()
        if not entries:
            archive.close()
            raise _bad_docx()
        if len(entries) > DOCX_MAX_ZIP_ENTRIES:
            archive.close()
            raise HTTPException(status_code=413, detail="The DOCX archive exceeds the entry limit.")

        seen: set[str] = set()
        uncompressed = 0
        names = set()
        for entry in entries:
            name = entry.filename.replace("\\", "/")
            parts = name.split("/")
            if (
                not name
                or name.startswith("/")
                or re.match(r"^[A-Za-z]:", name)
                or any(part in {".", ".."} for part in parts)
                or any(part == "" for part in parts[:-1])
            ):
                archive.close()
                raise _bad_docx("The DOCX archive contains a suspicious entry path.")
            if name.casefold() in seen:
                archive.close()
                raise _bad_docx("The DOCX archive contains duplicate entry names.")
            seen.add(name.casefold())
            names.add(name)
            if entry.flag_bits & 0x1:
                archive.close()
                raise _bad_docx("Encrypted DOCX archive entries are not supported.")
            if entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                archive.close()
                raise _bad_docx("The DOCX archive uses an unsupported compression method.")
            if entry.file_size > DOCX_MAX_ENTRY_BYTES:
                archive.close()
                raise HTTPException(status_code=413, detail="A DOCX archive component exceeds the size limit.")
            uncompressed += entry.file_size
            if uncompressed > DOCX_MAX_UNCOMPRESSED_BYTES:
                archive.close()
                raise HTTPException(status_code=413, detail="The DOCX archive exceeds the decompressed size limit.")
            if entry.file_size and (
                entry.compress_size <= 0
                or entry.file_size / entry.compress_size > DOCX_MAX_COMPRESSION_RATIO
            ):
                archive.close()
                raise _bad_docx("The DOCX archive has a suspicious compression ratio.")

        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            archive.close()
            raise _bad_docx("The file is not a valid DOCX document.")
        actual_total = 0
        for entry in entries:
            actual_entry = 0
            with archive.open(entry) as source:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    actual_entry += len(chunk)
                    actual_total += len(chunk)
                    if actual_entry > DOCX_MAX_ENTRY_BYTES or actual_total > DOCX_MAX_UNCOMPRESSED_BYTES:
                        archive.close()
                        raise HTTPException(
                            status_code=413,
                            detail="The DOCX archive exceeds the decompressed size limit.",
                        )
            if actual_entry != entry.file_size:
                archive.close()
                raise _bad_docx("The DOCX archive failed its integrity check.")
        return archive
    except HTTPException:
        if archive is not None:
            archive.close()
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, ValueError) as exc:
        if archive is not None:
            archive.close()
        raise _bad_docx() from exc


def _read_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise _bad_docx() from exc
    if info.file_size > DOCX_MAX_XML_BYTES:
        raise HTTPException(status_code=413, detail="A DOCX XML component exceeds the size limit.")
    try:
        raw = archive.read(name)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise _bad_docx() from exc
    if (
        len(raw) > DOCX_MAX_XML_BYTES
        or _UNSAFE_XML_DECLARATION.search(raw.replace(b"\x00", b""))
    ):
        raise _bad_docx("The DOCX contains unsafe or oversized XML.")
    try:
        return ElementTree.fromstring(raw)
    except (ElementTree.ParseError, RecursionError) as exc:
        raise _bad_docx() from exc


def _text_from_paragraph(element: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{_W_NS}}}t":
            parts.append(node.text or "")
        elif node.tag == f"{{{_W_NS}}}tab":
            parts.append("\t")
        elif node.tag in {f"{{{_W_NS}}}br", f"{{{_W_NS}}}cr"}:
            parts.append("\n")
    return "".join(parts)


def _style_map(root: ElementTree.Element | None) -> dict[str, tuple[str, int | None]]:
    result: dict[str, tuple[str, int | None]] = {}
    if root is None:
        return result
    for style in root.findall(".//w:style", _NS):
        style_id = style.get(f"{{{_W_NS}}}styleId")
        name_node = style.find("w:name", _NS)
        name = name_node.get(f"{{{_W_NS}}}val", "") if name_node is not None else ""
        outline = style.find(".//w:outlineLvl", _NS)
        level = None
        if outline is not None:
            try:
                level = int(outline.get(f"{{{_W_NS}}}val", "0")) + 1
            except ValueError:
                level = None
        match = re.search(r"heading\s*(\d+)", name, re.IGNORECASE)
        if match:
            level = int(match.group(1))
        elif name.strip().casefold() == "title":
            level = 1
        if style_id:
            result[style_id] = (name, level)
    return result


def _paragraph_heading(
    paragraph: ElementTree.Element,
    styles: dict[str, tuple[str, int | None]],
) -> tuple[str | None, int | None]:
    style_node = paragraph.find("w:pPr/w:pStyle", _NS)
    style_id = style_node.get(f"{{{_W_NS}}}val", "") if style_node is not None else ""
    name, level = styles.get(style_id, (style_id, None))
    outline = paragraph.find("w:pPr/w:outlineLvl", _NS)
    if outline is not None:
        try:
            level = int(outline.get(f"{{{_W_NS}}}val", "0")) + 1
        except ValueError:
            pass
    return name or None, level


def _paragraphs_in_cell(cell: ElementTree.Element) -> list[str]:
    texts = []
    for paragraph in cell.findall(".//w:p", _NS):
        text = _text_from_paragraph(paragraph).strip()
        if text:
            texts.append(text)
    return texts


def _table_rows(table: ElementTree.Element, table_index: int) -> list[dict]:
    rows_out: list[dict] = []
    raw_rows = table.findall("./w:tr", _NS)
    first_cells: list[str] = []
    if raw_rows:
        first_cells = [
            "\n".join(_paragraphs_in_cell(cell)).strip()
            for cell in raw_rows[0].findall("./w:tc", _NS)
        ]

    header_words = {
        "id", "name", "city", "amount", "status", "payment", "transaction",
        "identity", "customer", "support", "network", "email", "phone",
        "address", "date", "account", "case", "event", "description",
    }

    for row_index, row in enumerate(raw_rows, start=1):
        cells = []
        for cell_index, cell in enumerate(row.findall("./w:tc", _NS), start=1):
            cells.append({
                "source_id": f"table-{table_index + 1:04d}-row-{row_index:04d}-cell-{cell_index:03d}",
                "content": "\n".join(_paragraphs_in_cell(cell)),
            })
        if cells:
            explicit_header = row.find("w:trPr/w:tblHeader", _NS) is not None
            inferred_header = False
            if row_index == 1 and not explicit_header and first_cells:
                normalized = [
                    re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
                    for value in first_cells
                ]
                header_like = sum(
                    any(word in set(value.split()) for word in header_words)
                    for value in normalized
                )
                inferred_header = (
                    len(normalized) >= 2
                    and all(value and len(value) <= 64 for value in normalized)
                    and header_like >= max(1, len(normalized) // 2)
                )
            repeated_header = bool(first_cells) and [
                "\n".join(_paragraphs_in_cell(cell)).strip()
                for cell in row.findall("./w:tc", _NS)
            ] == first_cells
            is_header = explicit_header or inferred_header or (
                row_index > 1 and repeated_header
            )
            rows_out.append({
                "source_id": f"table-{table_index + 1:04d}-row-{row_index:04d}",
                "cells": cells,
                "is_header": is_header,
            })
    return rows_out


def _extract_story(
    root: ElementTree.Element,
    styles: dict[str, tuple[str, int | None]],
    sections: list[dict],
    heading_stack: list[tuple[int, str]],
    order: int,
    prefix: str = "",
) -> tuple[int, int, int]:
    paragraph_index = 0
    table_index = 0
    text_size = 0
    body = root.find("w:body", _NS)
    parent = body if body is not None else next(
        (node for node in root if node.tag in {f"{{{_W_NS}}}hdr", f"{{{_W_NS}}}ftr"}),
        root,
    )
    for child in list(parent):
        if child.tag == f"{{{_W_NS}}}p":
            paragraph_index += 1
            text = _text_from_paragraph(child)
            style, level = _paragraph_heading(child, styles)
            if level is not None:
                text = text.strip()
                if text:
                    heading_stack[:] = [(n, value) for n, value in heading_stack if n < level]
                    heading_stack.append((level, text))
            if text.strip():
                order += 1
                text_size += len(text)
                sections.append({
                    "source_id": f"{prefix}p-{paragraph_index:06d}",
                    "kind": "heading" if level is not None else "paragraph",
                    "order": order,
                    "heading_level": level,
                    "heading_style": style if level is not None else None,
                    "heading_path": [value for _, value in heading_stack],
                    "content": text,
                })
        elif child.tag == f"{{{_W_NS}}}tbl":
            table_index += 1
            rows = _table_rows(child, table_index - 1)
            for row in rows:
                order += 1
                content_length = sum(len(cell["content"]) for cell in row["cells"])
                text_size += content_length
                sections.append({
                    "source_id": f"{prefix}{row['source_id']}",
                    "kind": "table_row",
                    "order": order,
                    "table_id": f"{prefix}table-{table_index:04d}",
                    "heading_path": [value for _, value in heading_stack],
                    "cells": row["cells"],
                    "is_header": bool(row["is_header"]),
                })
    return order, text_size, paragraph_index + table_index


def parse_docx(raw_bytes: bytes, file_id: str) -> dict:
    """Parse DOCX content without extracting archive members or resolving links."""
    if not raw_bytes:
        raise _bad_docx("The uploaded DOCX file is empty.")
    archive = _validate_archive(raw_bytes)
    try:
        names = set(archive.namelist())
        styles = _style_map(_read_xml(archive, "word/styles.xml")) if "word/styles.xml" in names else {}
        document = _read_xml(archive, "word/document.xml")
        sections: list[dict] = []
        heading_stack: list[tuple[int, str]] = []
        order, text_size, _ = _extract_story(document, styles, sections, heading_stack, 0)

        warnings: list[str] = []
        header_footer_names = sorted(
            name for name in names
            if re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
        )
        for name in header_footer_names:
            story = _read_xml(archive, name)
            before = len(sections)
            prefix = "header-" if "/header" in name else "footer-"
            order, added_size, _ = _extract_story(
                story, styles, sections, heading_stack, order, prefix=prefix
            )
            text_size += added_size
            if len(sections) > before:
                warnings.append("headers_footers_included")
        if any(
            table.find(".//w:tbl", _NS) is not None
            for table in document.findall(".//w:tbl", _NS)
        ):
            warnings.append("nested_tables_flattened")

        external_relationships = False
        for name in names:
            if not name.endswith(".rels"):
                continue
            relationships = _read_xml(archive, name)
            if any(
                rel.get("TargetMode", "").casefold() == "external"
                for rel in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
            ):
                external_relationships = True
        if external_relationships:
            warnings.append("external_relationships_ignored")
        if any(name.startswith("word/media/") for name in names):
            warnings.append("embedded_images_not_extracted")
        if any(name.startswith("word/embeddings/") for name in names):
            warnings.append("embedded_objects_not_extracted")
        if not sections or not any(
            section.get("content", "").strip()
            or any(cell.get("content", "").strip() for cell in section.get("cells", []))
            for section in sections
        ):
            raise _bad_docx("The DOCX document contains no readable text.")
        if len(sections) > DOCX_MAX_SECTIONS or text_size > DOCX_MAX_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="The DOCX document exceeds the extraction limits.")

        core_properties = {}
        if "docProps/core.xml" in names:
            core = _read_xml(archive, "docProps/core.xml")
            core_namespaces = {
                "dc": "http://purl.org/dc/elements/1.1/",
                "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
                "dcterms": "http://purl.org/dc/terms/",
            }
            for key, path in {
                "title": ".//dc:title",
                "subject": ".//dc:subject",
                "creator": ".//dc:creator",
                "created": ".//dcterms:created",
                "modified": ".//dcterms:modified",
            }.items():
                element = core.find(path, core_namespaces)
                if element is not None and element.text:
                    core_properties[key] = element.text[:2048]
        return {
            "schema_version": 1,
            "document_kind": "docx",
            "file_id": file_id,
            "document_revision": hashlib.sha256(raw_bytes).hexdigest(),
            "metadata": core_properties,
            "sections": sections,
            "section_count": len(sections),
            "table_count": len({
                section.get("table_id")
                for section in sections
                if section.get("kind") == "table_row"
            }),
            "text_char_count": text_size,
            "extraction_status": "complete",
            "warnings": sorted(set(warnings)),
            "page_numbers_available": False,
        }
    except HTTPException:
        raise
    except (ElementTree.ParseError, KeyError, ValueError, TypeError, OSError, RuntimeError, RecursionError) as exc:
        raise _bad_docx() from exc
    finally:
        archive.close()
