"""Consolidate raw GDELT events into deduplicated physical incidents.

Raw events are never modified or deleted. Consolidation produces a separate
incident record that links back to every contributing event id, story URL and
source article, with a recorded confidence and the reason the records were
judged to be the same physical occurrence.

Blocking key keeps the comparison cheap: only events in the same country, the
same security category and within +/- 1 day are ever compared.
"""
from __future__ import annotations
import hashlib, math, re
from typing import Any, Iterable
from datetime import date

# --- tuning -----------------------------------------------------------------
MERGE_THRESHOLD = 0.60      # total score at or above this merges
MAX_KM = 75.0               # beyond this, never merge regardless of text
DAY_TOLERANCE = 2           # events this many days apart may still be one incident

W_GEO, W_TIME, W_TEXT, W_ACTOR, W_SUBTYPE, W_CAT, W_FATAL = (
    0.26, 0.10, 0.26, 0.10, 0.05, 0.10, 0.13)

# A centroid is not a location. When either record is coded at country/region
# precision, matching coordinates only prove "same country", so geography is
# capped and the merge has to be carried by text, actors and casualty figures.
CENTROID_GEO_CAP = 0.35

# Coders legitimately disagree on whether one strike is Explosions/Remote
# violence or Violence against civilians. These are treated as compatible so
# the same physical incident can still be consolidated across that boundary.
KINETIC = {"AIR_MISSILE_DRONE", "TERRORISM_IED", "VIOLENCE_AGAINST_CIVILIANS",
           "ARMED_CONFLICT", "STATE_CONTROL_BREAKDOWN"}

_STOP = {
    "the", "a", "an", "in", "on", "of", "and", "to", "at", "for", "by", "was", "were",
    "is", "are", "said", "reported", "reportedly", "after", "near", "from", "with",
    "that", "this", "its", "it", "as", "has", "have", "had", "but", "not", "no", "new",
}


def _tokens(s: str | None) -> set[str]:
    if not s:
        return set()
    return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in _STOP}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371.0088
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _actor_names(ev: dict) -> set[str]:
    return {(a.get("name") or "").strip().lower() for a in (ev.get("actors") or []) if a.get("name")}


def similarity(e1: dict, e2: dict) -> tuple[float, dict]:
    """Score two events as the same physical incident. Returns (score, evidence)."""
    ev: dict[str, Any] = {}
    g1, g2 = e1.get("geo") or {}, e2.get("geo") or {}
    lat1, lon1 = g1.get("latitude"), g1.get("longitude")
    lat2, lon2 = g2.get("latitude"), g2.get("longitude")

    p1, p2 = g1.get("geo_precision"), g2.get("geo_precision")
    centroid = 3 in (p1, p2)

    # Geography
    if None not in (lat1, lon1, lat2, lon2):
        km = haversine_km((lat1, lon1), (lat2, lon2))
        ev["distance_km"] = round(km, 2)
        if km > MAX_KM:
            return 0.0, {**ev, "blocked": f"{km:.0f} km apart, above {MAX_KM:.0f} km ceiling"}
        geo = max(0.0, 1.0 - (km / MAX_KM) ** 0.6)
        if centroid:
            geo = min(geo, CENTROID_GEO_CAP)
            ev["geo_basis"] = "one or both records coded at country/region centroid"
    else:
        # No coordinates on one or both sides: fall back to named place, and cap
        # the achievable score so an unlocated pair cannot merge on text alone.
        same_place = (g1.get("location") or "?").lower() == (g2.get("location") or "??").lower()
        geo = 0.55 if same_place else 0.15
        ev["distance_km"] = None
        ev["geo_basis"] = "named place" if same_place else "country only"

    # Time
    d1, d2 = e1.get("event_date"), e2.get("event_date")
    dd = abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days) if d1 and d2 else 9
    ev["days_apart"] = dd
    if dd > DAY_TOLERANCE:
        return 0.0, {**ev, "blocked": f"{dd} days apart"}
    tscore = {0: 1.0, 1: 0.6, 2: 0.35}[dd]

    # Text
    t1 = _tokens((e1.get("title") or "") + " " + (e1.get("summary") or ""))
    t2 = _tokens((e2.get("title") or "") + " " + (e2.get("summary") or ""))
    text = _jaccard(t1, t2)
    ev["text_similarity"] = round(text, 3)
    ev["shared_terms"] = sorted(t1 & t2)[:8]

    # Actors
    a1, a2 = _actor_names(e1), _actor_names(e2)
    actor = _jaccard(a1, a2)
    ev["actor_overlap"] = round(actor, 3)

    # Subtype
    sub = 1.0 if (e1.get("subcategory") and e1.get("subcategory") == e2.get("subcategory")) else 0.0

    # Security category compatibility
    c1 = (e1.get("_cls") or {}).get("security_category")
    c2 = (e2.get("_cls") or {}).get("security_category")
    if c1 and c1 == c2:
        cat = 1.0
    elif c1 in KINETIC and c2 in KINETIC:
        cat = 0.65
        ev["category_note"] = f"cross-category match ({c1} / {c2}), both kinetic"
    else:
        cat = 0.0

    # Fatality agreement: an identical non-zero toll on the same day is strong
    # evidence of one incident double-reported.
    f1, f2 = e1.get("fatalities") or 0, e2.get("fatalities") or 0
    if f1 and f2:
        fatal = 1.0 if f1 == f2 else max(0.0, 1.0 - abs(f1 - f2) / max(f1, f2))
    else:
        fatal = 0.0
    ev["fatalities_compared"] = [f1, f2]

    score = (W_GEO * geo + W_TIME * tscore + W_TEXT * text + W_ACTOR * actor
             + W_SUBTYPE * sub + W_CAT * cat + W_FATAL * fatal)

    # Corroboration bonus. Two records in the same country, within a day, with a
    # compatible category and an EXACT non-trivial casualty figure are almost
    # always one incident reported twice. Wire services repeat the toll verbatim
    # while headlines vary, which is precisely where text similarity is weakest.
    if f1 and f1 == f2 and f1 >= 3 and dd <= 1 and cat >= 0.65:
        score += 0.10
        ev["corroboration"] = f"identical casualty figure ({f1}) within {dd} day(s)"
    ev["components"] = {
        "geo": round(geo, 3), "time": round(tscore, 3), "text": round(text, 3),
        "actor": round(actor, 3), "subtype": sub, "category": cat, "fatalities": round(fatal, 3),
    }
    return round(score, 4), ev


def _incident_id(events: list[dict]) -> str:
    seed = "|".join(sorted(e["id"] for e in events))
    return "inc_" + hashlib.sha1(seed.encode()).hexdigest()[:16]


def _pick_primary(events: list[dict]) -> dict:
    """The best-evidenced record leads the incident: most fatalities reported,
    then highest confidence, then most corroborating articles."""
    def key(e):
        m = e.get("metrics") or {}
        return (e.get("fatalities") or 0, m.get("confidence") or 0, m.get("article_count") or 0)
    return max(events, key=key)


def consolidate(events: Iterable[dict]) -> list[dict]:
    """Group classified events into incidents. Union-find over blocked pairs."""
    events = [e for e in events if e.get("_cls", {}).get("counts_as_danger")]
    buckets: dict[tuple, list[dict]] = {}
    for e in events:
        geo = e.get("geo") or {}
        # Blocked on country ALONE. Blocking on category too would prevent the
        # most common real duplicate: one physical strike coded once as
        # Explosions/Remote violence and once as Violence against civilians.
        buckets.setdefault(geo.get("country"), []).append(e)

    incidents: list[dict] = []
    for country, group in buckets.items():
        group.sort(key=lambda e: e.get("event_date") or "")
        parent = list(range(len(group)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        pair_ev: dict[tuple[int, int], dict] = {}
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                d1, d2 = group[i].get("event_date"), group[j].get("event_date")
                if d1 and d2 and abs((date.fromisoformat(d1) - date.fromisoformat(d2)).days) > DAY_TOLERANCE:
                    continue
                s, evd = similarity(group[i], group[j])
                if s >= MERGE_THRESHOLD:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[ri] = rj
                    pair_ev[(i, j)] = {"score": s, **evd}

        clusters: dict[int, list[int]] = {}
        for i in range(len(group)):
            clusters.setdefault(find(i), []).append(i)

        for root, idxs in clusters.items():
            members = [group[i] for i in idxs]
            primary = _pick_primary(members)
            iid = _incident_id(members)
            evidence = [
                {"pair": [group[i]["id"], group[j]["id"]], **e}
                for (i, j), e in pair_ev.items() if i in idxs and j in idxs
            ]
            dedup_conf = round(max([e["score"] for e in evidence], default=1.0), 3) if len(members) > 1 else 1.0

            sources, seen = [], set()
            for m in members:
                for art in (m.get("top_articles") or []):
                    u = art.get("url")
                    if u and u not in seen:
                        seen.add(u)
                        sources.append({
                            "url": u, "title": art.get("title"),
                            "domain": art.get("domain"), "via_event_id": m["id"],
                        })

            story_urls = sorted({m.get("primary_story_url") for m in members if m.get("primary_story_url")})
            geo = primary.get("geo") or {}
            seccat = primary["_cls"]["security_category"]
            merged_cats = sorted({m["_cls"]["security_category"] for m in members})
            fatal_reports = sorted({(m.get("fatalities") or 0) for m in members})

            incidents.append({
                "incident_id": iid,
                "event_date": primary.get("event_date"),
                "country": country,
                "region": geo.get("region"),
                "continent": geo.get("continent"),
                "admin1": geo.get("admin1"),
                "location": geo.get("location"),
                "latitude": geo.get("latitude"),
                "longitude": geo.get("longitude"),
                "geo_precision": geo.get("geo_precision"),
                "geo_precision_label": geo.get("geo_precision_label"),
                "security_category": seccat,
                "security_subtype": primary["_cls"]["security_subtype"],
                "overlay_categories": sorted({o for m in members for o in m["_cls"]["overlay_categories"]}),
                "merged_categories": merged_cats,
                "category_disputed": len(merged_cats) > 1,
                "native_category": primary.get("category"),
                "native_subcategory": primary.get("subcategory"),
                "title": primary.get("title"),
                "summary": primary.get("summary"),
                "actors": primary.get("actors") or [],
                "organizations": [e.get("name") for e in (primary.get("entity_refs") or [])
                                  if e.get("type") == "organization"],
                "fatalities": max((m.get("fatalities") or 0) for m in members),
                "fatalities_reported_range": [fatal_reports[0], fatal_reports[-1]],
                "fatalities_disputed": len(fatal_reports) > 1,
                "significance": (primary.get("metrics") or {}).get("significance"),
                "goldstein_scale": (primary.get("metrics") or {}).get("goldstein_scale"),
                "confidence": (primary.get("metrics") or {}).get("confidence"),
                "article_count": sum((m.get("metrics") or {}).get("article_count") or 0 for m in members),
                "impact_realised": all(m["_cls"]["impact_realised"] for m in members),
                "verification_status": primary["_cls"]["verification_status"],
                "event_ids": sorted(m["id"] for m in members),
                "event_count": len(members),
                "story_urls": story_urls,
                "gdelt_event_urls": sorted({m.get("url") for m in members if m.get("url")}),
                "sources": sources,
                "dedup_confidence": dedup_conf,
                "dedup_evidence": evidence,
                "dedup_method": "blocked pairwise geo+time+text+actor, union-find" if len(members) > 1 else "singleton",
            })

    incidents.sort(key=lambda i: (i["event_date"] or "", i["significance"] or 0), reverse=True)
    return incidents
