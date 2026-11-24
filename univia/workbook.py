"""Import the relational UNIVIA workbook. Cell contents are data, never instructions."""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .schema import ValidationError, clean

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
SENTINELS = {"NOT_PUBLISHED", "TO_VERIFY", "NOT_VERIFIED", "NOT_APPLICABLE"}


def value(v):
    return None if isinstance(v, str) and v.strip().upper() in SENTINELS else clean(v)


def is_relational(path):
    with zipfile.ZipFile(path) as z:
        names = {s.get("name") for s in ET.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS)}
    return {"Countries", "Institutions", "Programmes"} <= names


def read_tables(path):
    """Preserve all rows, columns, original cells, formulas and source locations."""
    tables = {}
    with zipfile.ZipFile(path) as z:
        strings = ["".join(x.itertext()) for x in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)] if "xl/sharedStrings.xml" in z.namelist() else []
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
            target = rels[sheet.get(RID)]
            name = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            headers, records = None, []
            for row in ET.fromstring(z.read(name)).findall("m:sheetData/m:row", NS):
                cells, formulas = {}, {}
                for c in row.findall("m:c", NS):
                    v = c.find("m:v", NS)
                    text = v.text if v is not None else "".join(c.find("m:is", NS).itertext()) if c.find("m:is", NS) is not None else None
                    if c.get("t") == "s" and text is not None:
                        text = strings[int(text)]
                    if text is not None:
                        cells[c.get("r")] = text
                    formula = c.find("m:f", NS)
                    if formula is not None:
                        formulas[c.get("r")] = formula.text
                if not cells and not formulas:
                    continue
                if headers is None:
                    headers = {re.sub(r"\d", "", ref): text for ref, text in cells.items() if text}
                    continue
                by_col = {re.sub(r"\d", "", ref): text for ref, text in cells.items()}
                record = {field: by_col.get(col) for col, field in headers.items()}
                record["_excel_cells"] = cells
                if formulas:
                    record["_excel_formulas"] = formulas
                record["_provenance"] = {"file": str(path), "sheet": sheet.get("name"), "row": int(row.get("r"))}
                records.append(record)
            tables[sheet.get("name")] = records
    return tables


def source_ids(record):
    refs = set()
    for field, text in record.items():
        if field.endswith("__src") or field in ("source_ids", "source_id"):
            refs.update(re.findall(r"S-[A-Za-z0-9-]+", text or ""))
    return refs


def program_records(path):
    tables = read_tables(path)

    def index(table, field):
        rows = tables.get(table, [])
        result = {row[field]: row for row in rows}
        if len(result) != len(rows):
            raise ValidationError(f"Duplicate {field} in {table}")
        return result

    countries = index("Countries", "country_id")
    institutions = index("Institutions", "inst_id")
    cities = index("Cities", "city_id")
    regions = index("Regions", "region_id")
    professions = index("Professions", "profession_id")
    domains = index("Domains", "domain_id")
    sources = index("Sources", "source_id")
    for raw in tables["Programmes"]:
        pid = raw["prog_id"]
        if raw["inst_id"] not in institutions:
            raise ValidationError(f"{pid}: missing institution {raw['inst_id']}")
        inst = institutions[raw["inst_id"]]
        if inst["country_id"] not in countries:
            raise ValidationError(f"{pid}: missing country {inst['country_id']}")
        country = countries[inst["country_id"]]
        city = cities.get(inst["city_id"])
        region = regions.get(inst["region_id"])
        profession = professions.get(raw["profession_id"])
        related = {"Programmes": [raw], "Countries": [country], "Institutions": [inst]}
        for table, record in (("Cities", city), ("Regions", region), ("Professions", profession), ("Domains", domains.get(raw["domain_id"]))):
            if record:
                related[table] = [record]
        target_ids = {pid, inst["inst_id"], country["country_id"]}
        for record in (city, region):
            if record:
                target_ids.add(record.get("city_id") or record.get("region_id"))
        for table in ("Admission_Requirements", "Key_Dates", "Stats", "Scholarships", "Testimonies", "Fact_Checks"):
            rows = [r for r in tables.get(table, []) if r.get("target_id") in target_ids]
            if rows:
                related[table] = rows
        for table in ("Application_Routes", "Datasets", "Pathways"):
            rows = [r for r in tables.get(table, []) if (table == "Application_Routes" and r.get("inst_id") == inst["inst_id"]) or (table == "Datasets" and r.get("country_id") == country["country_id"]) or (table == "Pathways" and r.get("country_id") == country["country_id"] and r.get("profession_id") == raw["profession_id"])]
            if rows:
                related[table] = rows
        issues = [r for r in tables.get("Questions_Issues", []) if r.get("row_id") in target_ids or r.get("row_id") == "ALL"]
        if issues:
            related["Questions_Issues"] = issues
        refs = source_ids(raw)
        for rows in related.values():
            for row in rows:
                refs |= source_ids(row)
        absent = refs - sources.keys()
        if absent:
            raise ValidationError(f"{pid}: unresolved source references: {sorted(absent)}")
        source_records = [sources[sid] for sid in sorted(refs)]
        field_sources = {}
        for field, ref in raw.items():
            if field.endswith("__src") and value(ref):
                base = field[:-5]
                field_sources[base] = [{"source_id": sid, "url": sources[sid].get("url"), "quote": raw.get(base + "__quote")} for sid in sorted(source_ids({field: ref}))]
        fee_sources = field_sources.get("tuition_eu", [])
        primary = fee_sources or field_sources.get("duration_years", []) or field_sources.get("admission_summary", [])
        period = {"year": "annual", "month": "monthly", "ECTS": "ects", "first-year total": "first_year"}.get(value(raw["tuition_unit"]))
        tuition = value(raw["tuition_eu"])
        notes = value(raw["teaching_language"])
        study_fields = [v for v in (value(raw["specialisation"]), profession.get("name_en") if profession else None, domains.get(raw["domain_id"], {}).get("name_en")) if v]
        admission = [v for v in (value(raw["entry_after_bac"]), value(raw["admission_summary"])) if v]
        admission += [v for r in related.get("Admission_Requirements", []) for v in [value(r.get("rule_text")) or value(r.get("display_fr"))] if v]
        if value(raw["entrance_exam"]):
            admission.append("Entrance exam: " + raw["entrance_exam"])
        p = dict(program_id=pid, university=inst["name_en"], program_name=raw["name_en"], country=country["name_en"], city=(city or {}).get("name_en") or value(raw["campus"]), duration=value(raw["duration_years"]), teaching_language_notes=notes,
                 fields_of_study=study_fields or None, tuition_fee=tuition if period != "first_year" else None, tuition_currency=value(raw["tuition_currency"]), fee_period=period if period != "first_year" else None,
                 first_year_academic_total=tuition if period == "first_year" else None, extra_charges=value(raw["extra_fees_eu"]), fee_year=value(raw["fee_cycle"]), fee_notes=value(raw["fee_notes"]),
                 fee_scope="EU applicant tuition" if period != "first_year" else "EU first-year academic total", source_url=primary[0]["url"] if primary else None,
                 program_url=next((s["url"] for s in field_sources.get("duration_years", []) if s.get("url")), None), last_verified_date=value(raw["last_checked_on"]),
                 overview="\n".join(v for v in (value(raw["tagline_fr"]), value(raw["summary_fr"])) if v) or None, admission_requirements=list(dict.fromkeys(admission)) or None,
                 scholarships=[r["name"] + (": " + r["eligibility"] if value(r.get("eligibility")) else "") for r in related.get("Scholarships", [])] or None,
                 related_records=related, source_records=source_records, field_sources=field_sources, raw_record=raw)
        yield p, raw["_provenance"]


def archive_rows(paths):
    result = []
    for path in paths:
        if Path(path).suffix.lower() == ".xlsx" and is_relational(path):
            for table, rows in read_tables(path).items():
                for row in rows:
                    result.append({"table": table, "record": row, "provenance": row["_provenance"]})
    return result
