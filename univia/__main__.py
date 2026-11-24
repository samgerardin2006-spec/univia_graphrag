from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_env
from .generate import recommend
from .ingest import audit_workbooks, consolidate, save_dataset
from .retrieve import retrieve
from .schema import ValidationError


def main():
    parser = argparse.ArgumentParser(description="UNIVIA local GraphRAG")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="Map, validate, deduplicate, and build SQLite graph")
    ingest.add_argument("files", nargs="+")
    ingest.add_argument("--output", default="data")
    ingest.add_argument("--mapping", help="JSON object mapping original column names to canonical fields")
    for name in ("retrieve", "recommend"):
        p = sub.add_parser(name)
        p.add_argument("profile", help="Student profile JSON file")
        p.add_argument("--database", default="data/programs.sqlite")
        p.add_argument("--top-k", type=int, default=8)
        p.add_argument("--output", help="Write JSON result to this file")
        if name == "recommend":
            p.add_argument("--offline", action="store_true", help="Explicit deterministic preview; no API call")
            p.add_argument("--model")
    serve = sub.add_parser("serve", help="Launch local browser UI and JSON endpoints")
    serve.add_argument("--database", default="data/programs.sqlite")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        load_env()
        if args.command == "ingest":
            mapping = json.loads(Path(args.mapping).read_text()) if args.mapping else None
            programs, rejected = consolidate(args.files, mapping)
            output = Path(args.output)
            output.mkdir(parents=True, exist_ok=True)
            report = {"program_count": len(programs), "rejected_count": len(rejected), "rejected": rejected,
                      "missing_fields": {f: sum(p[f] is None for p in programs) for f in ("degree_level", "source_url", "last_verified_date", "estimated_living_cost", "full_program_languages")}}
            audit = audit_workbooks(args.files, programs)
            (output / "workbook_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
            report["sheet_coverage"] = [{k: s[k] for k in ("sheet", "program_row_count", "imported_row_count", "archived_table_row_count", "unimported_rows")} for book in audit["workbooks"] for s in book["sheets"]]
            report["countries"] = audit["countries"]
            report["all_program_rows_imported"] = audit["all_program_rows_imported"]
            (output / "ingestion_report.json").write_text(json.dumps(report, indent=2) + "\n")
            # Do not publish a silently partial dataset after validation conflicts.
            if rejected:
                raise ValidationError(f"{len(rejected)} invalid/conflicting rows. Dataset was not replaced. See {output / 'ingestion_report.json'}")
            from .workbook import archive_rows
            rows = archive_rows(args.files)
            report["archived_table_rows"] = len(rows)
            report["country_records"] = sorted(r["record"]["name_en"] for r in rows if r["table"] == "Countries")
            (output / "ingestion_report.json").write_text(json.dumps(report, indent=2) + "\n")
            save_dataset(programs, output, workbook_rows=rows)
            print(json.dumps(report, indent=2))
        elif args.command == "serve":
            from .server import serve
            serve(args.database, args.port)
        else:
            profile = json.loads(Path(args.profile).read_text())
            result = retrieve(args.database, profile, args.top_k)
            if args.command == "recommend":
                result = recommend(result, args.offline, args.model)
            rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            if args.output:
                Path(args.output).write_text(rendered)
            else:
                print(rendered, end="")
    except (ValidationError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
