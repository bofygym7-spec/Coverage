"""Deterministic security classification for GDELT Cloud events.

Four gates, in order:
  1. physical-security relevance  (is this danger at all?)
  2. native taxonomy -> security category
  3. keyword overlays (embassy / transport / coup / kidnapping ...)
  4. verification status from confidence + source count

No AI call is made here. AI is only invoked for events this module leaves
unresolved, and its output is written to separate columns so the deterministic
result is never overwritten. Rationale is recorded for every decision so a
classification can be audited or revised later.
"""
from __future__ import annotations
import json, os, re
from typing import Any

RULES_PATH = os.path.join(os.path.dirname(__file__), "classification_rules.json")
with open(RULES_PATH, encoding="utf-8") as fh:
    RULES = json.load(fh)

_REL = RULES["physical_security_relevant"]
_MAP = RULES["category_map"]["rules"]
_OVERLAYS = RULES["overlays"]["definitions"]
_VER = RULES["verification"]
CATEGORIES: dict[str, dict] = RULES["security_categories"]

# Pre-compile overlay keyword patterns once. Word-boundary matching avoids
# "port" firing inside "important" or "reported".
for _o in _OVERLAYS:
    _o["_pos"] = [re.compile(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", re.I) for k in _o["keywords"]]
    _o["_neg"] = [re.compile(re.escape(k), re.I) for k in _o.get("negative_keywords", [])]


def _haystack_core(ev: dict[str, Any]) -> str:
    """Title, summary and named actors only.

    Used for overlays that can PROMOTE an event to a new primary category.
    entity_refs and article titles are deliberately excluded here: GDELT
    documents entity matching as coverage through a linked story, and warns
    that a mention does not establish the entity is a party to the event. An
    aid agency quoted as a responder must not turn a strike into an attack on
    that agency.
    """
    parts = [ev.get("title") or "", ev.get("summary") or ""]
    for a in ev.get("actors") or []:
        parts.append(a.get("name") or "")
    return " \n ".join(p for p in parts if p)


def _haystack_wide(ev: dict[str, Any]) -> str:
    """Core plus linked entities and article headlines. Used only for
    non-promoting contextual flags, where a coverage-level mention is
    acceptable evidence for a tag but not for reclassification."""
    parts = [_haystack_core(ev)]
    for e in ev.get("entity_refs") or []:
        parts.append(e.get("name") or "")
    for a in (ev.get("top_articles") or [])[:5]:
        parts.append(a.get("title") or "")
    return " \n ".join(p for p in parts if p)


def is_physically_relevant(cat: str | None, sub: str | None) -> tuple[bool, str]:
    if not cat:
        return False, "no native category"
    if sub and sub in _REL["excluded_subcategories"]:
        return False, f"subcategory '{sub}' is administrative or non-physical"
    if cat in _REL["include_categories"]:
        return True, f"category '{cat}' is physical-security relevant"
    cond = _REL["conditional_categories"].get(cat)
    if cond:
        if sub and sub in cond["include_subcategories"]:
            return True, f"'{cat} :: {sub}' carries a physical-security component"
        return False, f"'{cat} :: {sub}' is not a physical-security subtype"
    if cat in _REL["excluded_categories"]:
        return False, f"category '{cat}' excluded: not physical danger"
    return False, f"category '{cat}' not in danger taxonomy"


def _base_category(cat: str, sub: str | None) -> tuple[str | None, str | None, bool, str]:
    for r in _MAP:
        if r["native_category"] != cat:
            continue
        if r["native_subcategory"] in ("*", sub):
            return (
                r["security_category"],
                r.get("security_subtype"),
                r.get("impact_realised", True),
                f"mapped from '{cat} :: {r['native_subcategory']}'",
            )
    return None, None, True, f"no mapping rule for '{cat} :: {sub}'"


def match_overlays(core_text: str, wide_text: str | None = None) -> list[dict]:
    hits = []
    for o in _OVERLAYS:
        # Promoting overlays are judged on core evidence only.
        text = core_text if o["promote"] else (wide_text or core_text)
        if any(n.search(text) for n in o["_neg"]):
            continue
        matched = [p.pattern for p in o["_pos"] if p.search(text)]
        if len(matched) >= o["min_matches"]:
            hits.append({
                "id": o["id"],
                "label": o["label"],
                "promote": o["promote"],
                "terms": [
                    re.sub(r"\(\?<!\[a-z\]\)|\(\?!\[a-z\]\)", "", m).replace("\\", "")
                    for m in matched[:4]
                ],
            })
    return hits


def verification_status(conf: float | None, n_articles: int) -> str:
    if conf is None or conf < _VER["verified_min_confidence"]:
        return _VER["insufficient_label"]
    if n_articles >= _VER["verified_min_articles"]:
        return "VERIFIED_MULTI_SOURCE"
    return "VERIFIED_SINGLE_SOURCE"


def classify(ev: dict[str, Any]) -> dict[str, Any]:
    """Return the classification block for one raw GDELT event."""
    cat = ev.get("category")
    sub = ev.get("subcategory")
    metrics = ev.get("metrics") or {}
    conf = metrics.get("confidence")
    n_art = metrics.get("article_count") or len(ev.get("top_articles") or [])

    out: dict[str, Any] = {
        "native_category": cat,
        "native_subcategory": sub,
        "security_category": None,
        "security_subtype": None,
        "overlay_categories": [],
        "counts_as_danger": False,
        "impact_realised": True,
        "classification_method": "deterministic",
        "classification_confidence": None,
        "classification_rationale": [],
        "verification_status": _VER["insufficient_label"],
    }

    relevant, why = is_physically_relevant(cat, sub)
    out["classification_rationale"].append(why)
    if not relevant:
        out["classification_method"] = "deterministic_excluded"
        return out

    base, subtype, realised, why2 = _base_category(cat, sub)
    out["classification_rationale"].append(why2)
    if base is None:
        # Physically relevant but unmapped. Store it, flag it, never guess.
        out["security_category"] = "OTHER_SERIOUS_THREAT"
        out["security_subtype"] = sub
        out["counts_as_danger"] = True
        out["verification_status"] = _VER["insufficient_label"]
        out["classification_rationale"].append("fell through to OTHER_SERIOUS_THREAT pending review")
        out["classification_confidence"] = 0.35
        return out

    out["security_category"] = base
    out["security_subtype"] = subtype or sub
    out["impact_realised"] = realised
    out["counts_as_danger"] = True

    overlays = match_overlays(_haystack_core(ev), _haystack_wide(ev))
    for o in overlays:
        if o["id"] not in out["overlay_categories"]:
            out["overlay_categories"].append(o["id"])
        out["classification_rationale"].append(
            f"overlay {o['id']} matched on: {', '.join(o['terms'])}"
        )

    # Promotion: an attack on an embassy is filed under diplomatic targeting,
    # not merely under 'explosion'. The displaced category is kept as an overlay
    # so nothing is lost and the original mapping stays visible.
    for o in overlays:
        if o["promote"] and o["id"] != out["security_category"]:
            displaced = out["security_category"]
            out["security_category"] = o["id"]
            if displaced not in out["overlay_categories"]:
                out["overlay_categories"].append(displaced)
            out["classification_rationale"].append(
                f"promoted to {o['id']} (was {displaced}) — targeting type outranks weapon type"
            )
            break

    out["verification_status"] = verification_status(conf, n_art)
    out["classification_confidence"] = round(min(1.0, 0.6 + 0.4 * (conf or 0)), 3)
    if not realised:
        out["classification_rationale"].append(
            "weapon intercepted or disrupted — counted separately from realised impact"
        )
    return out
