"""Country adapters, conservative normalization, deduplication and SQLite graph."""
from __future__ import annotations

import csv
import hashlib
import json
import posixpath
import re
import sqlite3
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .schema import ValidationError, clean, key, validate_program

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
ALIASES = {
    "university / centre": "university", "university / school": "university",
    "location": "city", "programme / route": "program_name", "programme": "program_name",
    "years": "duration", "teaching language": "teaching_language_notes",
    "tuition eur": "tuition_fee", "extra charges eur": "extra_charges",
    "first-year total eur": "first_year_academic_total", "currency": "tuition_currency",
    "fee year / scope": "fee_year", "fee / programme notes": "fee_notes",
    "admission information": "admission_requirements", "focus / specialisation": "fields_of_study",
    "published fee": "tuition_fee", "fee basis": "fee_scope",
    "fee / programme year": "fee_year", "notes": "overview",
}
LANGUAGES = ("English", "Spanish", "Catalan", "Portuguese", "Greek", "Valencian", "French", "German", "Italian", "Dutch", "Afrikaans")


def read_xlsx(path):
    """Read values + hyperlink targets without executing formulas or macros."""
    from .workbook import is_relational, program_records
    if is_relational(path):
        yield from program_records(path)
        return
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            strings = ["".join(x.itertext()) for x in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)]
        rels = {r.attrib["Id"]: r.attrib["Target"] for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
            rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rels[rid]
            name = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            root = ET.fromstring(z.read(name))
            hyperlink_rels = {}
            relfile = posixpath.dirname(name) + "/_rels/" + posixpath.basename(name) + ".rels"
            if relfile in z.namelist():
                hyperlink_rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read(relfile))}
            links = {h.get("ref"): hyperlink_rels.get(h.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")) for h in root.findall("m:hyperlinks/m:hyperlink", NS)}
            headers = None
            for row in root.findall("m:sheetData/m:row", NS):
                cells = {}
                row_links = []
                for c in row.findall("m:c", NS):
                    ref = c.get("r")
                    col = re.sub(r"\d", "", ref)
                    v = c.find("m:v", NS)
                    value = v.text if v is not None else "".join(c.find("m:is", NS).itertext()) if c.find("m:is", NS) is not None else None
                    if c.get("t") == "s" and value is not None:
                        value = strings[int(value)]
                    # Keep the original cell text, including "Not verified".
                    # Canonical fields are normalized later; raw evidence is lossless.
                    cells[col] = value
                    if links.get(ref):
                        row_links.append(links[ref])
                if any(key(v) == "country" for v in cells.values() if v):
                    headers = {col: key(v) for col, v in cells.items() if v}
                    continue
                if not headers or not any(clean(v) is not None for v in cells.values()):
                    continue
                record = {field: cells.get(col) for col, field in headers.items()}
                record["_hyperlinks"] = row_links
                record["_excel_cells"] = {c.get("r"): cells[re.sub(r"\d", "", c.get("r"))] for c in row.findall("m:c", NS) if cells[re.sub(r"\d", "", c.get("r"))] is not None}
                yield record, {"file": str(path), "sheet": sheet.get("name"), "row": int(row.get("r"))}


def read_records(path):
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        yield from read_xlsx(path)
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
        records = data.get("programs") if isinstance(data, dict) else data
        if not isinstance(records, list):
            raise ValidationError(f"{path}: expected a JSON array or {{programs: [...]}}")
        for i, record in enumerate(records, 1):
            yield record, {"file": str(path), "row": i}
    elif path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            for i, record in enumerate(csv.DictReader(f), 2):
                yield record, {"file": str(path), "row": i}
    else:
        raise ValidationError(f"Unsupported input: {path} (use JSON, CSV or XLSX)")


def normalize(raw, provenance, mapping=None):
    if not isinstance(raw, dict):
        raise ValidationError("Program row must be an object")
    from .schema import PROGRAM_FIELDS, LIST_FIELDS, NUMBER_FIELDS, BOOL_FIELDS
    mapped = {}
    for name, value in raw.items():
        target = (mapping or {}).get(name, ALIASES.get(key(name), key(name).replace(" ", "_")))
        if target in PROGRAM_FIELDS:
            mapped[target] = clean(value)
        elif name not in ("_hyperlinks", "_excel_cells"):
            raise ValidationError(f"Unmapped column {name!r}; supply a mapping JSON")
    object_fields = ("raw_record", "related_records", "field_sources")
    for f in LIST_FIELDS + ["required_qualifications", "quality_warnings", "provenance", "raw_record", "related_records", "source_records", "field_sources"]:
        v = mapped.get(f)
        if isinstance(v, str):
            if v[:1] in ("[", "{"):
                mapped[f] = json.loads(v)
            elif f not in ("provenance", "source_records") + object_fields:
                mapped[f] = [x.strip() for x in v.split(";") if x.strip()]
    for f in NUMBER_FIELDS:
        v = mapped.get(f)
        if isinstance(v, str):
            # Reject ranges, comma ambiguity and text; preserve them in raw_record.
            if re.fullmatch(r"\d+(?:\.\d+)?", v):
                mapped[f] = round(float(v), 6)
            else:
                mapped[f] = None
    for f in BOOL_FIELDS:
        if isinstance(mapped.get(f), str):
            if mapped[f].casefold() not in ("true", "false"):
                raise ValidationError(f"{f} must be true or false")
            mapped[f] = mapped[f].casefold() == "true"
    notes = mapped.get("teaching_language_notes")
    if notes and not mapped.get("teaching_language"):
        mapped["teaching_language"] = [lang for lang in LANGUAGES if re.search(r"\b" + lang + r"\b", notes, re.I)] or None
        # Only a plain single-language statement supports a full-language track.
        if key(notes) in {key(x) for x in LANGUAGES}:
            mapped["full_program_languages"] = [notes.title()]
    scope = mapped.get("fee_scope") or ""
    if mapped.get("fee_period") is None:
        if "monthly" in scope.casefold():
            mapped["fee_period"] = "monthly"
        elif "annual" in scope.casefold():
            mapped["fee_period"] = "annual"
    # A source hyperlink is retained, but does not imply that facts were verified today.
    links = [x for x in raw.get("_hyperlinks", []) if x.startswith(("https://", "http://"))]
    if links and not mapped.get("source_url"):
        mapped["source_url"] = links[0]
    identity = [mapped.get(f) for f in ("country", "university", "program_name", "fields_of_study", "city", "degree_level", "full_program_languages")]
    if not mapped.get("program_id"):
        mapped["program_id"] = "UNI-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).casefold().encode()).hexdigest()[:16]
    mapped["provenance"] = (mapped.get("provenance") or []) + [provenance]
    mapped["raw_record"] = mapped.get("raw_record") or raw
    warnings = mapped.get("quality_warnings") or []
    for f in ("degree_level", "source_url", "last_verified_date", "estimated_living_cost", "full_program_languages"):
        if mapped.get(f) is None:
            warnings.append(f"Missing {f}; do not infer it")
    if mapped.get("fee_period") == "monthly":
        warnings.append("Monthly tuition: annual instalment count is unknown; no annual conversion")
    mapped["quality_warnings"] = sorted(set(warnings))
    return validate_program(mapped)


def identity(p):
    return tuple(key(json.dumps(p.get(f), sort_keys=True, ensure_ascii=False)) for f in ("country", "university", "program_name", "fields_of_study", "city", "degree_level", "full_program_languages"))


def consolidate(paths, mapping=None):
    programs, by_identity, rejected = {}, {}, []
    for path in paths:
        for raw, provenance in read_records(path):
            try:
                p = normalize(raw, provenance, mapping)
                pid = p["program_id"]
                existing_id = by_identity.get(identity(p))
                if existing_id and existing_id != pid:
                    raise ValidationError(f"Duplicate identity with differing IDs: {existing_id}, {pid}; reconcile explicitly")
                if pid in programs:
                    old = programs[pid]
                    conflicts = [f for f in p if f not in ("provenance", "raw_record", "quality_warnings") and old[f] is not None and p[f] is not None and old[f] != p[f]]
                    if conflicts:
                        raise ValidationError(f"Conflicting duplicate {pid}: {conflicts}; reconcile explicitly")
                    for f in p:
                        if old[f] is None:
                            old[f] = p[f]
                    old["provenance"] += p["provenance"]
                    old["quality_warnings"] = sorted(set(old["quality_warnings"] + p["quality_warnings"]))
                else:
                    programs[pid] = p
                    by_identity[identity(p)] = pid
            except (ValidationError, ValueError, TypeError) as exc:
                rejected.append({"provenance": provenance, "error": str(exc)})
    return list(programs.values()), rejected


def audit_workbooks(paths, programs):
    """Independently archive every populated cell in every sheet and check row coverage."""
    reports = []
    for path in paths:
        path = Path(path)
        if path.suffix.lower() != ".xlsx":
            continue
        from .workbook import is_relational, read_tables
        relational_tables = read_tables(path) if is_relational(path) else None
        expected = {}
        for _, provenance in read_xlsx(path):
            expected.setdefault(provenance["sheet"], set()).add(provenance["row"])
        imported = {}
        for p in programs:
            for source in p["provenance"] or []:
                if source.get("file") == str(path):
                    imported.setdefault(source.get("sheet"), set()).add(source.get("row"))
        with zipfile.ZipFile(path) as z:
            strings = ["".join(x.itertext()) for x in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)] if "xl/sharedStrings.xml" in z.namelist() else []
            rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
            sheets = []
            for sheet in ET.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
                rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = rels[rid]
                name = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
                root = ET.fromstring(z.read(name))
                cells = {}
                for c in root.findall("m:sheetData/m:row/m:c", NS):
                    v = c.find("m:v", NS)
                    value = v.text if v is not None else "".join(c.find("m:is", NS).itertext()) if c.find("m:is", NS) is not None else None
                    if c.get("t") == "s" and value is not None:
                        value = strings[int(value)]
                    formula = c.find("m:f", NS)
                    if value is not None or formula is not None:
                        cells[c.get("r")] = {"value": value}
                        if formula is not None:
                            cells[c.get("r")]["formula"] = formula.text
                title = sheet.get("name")
                expected_rows = expected.get(title, set())
                imported_rows = imported.get(title, set())
                preserved = {ref for p in programs for source in p["provenance"] or [] if source.get("file") == str(path) and source.get("sheet") == title for ref in (p["raw_record"] or {}).get("_excel_cells", {})}
                unheaded = [ref for ref, cell in cells.items() if cell["value"] and int(re.sub(r"\D", "", ref)) in expected_rows and ref not in preserved]
                sheets.append({"sheet": title, "visibility": sheet.get("state", "visible"), "populated_cell_count": len(cells), "program_row_count": len(expected_rows), "imported_row_count": len(imported_rows), "archived_table_row_count": len(relational_tables[title]) if relational_tables is not None else None, "unimported_rows": sorted(expected_rows - imported_rows), "cells": cells, "unpreserved_program_cells": unheaded})
            reports.append({"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "sheet_count": len(sheets), "sheets": sheets})
    return {"workbooks": reports, "countries": sorted({p["country"] for p in programs}), "program_count": len(programs), "all_program_rows_imported": all(not s["unimported_rows"] and not s["unpreserved_program_cells"] for report in reports for s in report["sheets"])}


GRAPH_FIELDS = {"university": "offered_by", "country": "located_in", "city": "in_city", "fields_of_study": "studies", "teaching_language": "mentions_language", "full_program_languages": "taught_in", "campus_type": "has_campus", "location_type": "has_location"}


def save_dataset(programs, output, workbook_rows=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "programs.json").write_text(json.dumps(programs, ensure_ascii=False, indent=2) + "\n")
    (output / "workbook_tables.json").write_text(json.dumps(workbook_rows or [], ensure_ascii=False, indent=2) + "\n")
    from .schema import PROGRAM_FIELDS
    with (output / "programs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PROGRAM_FIELDS)
        writer.writeheader()
        for p in programs:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else v for k, v in p.items()})
    with sqlite3.connect(output / "programs.sqlite") as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS programs(program_id TEXT PRIMARY KEY, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS nodes(node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS edges(source TEXT NOT NULL, relation TEXT NOT NULL, target TEXT NOT NULL,
                evidence_field TEXT NOT NULL, PRIMARY KEY(source, relation, target));
            CREATE INDEX IF NOT EXISTS edge_target ON edges(target, relation);
            CREATE TABLE IF NOT EXISTS workbook_rows(table_name TEXT NOT NULL, row_id TEXT NOT NULL, record TEXT NOT NULL,
                provenance TEXT NOT NULL, PRIMARY KEY(table_name, row_id));
        """)
        db.execute("DELETE FROM edges")
        db.execute("DELETE FROM nodes")
        db.execute("DELETE FROM programs")
        db.execute("DELETE FROM workbook_rows")
        for entry in workbook_rows or []:
            row = entry["record"]
            table = entry["table"]
            row_id = str(next(v for k, v in row.items() if not k.startswith("_") and v is not None))
            db.execute("INSERT INTO workbook_rows VALUES (?,?,?,?)", (table, row_id, json.dumps(row, ensure_ascii=False), json.dumps(entry["provenance"])))
            node_id = table + ":" + row_id
            db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?)", (node_id, table, row.get("name_en") or row.get("title") or row.get("name") or row_id))
        for p in programs:
            db.execute("INSERT INTO programs VALUES (?,?)", (p["program_id"], json.dumps(p, ensure_ascii=False)))
            db.execute("INSERT INTO nodes VALUES (?,?,?)", (p["program_id"], "program", p["program_name"]))
            for field, relation in GRAPH_FIELDS.items():
                values = p[field] if isinstance(p[field], list) else [p[field]]
                for value in values:
                    if value is None:
                        continue
                    nid = field + ":" + key(value)
                    db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?)", (nid, field, value))
                    db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", (p["program_id"], relation, nid, field))
            for table, rows in (p.get("related_records") or {}).items():
                for row in rows:
                    row_id = str(next(v for k, v in row.items() if not k.startswith("_") and v is not None))
                    nid = table + ":" + row_id
                    db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?)", (nid, table, row.get("name_en") or row.get("name") or row_id))
                    db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", (p["program_id"], "has_" + table.lower(), nid, "related_records." + table))
            for source in p.get("source_records") or []:
                nid = "Sources:" + source["source_id"]
                db.execute("INSERT OR IGNORE INTO nodes VALUES (?,?,?)", (nid, "Sources", source.get("title") or source["source_id"]))
                db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", (p["program_id"], "supported_by", nid, "source_records"))
