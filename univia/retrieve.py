from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from .schema import ValidationError, key, validate_profile


def tokens(value):
    return set(re.findall(r"[^\W_]{2,}", key(value), re.UNICODE)) - {"and", "the", "with", "for", "of", "in", "to"}


def verification_errors(p, h):
    errors = []
    if not p["source_url"] or not p["last_verified_date"]:
        errors.append("Missing source URL or verification date")
    elif (date.today() - date.fromisoformat(p["last_verified_date"])).days > h.get("max_verification_age_days", 365):
        errors.append("Verification is stale")
    return errors


def eligibility(p, student):
    """Fail closed: missing facts needed by a hard constraint cause exclusion."""
    h, errors = student["hard_constraints"], []
    if student.get("desired_degree_level") and p["degree_level"] != student["desired_degree_level"]:
        errors.append("Degree level differs or is unknown")
    if key(p["country"]) in {key(x) for x in student["excluded_countries"]}:
        errors.append("Country is excluded")
    checks = [("country", h.get("eligible_countries")), ("campus_type", h.get("campus_types")), ("location_type", h.get("location_types"))]
    for field, allowed in checks:
        if allowed and (p[field] is None or key(p[field]) not in {key(x) for x in allowed}):
            errors.append(f"{field} is unknown or outside the allowed values")
    allowed = h.get("teaching_languages")
    if allowed and not {key(x) for x in (p["full_program_languages"] or [])} & {key(x) for x in allowed}:
        errors.append("No confirmed full-program track in an allowed teaching language")
    for f in h.get("required_fields", []):
        if p[f] is None or p[f] == []:
            errors.append(f"Required field is missing: {f}")
    if "max_verification_age_days" in h:
        errors += verification_errors(p, h)
    if student.get("annual_tuition_budget") is not None:
        errors += verification_errors(p, h)
        if p["tuition_fee"] is None or p["fee_period"] != "annual":
            errors.append("Annual tuition is missing or its period is not annual")
        if p["tuition_currency"] != student["currency"]:
            errors.append("Tuition currency differs; no implicit exchange rates")
        elif p["fee_period"] == "annual" and p["tuition_fee"] is not None and p["tuition_fee"] > student["annual_tuition_budget"]:
            errors.append("Annual tuition exceeds budget")
    if student.get("annual_total_budget") is not None:
        errors += verification_errors(p, h)
        if p["costs_verified"] is not True:
            errors.append("Total annual costs are not verified for this applicant/fee scope")
        if p["fee_period"] != "annual" or p["living_cost_period"] != "annual":
            errors.append("Annual tuition and annual living costs are required")
        if any(p[f] is None for f in ("tuition_fee", "estimated_living_cost", "annual_additional_costs", "living_cost_source_url")):
            errors.append("Incomplete total cost evidence (tuition, living, additional costs, source)")
        if p["tuition_currency"] != student["currency"] or p["living_cost_currency"] != student["currency"]:
            errors.append("Currency mismatch; no implicit exchange rates")
        costs = [p[f] for f in ("tuition_fee", "estimated_living_cost", "annual_additional_costs")]
        if all(x is not None for x in costs) and sum(costs) > student["annual_total_budget"]:
            errors.append("Total annual cost exceeds budget")
    # Structured known requirements always apply; no cross-scale grade equivalence inferred.
    if p["minimum_grades"] is not None:
        grades = student.get("grades")
        if not grades or grades["scale"] != p["minimum_grade_scale"]:
            errors.append("Grades are missing or use an incomparable scale")
        elif grades["average"] < p["minimum_grades"]:
            errors.append("Grades below the published minimum")
    if p["required_qualifications"]:
        if key(student.get("previous_qualification")) not in {key(x) for x in p["required_qualifications"]}:
            errors.append("Previous qualification does not satisfy the structured requirement")
    if h.get("require_verified_requirements"):
        errors += verification_errors(p, h)
        if p["requirements_verified"] is not True:
            errors.append("Admission requirements are not verified")
        # Free-text admission rules cannot be machine certified in Phase 1.
        if p["admission_requirements"] or p["academic_prerequisites"]:
            errors.append("Narrative admission rules require human review; eligibility cannot be certified")
        if p["required_qualifications"] is None or p["minimum_grades"] is None:
            errors.append("Structured qualification and grade rules are incomplete")
    return sorted(set(errors))


def known_conflicts(p, student):
    """Separate known failures from missing evidence for exploratory results."""
    h, conflicts = student["hard_constraints"], []
    for field, allowed in (("country", h.get("eligible_countries")), ("campus_type", h.get("campus_types")), ("location_type", h.get("location_types"))):
        if allowed and p[field] is not None and key(p[field]) not in {key(x) for x in allowed}:
            conflicts.append(f"{field} is outside your allowed values")
    if key(p["country"]) in {key(x) for x in student["excluded_countries"]}:
        conflicts.append("Country is excluded")
    if student.get("desired_degree_level") and p["degree_level"] and student["desired_degree_level"] != p["degree_level"]:
        conflicts.append("Degree level differs")
    if h.get("teaching_languages") and p["full_program_languages"] and not {key(x) for x in h["teaching_languages"]} & {key(x) for x in p["full_program_languages"]}:
        conflicts.append("No recorded full track in your teaching language")
    currency = student.get("currency")
    tuition = p["tuition_fee"]
    if p["fee_period"] == "annual" and p["tuition_currency"] == currency and tuition is not None:
        for field in ("annual_tuition_budget", "annual_total_budget"):
            if student.get(field) is not None and tuition > student[field]:
                conflicts.append("Known annual tuition exceeds your budget")
        if student.get("annual_total_budget") is not None and p["living_cost_period"] == "annual" and p["living_cost_currency"] == currency:
            costs = [p[f] for f in ("tuition_fee", "estimated_living_cost", "annual_additional_costs")]
            if sum(x for x in costs if x is not None) > student["annual_total_budget"]:
                conflicts.append("Known annual costs exceed your budget")
    grades = student.get("grades")
    if grades and p["minimum_grades"] is not None and grades["scale"] == p["minimum_grade_scale"] and grades["average"] < p["minimum_grades"]:
        conflicts.append("Grades below the published minimum")
    if student.get("previous_qualification") and p["required_qualifications"] and key(student["previous_qualification"]) not in {key(x) for x in p["required_qualifications"]}:
        conflicts.append("Previous qualification does not satisfy the requirement")
    return conflicts


def rank_candidate(p, student, graph_paths):
    reasons, score = [], 0.0

    def add(field, preference, text, weight, paths=None):
        nonlocal score
        rid = "R" + str(len(reasons) + 1)
        reasons.append({"reason_id": rid, "text": text, "evidence": {"field": field, "value": p[field]}, "profile_field": preference, "graph_paths": paths or []})
        score += weight

    query = set()
    for f in ("interests", "preferred_subjects", "career_goals"):
        query |= tokens(" ".join(student[f]))
    for field, weight in (("program_name", 4), ("fields_of_study", 4), ("key_subjects", 3), ("overview", 1), ("career_paths", 2), ("distinctive_features", 1)):
        val = p[field]
        overlap = query & tokens(" ".join(val) if isinstance(val, list) else val or "")
        if overlap:
            add(field, "interests/preferred_subjects/career_goals", f"Keyword match in {field}: {', '.join(sorted(overlap))}.", weight * len(overlap))
    for field, preference, weight in (("country", "preferred_countries", 2), ("campus_type", "campus_preferences", 2), ("location_type", "location_preferences", 2), ("full_program_languages", "preferred_languages", 2)):
        values = p[field] if isinstance(p[field], list) else [p[field]]
        matches = {key(v) for v in values if v} & {key(v) for v in student[preference]}
        if matches:
            add(field, preference, f"Matches {preference}: {', '.join(sorted(matches))}.", weight)
    # Actual graph traversal: student term -> subject node <- eligible program.
    matching_paths = [path for path in graph_paths if path["relation"] == "studies" and query & tokens(path["label"])]
    if matching_paths:
        add("fields_of_study", "interests/preferred_subjects/career_goals", "Subject graph connects your interests to this program.", 2 * len(matching_paths), matching_paths)
    if not reasons:
        add("program_name", "hard_constraints", "This record passed the supplied hard filters; no keyword or preference match was found.", 0)
    warnings = list(p["quality_warnings"] or [])
    if student.get("annual_total_budget") is None:
        warnings.append("Affordability was not checked: no annual budget was supplied")
    if not student["hard_constraints"].get("require_verified_requirements"):
        warnings.append("Admission eligibility is not certified; verify narrative and missing requirements")
    return {"program_id": p["program_id"], "retrieval_score": score, "record": p, "allowed_reasons": reasons, "warnings": warnings}


def retrieve(database, raw_student, top_k=8, include_unverified=False, cheapest=False):
    student = validate_profile(raw_student)
    if type(top_k) is not int or not 5 <= top_k <= 10:
        raise ValidationError("top_k must be an integer between 5 and 10")
    database = Path(database).resolve()
    if not database.is_file():
        raise ValidationError("Database is missing; run ingest first")
    candidates, rejected = [], []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        for (record,) in db.execute("SELECT record FROM programs ORDER BY program_id"):
            p = json.loads(record)
            errors = eligibility(p, student)
            if errors and (not include_unverified or known_conflicts(p, student)):
                rejected.append({"program_id": p["program_id"], "reasons": errors})
                continue
            paths = [{"program_id": p["program_id"], "relation": r, "node_id": n, "label": label, "evidence_field": f} for r, n, label, f in db.execute("SELECT e.relation,e.target,n.label,e.evidence_field FROM edges e JOIN nodes n ON n.node_id=e.target WHERE e.source=? ORDER BY e.relation,e.target", (p["program_id"],))]
            c = rank_candidate(p, student, paths)
            c.update(match_status="unverified" if errors else "passes_supplied_filters", pending_checks=errors)
            candidates.append(c)
    if cheapest:
        academic_fields = {"program_name", "fields_of_study", "key_subjects", "overview", "career_paths", "distinctive_features"}
        relevant = [c for c in candidates if not student["interests"] or any(r["evidence"]["field"] in academic_fields and r["profile_field"] != "hard_constraints" for r in c["allowed_reasons"])]
        groups = {}
        for c in relevant:
            p = c["record"]
            if p["tuition_fee"] is None or not p["tuition_currency"]:
                continue
            group = (p["tuition_currency"], p["fee_period"])
            old = groups.get(group)
            if old is None or (p["tuition_fee"], -c["retrieval_score"], c["program_id"]) < (old["record"]["tuition_fee"], -old["retrieval_score"], old["program_id"]):
                groups[group] = c
        candidates = list(groups.values())
        candidates.sort(key=lambda c: (c["record"]["tuition_currency"], c["record"]["fee_period"] or "", c["record"]["tuition_fee"], c["program_id"]))
    else:
        candidates.sort(key=lambda c: (bool(c["pending_checks"]), -c["retrieval_score"], c["program_id"]))
    return {"student_profile": student, "eligible_count": sum(not c["pending_checks"] for c in candidates), "unverified_count": sum(bool(c["pending_checks"]) for c in candidates), "candidates": candidates[:top_k], "excluded": rejected}
