#!/usr/bin/env python3
"""Classify what GDELT's `geo.country` value actually refers to.

The field is not a country list. In a full global backfill it also carries
aggregate codes (WLD, UNK), continent and region names (EUR, OCE, Middle East,
Caribbean), water bodies (Red Sea, International Waters), and bare ISO-3 codes
where the name failed to resolve. Listing all of these as rows in a country
table inflates the country count and puts "Arctic Ocean" next to Nigeria.

Nothing is deleted. Every incident keeps the country string GDELT gave it; this
module only adds a scope label so the country view can show countries and the
rest stays reachable under its own heading. Deleting would breach the rule that
raw records are never destroyed, and would also hide real incidents — an attack
in international waters happened somewhere, even if not in a country.
"""
from __future__ import annotations

# ISO-3 codes GDELT sometimes emits instead of a name. Mapping them back merges
# the code row into the named row rather than leaving two entries for one place.
ISO3_TO_NAME = {
    "TUV": "Tuvalu", "PCN": "Pitcairn Islands", "MNP": "Northern Mariana Islands",
    "KOS": "Kosovo", "IOT": "British Indian Ocean Territory", "VAT": "Vatican City",
    "SGS": "South Georgia", "ATA": "Antarctica", "BVT": "Bouvet Island",
    "HMD": "Heard and McDonald Islands", "UMI": "U.S. Minor Outlying Islands",
    "TKL": "Tokelau", "NIU": "Niue", "COK": "Cook Islands", "WLF": "Wallis and Futuna",
    "SPM": "Saint Pierre and Miquelon", "MSR": "Montserrat", "AIA": "Anguilla",
    "VGB": "British Virgin Islands", "TCA": "Turks and Caicos Islands",
    "FLK": "Falkland Islands", "SHN": "Saint Helena", "MAF": "Saint Martin",
    "BLM": "Saint Barthelemy", "ALA": "Aland Islands", "FRO": "Faroe Islands",
    "GRL": "Greenland", "SJM": "Svalbard",
}

# Aggregates and unknowns. These are not places at all.
AGGREGATE = {"WLD", "UNK", "UNKNOWN", "WORLD", "N/A", "NONE", "-", ""}

# Continent and region names that appear in the country field.
REGION_CODE = {"EUR": "Europe", "OCE": "Oceania", "PAC": "Pacific",
               "AME": "Americas", "AFR": "Africa", "ASI": "Asia", "MEA": "Middle East"}
REGION = {
    "europe", "eur", "asia", "africa", "oceania", "oce", "pacific", "pac",
    "americas", "north america", "south america", "central america",
    "middle east", "caribbean", "eastern europe", "western europe",
    "northern africa", "western africa", "eastern africa", "southern africa",
    "middle africa", "south asia", "east asia", "southeast asia", "central asia",
    "latin america", "sub-saharan africa", "balkans", "scandinavia",
}

# Seas, oceans and straits. Real locations, but not jurisdictions, so they
# cannot carry a hardship or danger designation for a posting.
WATERS_EXACT = {
    "international waters", "high seas", "red sea", "arctic ocean",
    "atlantic ocean", "pacific ocean", "indian ocean", "southern ocean",
    "mediterranean sea", "black sea", "caspian sea", "baltic sea", "north sea",
    "south china sea", "east china sea", "sea of japan", "arabian sea",
    "persian gulf", "arabian gulf", "gulf of aden", "gulf of oman",
    "gulf of guinea", "gulf of mexico", "caribbean sea", "bay of bengal",
    "strait of hormuz", "bab al-mandeb", "suez canal", "gulf of thailand",
    "andaman sea", "coral sea", "barents sea", "bering sea", "aegean sea",
}
WATERS_TOKENS = (" sea", " ocean", " gulf", " strait", " channel", " bay",
                 "waters", " canal")

SCOPE_COUNTRY = "COUNTRY"
SCOPE_WATERS = "WATERS"
SCOPE_REGION = "REGION"
SCOPE_UNRESOLVED = "UNRESOLVED"


_SMALL = {"of", "the", "and", "de", "al", "el", "da"}


def _titlecase(name: str) -> str:
    """Title-case that leaves connectives lowercase: 'Strait of Hormuz'."""
    parts = name.split()
    return " ".join(p.lower() if i and p.lower() in _SMALL else p.capitalize()
                    for i, p in enumerate(parts))


def canonical(raw: str | None) -> tuple[str, str]:
    """Return (display_name, scope) for a raw GDELT country value.

    Case is normalised first, which is what merges the duplicate pair
    "International Waters" / "International waters" into a single row.
    """
    if raw is None:
        return "Unresolved", SCOPE_UNRESOLVED
    name = " ".join(str(raw).split())          # collapse whitespace
    if not name:
        return "Unresolved", SCOPE_UNRESOLVED

    upper, lower = name.upper(), name.lower()

    if upper in AGGREGATE:
        return "Unresolved", SCOPE_UNRESOLVED

    if upper in ISO3_TO_NAME:
        return ISO3_TO_NAME[upper], SCOPE_COUNTRY

    if upper in REGION_CODE:
        return REGION_CODE[upper], SCOPE_REGION

    if lower in REGION:
        return _titlecase(name), SCOPE_REGION

    if lower in WATERS_EXACT or any(t in lower for t in WATERS_TOKENS):
        # Title-cased so the two casings of "international waters" collapse.
        return _titlecase(name), SCOPE_WATERS

    # A bare 3-letter uppercase token that matched nothing above is an
    # unresolved code. It is NOT guessed at: "CAR" could be Central African
    # Republic or Caribbean, and picking one would fabricate a location.
    if len(name) == 3 and name.isupper() and name.isalpha():
        return name, SCOPE_UNRESOLVED

    return name, SCOPE_COUNTRY


def annotate(incidents: list[dict]) -> dict[str, int]:
    """Attach country_display and geo_scope to each incident. Returns a tally."""
    tally: dict[str, int] = {}
    for i in incidents:
        disp, scope = canonical(i.get("country"))
        i["country_display"] = disp
        i["geo_scope"] = scope
        tally[scope] = tally.get(scope, 0) + 1
    return tally


if __name__ == "__main__":
    probes = ["Nigeria", "WLD", "UNK", "EUR", "OCE", "PAC", "CAR", "KOS", "TUV",
              "MNP", "IOT", "PCN", "PCT", "Middle East", "Caribbean", "Red Sea",
              "Arctic Ocean", "International Waters", "International waters",
              "Strait of Hormuz", "U.S. Virgin Islands", "Sint Maarten",
              "DR Congo", "Guam", "Gibraltar", "Jersey", "Bermuda", "Curacao",
              "New Caledonia"]
    w = max(len(p) for p in probes)
    for p in probes:
        d, s = canonical(p)
        flag = "" if d == p else f"  -> {d}"
        print(f"  {p:<{w}}  {s:<10}{flag}")
