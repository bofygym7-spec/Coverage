#!/usr/bin/env python3
"""Danger status: measurement, designation, and where the two disagree.

The dashboard does not decide what counts as danger. Bodies whose job that is
already publish designations, and the framework this feeds already names them as
primary sources. So three things are computed and kept SEPARATE rather than
collapsed into one invented score:

  MEASUREMENT  a percentile band over this store's own severity-weighted
               incident counts. Says where a country sits relative to the rest
               of the observed world. Never an absolute judgement.

  DESIGNATION  whether an authority has designated the country for danger pay.
               Carries the authority, the rate and the effective date.

  DIVERGENCE   the useful output. A country measuring high with no designation
               is worth a committee look; one designated but measuring quiet
               may be a designation whose conditions have abated, or may be a
               place the press has stopped covering. Either way the dashboard
               points at it rather than resolving it.

Collapsing these into a single LOW/MEDIUM/HIGH would hide which part is
evidence and which part is authority, and would make the number impossible to
defend in a hardship-allowance decision.
"""
from __future__ import annotations

import json
import os
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESIGNATIONS_PATH = os.path.join(ROOT, "db", "official_designations.json")

# Percentile cut points for the measurement band, over countries that recorded
# at least one incident in the window. Deliberately coarse: the underlying
# counts carry real uncertainty and finer bands would imply precision the
# evidence does not support.
BANDS = [
    (0.90, "MEASURED_TOP_DECILE"),
    (0.75, "MEASURED_UPPER_QUARTILE"),
    (0.50, "MEASURED_UPPER_HALF"),
    (0.00, "MEASURED_LOWER_HALF"),
]

# Relative severity of a physical incident by category. Mirrors
# danger_scoring_config so the committee can retune in one place.
DEFAULT_WEIGHTS = {
    "ARMED_CONFLICT": 1.0, "AIR_MISSILE_DRONE": 1.0, "TERRORISM_IED": 1.0,
    "VIOLENCE_AGAINST_CIVILIANS": 0.9, "KIDNAPPING": 0.8, "VIOLENT_UNREST": 0.5,
    "ARMED_GROUPS": 0.6, "FOREIGN_DIPLOMATIC_TARGET": 1.0,
    "TRANSPORT_ACCESS_DISRUPTION": 0.7, "STATE_CONTROL_BREAKDOWN": 1.0,
    "OTHER_SERIOUS_THREAT": 0.4,
}
FATALITY_WEIGHT = 0.1          # each reported death adds this much to the score


def load_designations(path: str = DESIGNATIONS_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def severity_score(row: dict, weights: dict[str, float]) -> float:
    """Weighted incident count for the 30-day window, plus a fatality term.

    A bare incident count treats a looting report and an airstrike alike. This
    does not, and the weights are visible rather than buried in code.
    """
    s = sum(weights.get(cat, 0.4) * n for cat, n in (row.get("categories") or {}).items())
    return s + FATALITY_WEIGHT * (row.get("d30_fatalities") or 0)


def _band(pct: float) -> str:
    for cut, name in BANDS:
        if pct >= cut:
            return name
    return BANDS[-1][1]


def apply(rows: list[dict], weights: dict[str, float] | None = None,
          overrides: dict[str, str] | None = None,
          designations: dict | None = None) -> dict[str, Any]:
    """Attach measurement, designation and divergence to each country row.

    Only rows scoped COUNTRY are ranked. Water bodies and regions carry
    incidents but cannot hold a danger-pay designation, so ranking them against
    countries would be a category error.
    """
    weights = weights or DEFAULT_WEIGHTS
    overrides = overrides or {}
    data = designations or load_designations()
    dssr = data.get("dssr650_designations", {})
    icsc = (data.get("sources", {}).get("icsc", {}) or {}).get("designations", {}) or {}

    ranked = [r for r in rows if (r.get("geo_scope") or "COUNTRY") == "COUNTRY"]
    for r in ranked:
        r["severity_score"] = round(severity_score(r, weights), 2)

    # Percentile over countries with any activity. Countries with none are not
    # "safest by measurement" — they are simply unmeasured, and are labelled so.
    active = sorted((r["severity_score"] for r in ranked if r["severity_score"] > 0))
    n = len(active)

    tally = {"designated": 0, "measured_high_undesignated": 0,
             "designated_quiet": 0, "overridden": 0}

    for r in rows:
        scope = r.get("geo_scope") or "COUNTRY"
        country = r.get("country")
        sc = r.get("severity_score", 0.0)

        if scope != "COUNTRY":
            r["measurement"] = {"band": "NOT_RANKED", "percentile": None,
                                "reason": "not a country; cannot hold a designation"}
        elif n == 0 or sc <= 0:
            r["measurement"] = {"band": "NO_RECORDED_INCIDENTS", "percentile": None,
                                "reason": "no physical incidents recorded in this window; "
                                          "this is an absence of records, not a finding of safety"}
        else:
            below = sum(1 for v in active if v < sc)
            pct = below / n
            r["measurement"] = {"band": _band(pct), "percentile": round(pct * 100),
                                "score": sc, "ranked_against": n}

        d = dssr.get(country)
        u = icsc.get(country)
        if d or u:
            r["designation"] = {
                "designated": True,
                "dssr650": d, "icsc": u,
                "authority": " / ".join(x for x in (
                    "US DSSR 650" if d else "", "UN ICSC" if u else "") if x),
            }
            tally["designated"] += 1
        else:
            r["designation"] = {"designated": False, "dssr650": None, "icsc": None,
                                "authority": None}

        band = r["measurement"]["band"]
        des = r["designation"]["designated"]
        if band == "MEASURED_TOP_DECILE" and not des:
            r["divergence"] = "MEASURED_HIGH_NOT_DESIGNATED"
            tally["measured_high_undesignated"] += 1
        elif des and band in ("MEASURED_LOWER_HALF", "NO_RECORDED_INCIDENTS"):
            r["divergence"] = "DESIGNATED_BUT_QUIET"
            tally["designated_quiet"] += 1
        else:
            r["divergence"] = None

        ov = overrides.get(country)
        if ov:
            r["committee_override"] = ov
            tally["overridden"] += 1
        else:
            r["committee_override"] = None

        # danger_status stays a provenance label, not an invented severity grade.
        r["danger_status"] = (ov or ("DESIGNATED" if des else band))
        r["danger_basis"] = ("committee override" if ov else
                             (f"designated by {r['designation']['authority']}" if des else
                              "measured from stored incidents only; no official designation"))
    return tally


if __name__ == "__main__":
    d = load_designations()
    print(f"DSSR 650 designations: {len(d['dssr650_designations'])} countries")
    print(f"  rates effective    : {d['sources']['dssr650']['rates_effective']}")
    print(f"  ICSC designations  : {len(d['sources']['icsc']['designations'])} "
          f"(linked, not bundled — see redistribution note)")
