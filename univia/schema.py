from __future__ import annotations

import math
import re
from datetime import date
from urllib.parse import urlparse


class ValidationError(ValueError):
    pass


TEXT_FIELDS = "program_id university program_name degree_level country city program_url source_url last_verified_date tuition_currency fee_period living_cost_currency living_cost_period living_cost_source_url campus_type location_type overview fee_notes fee_scope fee_year teaching_language_notes".split()
LIST_FIELDS = "fields_of_study teaching_language full_program_languages admission_requirements academic_prerequisites key_subjects distinctive_features career_paths scholarships".split()
NUMBER_FIELDS = "tuition_fee extra_charges first_year_academic_total estimated_living_cost duration minimum_grades minimum_grade_scale annual_additional_costs".split()
BOOL_FIELDS = "costs_verified requirements_verified".split()
OTHER_FIELDS = ["provenance", "raw_record", "quality_warnings", "required_qualifications", "related_records", "source_records", "field_sources"]
PROGRAM_FIELDS = TEXT_FIELDS + LIST_FIELDS + NUMBER_FIELDS + BOOL_FIELDS + OTHER_FIELDS
DEGREES = {"bachelor", "master", "doctorate", "integrated_master", "associate", "diploma"}
UNKNOWN = {"", "unknown", "not verified", "not stated", "n/a", "null", "none"}


def clean(value):
    if value is None or (isinstance(value, str) and value.strip().casefold() in UNKNOWN):
        return None
    return value.strip() if isinstance(value, str) else value


def key(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValidationError(f"{name} must be a finite nonnegative number")
    return value


def string_list(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValidationError(f"{name} must be an array of nonempty strings")
    return value


def validate_program(raw):
    if not isinstance(raw, dict):
        raise ValidationError("Each program must be an object")
    unknown = set(raw) - set(PROGRAM_FIELDS)
    if unknown:
        raise ValidationError(f"Unmapped program fields: {sorted(unknown)}")
    p = {f: clean(raw.get(f)) for f in PROGRAM_FIELDS}
    for f in TEXT_FIELDS:
        if p[f] is not None and not isinstance(p[f], str):
            raise ValidationError(f"{f} must be a string or null")
    for f in ("program_id", "university", "program_name", "country"):
        if not p[f]:
            raise ValidationError(f"Missing required field: {f}")
    for f in LIST_FIELDS + ["required_qualifications", "quality_warnings"]:
        if p[f] is not None:
            string_list(p[f], f)
    for f in NUMBER_FIELDS:
        if p[f] is not None:
            number(p[f], f)
    for f in BOOL_FIELDS:
        if p[f] is not None and type(p[f]) is not bool:
            raise ValidationError(f"{f} must be a boolean or null")
    for f in ("tuition_currency", "living_cost_currency"):
        if p[f] is not None:
            p[f] = p[f].upper()
            if not re.fullmatch(r"[A-Z]{3}", p[f]):
                raise ValidationError(f"{f} must be a three-letter currency code")
    if p["degree_level"] is not None:
        p["degree_level"] = key(p["degree_level"])
        if p["degree_level"] not in DEGREES:
            raise ValidationError("Unsupported degree_level")
    for f in ("fee_period", "living_cost_period"):
        if p[f] not in (None, "annual", "monthly", "semester", "total", "ects"):
            raise ValidationError(f"Unsupported {f}")
    for f in ("program_url", "source_url", "living_cost_source_url"):
        if p[f] is not None and (urlparse(p[f]).scheme not in ("http", "https") or not urlparse(p[f]).netloc):
            raise ValidationError(f"{f} must be an HTTP(S) URL")
    if p["last_verified_date"] is not None:
        try:
            verified = date.fromisoformat(p["last_verified_date"])
        except ValueError as exc:
            raise ValidationError("last_verified_date must be YYYY-MM-DD") from exc
        if verified > date.today():
            raise ValidationError("last_verified_date cannot be in the future")
    if p["minimum_grades"] is not None:
        if not p["minimum_grade_scale"] or p["minimum_grades"] > p["minimum_grade_scale"]:
            raise ValidationError("minimum_grades requires a valid minimum_grade_scale")
    if p["provenance"] is not None and not isinstance(p["provenance"], list):
        raise ValidationError("provenance must be an array")
    if p["raw_record"] is not None and not isinstance(p["raw_record"], dict):
        raise ValidationError("raw_record must be an object")
    for f in ("related_records", "field_sources"):
        if p[f] is not None and not isinstance(p[f], dict):
            raise ValidationError(f"{f} must be an object")
    if p["source_records"] is not None and not isinstance(p["source_records"], list):
        raise ValidationError("source_records must be an array")
    return p


PROFILE_FIELDS = set("student_id academic_summary grades previous_qualification desired_degree_level interests preferred_subjects career_goals annual_total_budget annual_tuition_budget currency preferred_countries excluded_countries preferred_languages campus_preferences location_preferences hard_constraints flexible_preferences expectations_summary".split())
HARD_FIELDS = {"eligible_countries", "teaching_languages", "campus_types", "location_types", "require_verified_requirements", "required_fields", "max_verification_age_days"}
PROFILE_LISTS = "interests preferred_subjects career_goals preferred_countries excluded_countries preferred_languages campus_preferences location_preferences flexible_preferences".split()


def validate_profile(raw):
    if not isinstance(raw, dict) or set(raw) - PROFILE_FIELDS:
        raise ValidationError("Student profile must be an object using the documented fields")
    p = dict(raw)
    if not isinstance(p.get("student_id"), str) or not p["student_id"].strip():
        raise ValidationError("student_id is required (use a fictional ID)")
    for f in PROFILE_LISTS:
        p[f] = string_list(p.get(f, []), f)
    for f in ("academic_summary", "previous_qualification", "expectations_summary"):
        if p.get(f) is not None and not isinstance(p[f], str):
            raise ValidationError(f"{f} must be a string")
    degree = p.get("desired_degree_level")
    if degree is not None and degree not in DEGREES:
        raise ValidationError("Invalid desired_degree_level")
    for budget_field in ("annual_total_budget", "annual_tuition_budget"):
        if p.get(budget_field) is not None:
            number(p[budget_field], budget_field)
    if any(p.get(f) is not None for f in ("annual_total_budget", "annual_tuition_budget")):
        if not isinstance(p.get("currency"), str) or not re.fullmatch(r"[A-Za-z]{3}", p["currency"]):
            raise ValidationError("A three-letter currency is required with a budget")
        p["currency"] = p["currency"].upper()
    grades = p.get("grades")
    if grades is not None:
        if not isinstance(grades, dict) or set(grades) != {"average", "scale"}:
            raise ValidationError("grades must contain average and scale")
        number(grades["average"], "grades.average")
        number(grades["scale"], "grades.scale")
        if not grades["scale"] or grades["average"] > grades["scale"]:
            raise ValidationError("Invalid grade scale")
    h = p.get("hard_constraints", {})
    if not isinstance(h, dict) or set(h) - HARD_FIELDS:
        raise ValidationError(f"Supported hard_constraints: {sorted(HARD_FIELDS)}; unknown constraints are never ignored")
    for f in HARD_FIELDS - {"require_verified_requirements", "max_verification_age_days"}:
        if f in h:
            string_list(h[f], f)
            if not h[f]:
                raise ValidationError(f"{f} must not be empty; omit it when unconstrained")
    if "required_fields" in h and set(h["required_fields"]) - set(PROGRAM_FIELDS):
        raise ValidationError("Unknown required_fields")
    if "require_verified_requirements" in h and type(h["require_verified_requirements"]) is not bool:
        raise ValidationError("require_verified_requirements must be boolean")
    if "max_verification_age_days" in h:
        age = h["max_verification_age_days"]
        if type(age) is not int or age < 0:
            raise ValidationError("max_verification_age_days must be a nonnegative integer")
    p["hard_constraints"] = h
    return p
