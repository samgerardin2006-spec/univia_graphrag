"""A small, explicit English question adapter for the Phase 1 search rules."""
from __future__ import annotations

import re
import json
import sqlite3
from collections import Counter
from pathlib import Path

from .countries import COUNTRIES
from .generate import MistralError, recommend
from .retrieve import retrieve, tokens
from .schema import ValidationError, validate_profile

LANGUAGES = ["English", "Spanish", "Portuguese", "French", "German", "Italian", "Dutch", "Greek", "Catalan", "Afrikaans"]
REGIONS = {
    "eastern europe": ["Bulgaria", "Czechia", "Hungary", "Poland", "Romania", "Slovakia", "Ukraine", "Moldova", "Belarus", "Russia"],
    "western europe": ["Austria", "Belgium", "France", "Germany", "Liechtenstein", "Luxembourg", "Monaco", "Netherlands", "Switzerland"],
    "southern europe": ["Spain", "Portugal", "Italy", "Greece", "Malta", "Croatia", "Slovenia", "Albania", "Montenegro", "Serbia", "North Macedonia", "Bosnia and Herzegovina"],
}
STOP = set("i im i'm want looking look find show me recommend recommendations study studying course courses program programs programme programmes university universities degree degrees interested interest please which what where can could should would best good options option like my a an is are that with for and or in of to the at about available offer offers do does tell more preferably prefer only need taught teaching language speak speaking under below less than budget total annual year per have has not no except exclude excluding avoid dont don't anywhere euros euro eur usd gbp zar cost costs tuition fees fee bachelor bachelors master's masters master phd doctorate".split())


def parse_question(question):
    if not isinstance(question, str) or not 3 <= len(question.strip()) <= 2000:
        raise ValidationError("Please enter a question between 3 and 2,000 characters.")
    q = question.strip()
    lower = q.casefold().replace("’", "'")
    p = {"student_id": "QUESTION", "expectations_summary": q, "hard_constraints": {}}
    h, understood, clarifications = p["hard_constraints"], [], []
    pending_budget = None
    positive, negative = [], []
    country_mentions = []
    for country, aliases in COUNTRIES.items():
        for alias in aliases:
            for match in re.finditer(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", lower):
                country_mentions.append((match.start(), match.end(), country))
    # Prefer the full name: Papua New Guinea must not also select Guinea, and
    # South Sudan must not also select Sudan.
    end = -1
    for start, stop, country in sorted(country_mentions, key=lambda m: (m[0], -m[1])):
        if start < end:
            continue
        end = stop
        before = lower[max(0, start - 55):start]
        target = negative if re.search(r"\b(?:no|not(?: in)?|except|excluding|exclude|avoid|outside(?: of)?)\s+(?:(?:[a-z ]+)(?:,| and | or )\s*)*$", before) else positive
        if country not in target:
            target.append(country)
    if positive:
        h["eligible_countries"] = positive
        understood.append("Countries: " + ", ".join(positive))
    if negative:
        p["excluded_countries"] = negative
        understood.append("Exclude: " + ", ".join(negative))
    for region, countries in REGIONS.items():
        if region in lower:
            h["eligible_countries"] = [x for x in positive if x in countries] if positive else countries
            if not h["eligible_countries"]:
                h["eligible_countries"] = countries
                clarifications.append("The named country and region conflict; the region filter is shown explicitly and the country needs clarification.")
            understood.append("Region: " + region.title() + " (" + ", ".join(countries) + ")")
            break
    for label, pattern in (("bachelor", r"\b(?:bachelor'?s?|undergraduate)\b"), ("master", r"\bmaster'?s?\b"), ("doctorate", r"\b(?:phd|doctorate|doctoral)\b")):
        if re.search(pattern, lower):
            if p.get("desired_degree_level"):
                clarifications.append("Please ask about one degree level at a time.")
            p["desired_degree_level"] = label
            understood.append("Degree: " + label)
    languages = [lang for lang in LANGUAGES if re.search(r"\b" + lang.lower() + r"\b", lower)]
    if languages:
        # A requested teaching language is mandatory; an incidental mention can be clarified.
        h["teaching_languages"] = languages
        understood.append("Full teaching language: " + " or ".join(languages))
        if re.search(r"\b(?:not|except|excluding|avoid)\s+(?:taught\s+)?(?:in\s+)?(?:" + "|".join(x.lower() for x in languages) + r")\b", lower):
            clarifications.append("Please name the teaching language you want; excluding a language needs a manual check for bilingual requirements.")
    currency_pattern = r"(?:€|\bEUR\b|\beuros?\b|\$|\bUSD\b|\bdollars?\b|£|\bGBP\b|\bpounds?\b|\bZAR\b)"
    amount_pattern = r"(\d+(?:[ ,]\d{3})*(?:\.\d+)?\s*[kK]?)"
    amounts = list(re.finditer(currency_pattern + r"\s*" + amount_pattern + r"|" + amount_pattern + r"\s*" + currency_pattern, q, re.I))
    cheapest = bool(re.search(r"\b(?:cheap(?:est)?|lowest(?:[- ]cost|[- ]priced?| tuition| fees?)?|least expensive|most affordable)\b", lower))
    money_words = re.search(r"\b(?:budget|afford\w*|under|below|less than|maximum|max)\b", lower)
    tuition_only = bool(re.search(r"tuition[ -]only|only tuition|for tuition|tuition budget|(?:don't|dont|do not) care about living", lower)) or ("tuition" in lower and "total" not in lower and "including living" not in lower)
    scope = "tuition" if tuition_only else "total"
    if amounts:
        if len(amounts) != 1:
            clarifications.append("Several amounts were provided; the budget is not yet verified. Specify one budget and its period to confirm affordability.")
        else:
            match = amounts[0]
            raw = (match.group(1) or match.group(2)).replace(",", "").replace(" ", "")
            amount = float(raw.rstrip("kK")) * (1000 if raw[-1:].lower() == "k" else 1)
            segment = match.group().lower()
            currency = "EUR" if re.search(r"€|eur", segment) else "USD" if re.search(r"\$|usd|dollar", segment) else "GBP" if re.search(r"£|gbp|pound", segment) else "ZAR"
            if re.search(r"\b(?:month|monthly|semester)\b", lower) or not money_words:
                pending_budget = {"amount": amount, "currency": currency, "scope": scope}
                clarifications.append("The budget period or whether it is a maximum needs confirmation; I can still show research leads without claiming affordability.")
            else:
                p["annual_tuition_budget" if tuition_only else "annual_total_budget"] = amount
                p["currency"] = currency
                understood.append(f"Maximum annual {scope}: {amount:,.0f} {currency}")
    elif money_words:
        bare = re.search(amount_pattern, q)
        amount = None
        if bare:
            raw = bare.group(1).replace(",", "").replace(" ", "")
            amount = float(raw.rstrip("kK")) * (1000 if raw[-1:].lower() == "k" else 1)
        pending_budget = {"amount": amount, "currency": None, "scope": scope}
        clarifications.append((f"Your {scope} budget is {amount:,.0f}, but its currency is unspecified. " if amount is not None else "No numeric budget was supplied. ") + "Affordability remains unverified; add the currency when convenient.")
        if tuition_only:
            understood.append("Budget covers tuition only; living costs are excluded")
    if re.search(r"\b(?:scholarships?|visa|ranking|ranked|ielts|toefl|sat|gmat|gre|accessible|disability)\b", lower):
        clarifications.append("That requirement needs a manual evidence check. This search can enforce country, degree, teaching language, campus type, and annual total budget; it cannot certify the additional requirement.")
    if re.search(r"\b(?:grade|gpa|admission|eligible|qualify|qualification)\b", lower):
        clarifications.append("Admission eligibility needs a structured student profile and verified requirements. Please use the profile API or have the team review your qualifications; this question search cannot certify admission.")
    if re.search(r"\b(?:in|near|around|outside|within)\s+(?:europe|africa|asia|london|paris|barcelona|madrid|lisbon|porto|milan|rome|cape town|pretoria|johannesburg)\b", lower):
        clarifications.append("Please specify a country for the location filter. City, region and distance requirements need a manual check in this question search.")
    for value in ("urban", "rural", "suburban"):
        if re.search(r"\b" + value + r"\b", lower):
            h["campus_types"] = [value]
            understood.append("Campus: " + value)
    # Prevent unrecognized explicit mandatory conditions from becoming mere keywords.
    if re.search(r"\b(?:must|require|required)\b", lower) and not (languages or positive or p.get("desired_degree_level") or amounts or h.get("campus_types")):
        clarifications.append("Please specify the required country, teaching language, degree level, or annual budget. Other mandatory conditions need a manual evidence check.")
    interests = tokens(lower) - STOP - {lang.lower() for lang in languages} - set("eastern western southern europe budget living care costs yearly annual currency tuition only dont don't cheapest cheap lowest least expensive most affordable give option price priced cost".split())
    for aliases in COUNTRIES.values():
        for alias in aliases:
            interests -= tokens(alias)
    p["interests"] = sorted(x for x in interests if not x.isdigit())
    if len(re.findall(r"\b(?:bachelor'?s?|undergraduate|master'?s?|phd|doctorate|doctoral)\b", lower)) > 1:
        p.pop("desired_degree_level", None)
    return {"profile": validate_profile(p), "understood": understood, "clarifications": list(dict.fromkeys(clarifications)), "pending_budget": pending_budget, "cheapest": cheapest}


def answer_question(database, question, offline=False, transport=None):
    parsed = parse_question(question)
    retrieval = retrieve(database, parsed["profile"], include_unverified=True, cheapest=parsed["cheapest"])
    retrieval["search_notes"] = parsed["clarifications"]
    # Do not fill the shortlist with unrelated programs when a subject was requested.
    if parsed["profile"]["interests"]:
        academic_fields = {"program_name", "fields_of_study", "key_subjects", "overview", "career_paths", "distinctive_features"}
        retrieval["candidates"] = [c for c in retrieval["candidates"] if any(r["evidence"]["field"] in academic_fields and r["profile_field"] != "hard_constraints" for r in c["allowed_reasons"])]
    if parsed["clarifications"]:
        for c in retrieval["candidates"]:
            c["match_status"] = "unverified"
            c["pending_checks"] += parsed["clarifications"]
    try:
        result = recommend(retrieval, offline=offline, transport=transport, answer_when_empty=True)
    except MistralError as exc:
        # Still provide an evidence-based answer, and explicitly report the failed API call.
        result = recommend(retrieval, offline=True, answer_when_empty=True)
        result.update(mode="database_fallback", api=exc.api, api_error=str(exc))
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        coverage = sorted({json.loads(row[0])["country"] for row in db.execute("SELECT record FROM programs")})
        country_information = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='workbook_rows'").fetchone():
            wanted = {v.casefold() for v in parsed["profile"]["hard_constraints"].get("eligible_countries", [])}
            country_information = [r for (text,) in db.execute("SELECT record FROM workbook_rows WHERE table_name='Countries'") for r in [json.loads(text)] if r.get("name_en", "").casefold() in wanted]
    if not result["recommendations"]:
        answer = "No programs match this search. " + ("The workbook contains programs in " + ", ".join(coverage) + "." if coverage else "")
    else:
        count = len(result["recommendations"])
        answer = ("Here are the lowest listed tuition amounts, grouped by currency and fee period." if parsed["cheapest"] else f"Here {'is 1 program option' if count == 1 else 'are ' + str(count) + ' program options'} from the workbook.")
    if parsed["profile"].get("annual_tuition_budget") is not None or (parsed["pending_budget"] or {}).get("scope") == "tuition":
        answer += " Your budget applies to tuition only; living costs are not part of that check."
    counts = Counter(reason for item in retrieval["excluded"] for reason in item["reasons"])
    result.update(answer=answer, understood=parsed["understood"], question=question, notes=parsed["clarifications"], pending_budget=parsed["pending_budget"], coverage=coverage, country_information=country_information, exclusion_summary=[{"reason": r, "count": n} for r, n in counts.most_common(5)])
    return result
