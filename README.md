# UNIVIA · Local GraphRAG prototype

Python 3.9+; no third-party runtime dependencies. Run commands from this directory.

```sh
python3 -m univia ingest UNIVIA_INTL_DB_v4_SG_final.xlsx
python3 -m univia serve
```

Open http://127.0.0.1:8000 and ask a question, such as “I'm interested in economics and data. What could I study in Portugal?” No JSON input is needed. The page uses Mistral when a key is configured; otherwise it selects the clearly labeled database preview. You can switch modes with “Answer with”.

To use Mistral, put your own key in the local `.env` file:

```sh
MISTRAL_API_KEY=your-real-key
MISTRAL_MODEL=mistral-small-latest
```

Then run `python3 -m univia serve`, open http://127.0.0.1:8000 and select “Mistral AI”. The app loads `.env` from the current directory at startup; restart it after editing the file. Existing exported shell variables take priority. Values may be quoted; shell expansion and multiline values are not supported.

The key stays on the Python server. The student profile and up to 10 retrieved records are sent to Mistral. Use fictional profiles for this project.

Keep `.env.example` in the repository as the shared configuration template. If `.env` is missing in a fresh checkout, copy `.env.example` to `.env` and add your key there. `.gitignore` excludes `.env` and its local variants, private-key files, Python environments and generated datasets. Ignore rules do not remove files already tracked by Git: if a real key is committed, revoke it and remove it from the repository history before sharing.

## Pipeline

1. **Ingest:** load XLSX sheets, JSON arrays, or CSV files; map columns into one schema; validate; preserve original rows, URLs, and file/sheet/row provenance; merge duplicates; export JSON, CSV and SQLite. Conflicting duplicates or invalid rows produce an ingestion report and prevent replacement of the dataset.
2. **Retrieve:** exclude records that fail any hard constraint before scoring. Rank eligible records by keyword overlap in academic fields, subject graph connections, language preferences, country, campus and location fit. Retrieve 5–10 records (default 8), or all survivors when fewer exist.
3. **Generate:** send only the shortlisted records, evidence reasons, student profile and explicit constraints to Mistral's chat completions API using a JSON schema. Mistral ranks up to 5 results and chooses evidence-backed reason IDs.
4. **Validate:** reject the entire model response if it contains unknown/unretrieved/duplicate program IDs, unsupported fields or invalid reason IDs. Hydrate facts and explanations from database evidence. The structured profile API remains strict: missing evidence excludes records and zero eligible programs skips the API. The question UI displays the available program facts; internal match classifications and pending checks remain available in the expandable search details. It does not claim eligibility or affordability just because a program is shown. Known country, degree, language, budget and academic-rule violations remain excluded, and final validation checks these again.

The SQLite graph is real persisted data: `nodes` and `edges` link each program to universities, countries, cities, subjects, languages, campus types and location types. Subject scoring traverses `student interest term → matching subject node ← eligible program`; results carry those paths and the underlying source field. Graph expansion never brings an excluded program back into the candidate set. This is a small explicit knowledge graph with keyword retrieval; embeddings, inferred relationships and community-summary GraphRAG are not implemented in Phase 1.

Mistral controls ordering and evidence selection. Explanations use deterministic templates so arbitrary model prose cannot introduce unsupported program facts. This intentionally constrains the model's writing freedom. The client follows [Mistral's structured-output documentation](https://docs.mistral.ai/inference/conversations/structured-output/custom) and [chat API reference](https://docs.mistral.ai/api).

## Current workbook quality

The full workbook `UNIVIA_INTL_DB_v4_SG_final.xlsx` contains **22 sheets and 461 data rows**, all archived in JSON and SQLite. It imports **99 distinct programs**: Spain (38), Cyprus (4), Portugal (8), South Africa (37), and Romania (12). Its Countries table has eight country records, including Belgium, Italy and Hungary, which have country metadata but no program rows. Existing workbook program IDs are retained.

The relational importer joins Programmes to Institutions, Countries, Cities, Regions and Professions, plus applicable admissions rules, application routes, dates, scholarships, statistics, fact checks and recognition pathways. Source IDs resolve to all original source rows, URLs and quotes. All 136 source rows are archived. Empty sheets still have their headers preserved in the workbook audit; Prompts and other sheet contents are treated solely as data.

Amounts retain their units: 59 program rows use yearly fees, 21 use per-ECTS fees, three use monthly fees and two provide first-year academic totals. A per-ECTS amount is never mislabeled as annual tuition, and an academic total is never relabeled tuition-only. The remaining 14 rows have no tuition unit. Country-level living-cost ranges remain country-level text rather than invented program-specific amounts. Historical admissions statistics are not converted into guaranteed admission thresholds. Grade alternatives and rule groups retain their original structure in linked records.

All 99 records have a check date, 98 have a resolved primary source URL and 39 have a simple single-language statement that supports a full-program language field. The workbook does not explicitly supply program degree levels. A mention of English within a multilingual description does not establish an English-only track.

`examples/student_explore.json` demonstrates subject ranking without asserting affordability or admission eligibility. Its eligible-country filter leaves 45 records. `examples/student_strict.json` correctly returns no matches against this workbook. To support strict recommendations, enrich records from original sources with explicit degree level, full-language-track evidence, verified applicant-applicable costs, living costs and structured admissions rules. The app does not invent those facts or search for them during ingestion.

Missing values remain `null` in JSON/SQLite and blank in CSV. Arrays and objects are JSON-encoded inside CSV cells. All currency codes are uppercased; numeric values remain in their stated currency and period. No exchange rates, monthly instalment counts, annual conversions or grade-scale equivalences are guessed.

## Commands and input formats

```sh
# Combine files under the common schema
python3 -m univia ingest country_a.json country_b.csv country_c.xlsx --output data

# Supply exact column-name aliases for a new country file
python3 -m univia ingest country.csv --mapping examples/column_mapping.json

# Inspect candidate scores, graph paths and exclusion reasons
python3 -m univia retrieve examples/student_explore.json --output data/retrieval.json

# Explicit offline preview
python3 -m univia recommend examples/student_explore.json --offline

# Live Mistral request; requires MISTRAL_API_KEY
python3 -m univia recommend examples/student_explore.json --output data/results.json

# Expected empty result for the supplied incomplete workbook
python3 -m univia recommend examples/student_strict.json --offline

# Verification suite, including mocked API responses
python3 -m unittest discover -s tests -v
```

JSON files accept an array of canonical records or `{"programs": [...]}`. CSV headers can use the canonical schema or a mapping JSON object. XLSX files with Countries, Institutions and Programmes sheets use the relational importer. Legacy flat XLSX files use their `Country` header row. Neither importer executes spreadsheet formulas or document instructions. Unrecognized flat-file columns are rejected until mapped. Avoid pointing the importer at its own output and original inputs in the same run.

`data/ingestion_report.json` lists rejected records and missing-field counts. Successful runs write `data/programs.json`, `data/programs.csv` and `data/programs.sqlite`. Reingestion rebuilds the graph. Explicit IDs are retained; otherwise a deterministic ID uses country, university, program, specialization, city, degree level and full-language track. Keep explicit IDs in curated data once IDs are referenced externally. Conflicting IDs for the same identity require manual reconciliation.

Excel ingestion also writes `data/workbook_audit.json`: a SHA-256 fingerprint, complete sheet list (including hidden sheets), every populated cell with its original value and cached formula, and per-sheet program-row coverage. `raw_record._excel_cells` preserves the original program cells. For relational workbooks, `data/workbook_tables.json` and SQLite's `workbook_rows` table preserve every data row from every sheet, including administrative issues and rows without program links. `related_records` attaches the applicable rows to each program; `source_records` and `field_sources` preserve source identities, URLs and quotes. The graph links programs to the related table rows and their evidence sources. Country-only metadata remains available even without program rows. Recognizing a country name does not invent programs there.

## Program schema

See `univia/schema.py` for executable validation and `examples/program_template.json` for the complete canonical shape. Only `program_id`, `university`, `program_name`, and `country` are required after normalization. Other facts are nullable.

| Group | Fields |
| --- | --- |
| Identity | `program_id`, `university`, `program_name`, `degree_level`, `country`, `city`, `program_url`, `source_url`, `last_verified_date` |
| Academic | `fields_of_study`, `teaching_language`, `full_program_languages`, `teaching_language_notes`, `admission_requirements`, `academic_prerequisites`, `required_qualifications`, `minimum_grades`, `minimum_grade_scale` |
| Cost | `tuition_fee`, `tuition_currency`, `fee_period`, `extra_charges`, `first_year_academic_total`, `annual_additional_costs`, `estimated_living_cost`, `living_cost_currency`, `living_cost_period`, `living_cost_source_url`, `fee_scope`, `fee_year`, `fee_notes`, `costs_verified` |
| Fit | `duration` (years), `campus_type`, `location_type`, `scholarships`, `overview`, `key_subjects`, `distinctive_features`, `career_paths` |
| Audit | `requirements_verified`, `provenance`, `raw_record`, `quality_warnings` |
| Relational evidence | `related_records`, `source_records`, `field_sources` |

`degree_level`: `bachelor`, `master`, `doctorate`, `integrated_master`, `associate`, or `diploma`. Periods: `annual`, `monthly`, `semester`, `total`, or `ects` (per ECTS tuition). First-year academic totals have their own field. Amounts must be finite and nonnegative. Dates must be ISO dates and cannot be in the future.

`teaching_language` records languages mentioned; `full_program_languages` records verified language tracks covering the whole program. Language proficiency, bilingual requirements and language tests belong in admission requirements and need separate review.

`costs_verified: true` is a curator assertion that the annual cost components are complete and applicable to this fictional applicant and fee category. `annual_additional_costs` must be explicitly supplied (including zero when verified); it includes all mandatory non-tuition academic charges without double-counting `extra_charges`. The prototype has no nationality/fee-category rule engine: curate a dataset for the intended applicant audience, and do not mark mixed EU/non-EU fee categories verified. A verification date establishes recency, not fee-year applicability; the curator must check the relevant intake.

## Student profile and constraint semantics

The examples show the student schema. `student_id` is required; use a fictional identifier. Unknown profile fields and unknown hard constraints are rejected rather than silently ignored. Optional lists default to empty lists. Free-text academic summaries, expectations and flexible preferences are sent to Mistral for ranking context; they are not executable eligibility rules.

| Input | Enforcement |
| --- | --- |
| `desired_degree_level` | Hard exact degree match; missing program degree excludes |
| `excluded_countries` | Hard exclusion |
| `annual_total_budget` + `currency` | Hard annual total-cost ceiling; incomplete/unverified/mismatched-currency costs exclude |
| `annual_tuition_budget` + `currency` | Hard annual tuition ceiling, without requiring living-cost data; annual period, source and verification evidence required in strict mode |
| `preferred_countries`, `preferred_languages`, `campus_preferences`, `location_preferences` | Soft scoring preferences; use hard constraints to make these mandatory |
| `hard_constraints.eligible_countries` | Allowed country list |
| `hard_constraints.teaching_languages` | At least one confirmed full-program track in an allowed language |
| `hard_constraints.campus_types`, `location_types` | Allowed values; unknown facts exclude |
| `hard_constraints.required_fields` | Every listed canonical field must be present |
| `hard_constraints.max_verification_age_days` | Source URL and verification date required; age cannot exceed this limit |
| `hard_constraints.require_verified_requirements` | Require verified, complete, structured grade and qualification rules; unhandled narrative admissions rules exclude for human review |

Known structured minimum grades and qualification rules are enforced even without strict admissions mode. Grade scales must match exactly. Strict mode requires `minimum_grades`, `minimum_grade_scale`, `required_qualifications`, verification metadata and `requirements_verified: true`. Narrative admission requirements and academic prerequisites are excluded in strict mode because the prototype cannot reliably certify their satisfaction. Without strict admissions mode, results prominently state that admission eligibility is not certified.

Budget and strict-admissions checks default to a maximum verification age of 365 days unless explicitly overridden. When no budget is supplied, results state that affordability was not checked. Empty hard-constraint lists are rejected; omit a constraint to leave it unrestricted.

## Local API and boundaries

The browser uses `POST /api/ask` with `{"question": "Economics in Portugal", "offline": false}`. It displays a readable answer, a shortlist, interpreted filters and expandable record evidence. `GET /api/status` reports whether the server loaded a key, the model name and server version; it never returns the key. A loaded key is not evidence of a successful API call.

Question parsing is a deterministic English adapter, not a general natural-language constraint solver. It recognizes named countries and explicit region lists in `univia/questions.py`, degree levels, teaching languages, campus types, annual total budgets and tuition-only budgets. The interpreted filters appear in the answer. Amounts use a decimal point, optional thousands separators and optional `k`. A missing currency remains unspecified: `10k for tuition only` means a tuition budget of 10,000 with an unresolved currency, not a request for living costs. Unsupported or ambiguous conditions remain pending checks in the search details rather than blocking the entire answer. Cards omit empty fields, verification banners and missing-information boilerplate; original source statements are retained in raw records and the audit. No currency conversion, monthly annualization or missing program fact is invented.

For example, a question about medicine in Eastern Europe retains the region filter and lists the workbook's actual coverage when no programs match. It does not recommend Spanish or Cypriot programs as Eastern European matches. Region labels have differing definitions; the exact country list used by the adapter is displayed in the interpreted filters.

“Cheapest” and “lowest tuition” are sort requests, not demands for the student to supply a numeric budget. Retrieval considers all subject-matching records before truncation and selects the lowest listed tuition amount within each currency/fee-period group. Monthly and annual fees remain separate; zero tuition is retained. If the source does not give a period, the app displays the amount without inventing one. Mistral receives only the selected lowest-price records, so it cannot reorder a higher-price program into a cheapest result.

For complex requirements use the structured profile API; the question adapter cannot reliably interpret every possible wording. Each question is independent; there is no chat memory. The original question reaches Mistral as ranking context. Mistral orders records and selects supplied reason IDs; the application assembles displayed prose and facts from that evidence. Candidates without a subject keyword match are omitted when a subject is requested.

Every question response includes an `api` object with `called`, `status` and `model`. Successful calls also report the provider's request ID, token usage and elapsed time when available. Errors identify HTTP failures, connection failures or rejected model output. The page shows this information directly, and the server logs the status without logging the key or question. In Mistral mode the question endpoint attempts the API even with zero candidates; the model must return an empty recommendation list in that case. Offline preview never calls the API. These semantics differ deliberately from the strict profile endpoint's empty-result shortcut.

`POST /api/retrieve` or `POST /api/recommend`, with `Content-Type: application/json`:

```json
{"profile": {"student_id": "S001", "interests": ["economics"]}, "top_k": 8, "offline": true}
```

The server binds to loopback, accepts same-origin browser requests, and does not store profiles. It is a local prototype without accounts, production authentication, rate limiting or a deployment setup. The question endpoint still supplies a database answer when the API fails, explicitly marked `database_fallback` with the API error. There is no silent fallback. Strict CLI/profile recommendations continue to surface failures. Mistral calls require a key and network access; automated tests mock the API and do not establish live model availability. Restart the server after code or `.env` changes and refresh the page.
