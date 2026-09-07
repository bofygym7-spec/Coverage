"""Storage and metrics.

The SQLite store mirrors db/001_schema.sql column-for-column so the same
pipeline runs against Supabase Postgres by swapping the connection. Raw events
are append-and-update only: a row is never deleted, so history is preserved and
every aggregate stays reproducible from stored records.
"""
from __future__ import annotations
import json, sqlite3, math
from datetime import date, timedelta
import json
from collections import defaultdict

from pipeline import geo_scope
from typing import Any, Iterable

DDL = """
CREATE TABLE IF NOT EXISTS raw_events (
  gdelt_event_id TEXT PRIMARY KEY,
  event_date TEXT NOT NULL, family TEXT, native_category TEXT, native_subcategory TEXT,
  country TEXT, region TEXT, continent TEXT, admin1 TEXT, location TEXT,
  latitude REAL, longitude REAL, geo_precision INTEGER, geo_precision_label TEXT,
  title TEXT, summary TEXT, actors_json TEXT, entities_json TEXT,
  fatalities INTEGER, has_fatalities INTEGER, civilian_targeting INTEGER,
  significance REAL, goldstein_scale REAL, confidence REAL, article_count INTEGER,
  gdelt_event_url TEXT, primary_story_url TEXT, raw_payload TEXT,
  first_seen_at TEXT NOT NULL, last_updated_at TEXT NOT NULL, source_run_id INTEGER
);
CREATE TABLE IF NOT EXISTS security_classifications (
  gdelt_event_id TEXT PRIMARY KEY REFERENCES raw_events(gdelt_event_id),
  security_category TEXT, security_subtype TEXT, overlay_categories_json TEXT,
  counts_as_danger INTEGER, impact_realised INTEGER,
  classification_method TEXT, classification_confidence REAL,
  classification_rationale_json TEXT, verification_status TEXT,
  rules_version TEXT, classified_at TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY,
  event_date TEXT, country TEXT, admin1 TEXT, location TEXT,
  latitude REAL, longitude REAL, geo_precision INTEGER, geo_precision_label TEXT,
  security_category TEXT, security_subtype TEXT, overlay_categories_json TEXT,
  native_category TEXT, native_subcategory TEXT, title TEXT, summary TEXT,
  actors_json TEXT, organizations_json TEXT,
  fatalities INTEGER, fatalities_range_json TEXT, fatalities_disputed INTEGER,
  significance REAL, goldstein_scale REAL, confidence REAL, article_count INTEGER,
  impact_realised INTEGER, verification_status TEXT,
  event_count INTEGER, dedup_confidence REAL, dedup_method TEXT, dedup_evidence_json TEXT,
  first_seen_at TEXT, last_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS incident_event_links (
  incident_id TEXT, gdelt_event_id TEXT, PRIMARY KEY (incident_id, gdelt_event_id)
);
CREATE TABLE IF NOT EXISTS incident_sources (
  incident_id TEXT, url TEXT, title TEXT, domain TEXT, via_event_id TEXT,
  PRIMARY KEY (incident_id, url)
);
CREATE TABLE IF NOT EXISTS incident_stories (
  incident_id TEXT, story_url TEXT, PRIMARY KEY (incident_id, story_url)
);
CREATE TABLE IF NOT EXISTS country_daily_metrics (
  country TEXT, metric_date TEXT, security_category TEXT,
  incident_count INTEGER, fatalities INTEGER, event_count INTEGER,
  PRIMARY KEY (country, metric_date, security_category)
);
CREATE TABLE IF NOT EXISTS city_daily_metrics (
  country TEXT, city TEXT, metric_date TEXT, proximity_band TEXT,
  security_category TEXT, incident_count INTEGER, fatalities INTEGER,
  PRIMARY KEY (country, city, metric_date, proximity_band, security_category)
);
CREATE TABLE IF NOT EXISTS refresh_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, status TEXT,
  window_start TEXT, window_end TEXT,
  events_fetched INTEGER, events_inserted INTEGER, events_updated INTEGER,
  incidents_built INTEGER, errors_json TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS danger_scoring_config (
  key TEXT PRIMARY KEY, value_json TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_country_date ON raw_events(country, event_date);
CREATE INDEX IF NOT EXISTS ix_inc_country_date ON incidents(country, event_date);
CREATE INDEX IF NOT EXISTS ix_inc_cat ON incidents(security_category);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(DDL)
    return con


def start_run(con, w_start, w_end) -> int:
    cur = con.execute(
        "INSERT INTO refresh_runs (started_at,status,window_start,window_end) VALUES (?,?,?,?)",
        (_now(), "RUNNING", str(w_start), str(w_end)))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    con.execute(f"UPDATE refresh_runs SET finished_at=?, status=?, {cols} WHERE run_id=?",
                (_now(), kw.pop("status", "OK") if False else "OK", *kw.values(), run_id))
    con.commit()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_events(con, events: Iterable[dict], run_id: int) -> tuple[int, int]:
    """Insert new events; update mutable fields on ones already stored.
    first_seen_at is never overwritten, so provenance survives re-ingestion."""
    ins = upd = 0
    now = _now()
    for e in events:
        g = e.get("geo") or {}
        m = e.get("metrics") or {}
        row = (
            e["id"], e.get("event_date"), e.get("family"), e.get("category"), e.get("subcategory"),
            g.get("country"), g.get("region"), g.get("continent"), g.get("admin1"), g.get("location"),
            g.get("latitude"), g.get("longitude"), g.get("geo_precision"), g.get("geo_precision_label"),
            e.get("title"), e.get("summary"),
            json.dumps(e.get("actors") or [], ensure_ascii=False),
            json.dumps(e.get("entity_refs") or [], ensure_ascii=False),
            e.get("fatalities"), 1 if e.get("has_fatalities") else 0,
            None if e.get("civilian_targeting") is None else int(bool(e.get("civilian_targeting"))),
            m.get("significance"), m.get("goldstein_scale"), m.get("confidence"), m.get("article_count"),
            e.get("url"), e.get("primary_story_url"),
            json.dumps(e, ensure_ascii=False), now, now, run_id,
        )
        exists = con.execute("SELECT 1 FROM raw_events WHERE gdelt_event_id=?", (e["id"],)).fetchone()
        if exists:
            con.execute("""UPDATE raw_events SET event_date=?, native_category=?, native_subcategory=?,
                fatalities=?, significance=?, goldstein_scale=?, confidence=?, article_count=?,
                summary=?, raw_payload=?, last_updated_at=?, source_run_id=? WHERE gdelt_event_id=?""",
                (e.get("event_date"), e.get("category"), e.get("subcategory"), e.get("fatalities"),
                 m.get("significance"), m.get("goldstein_scale"), m.get("confidence"), m.get("article_count"),
                 e.get("summary"), json.dumps(e, ensure_ascii=False), now, run_id, e["id"]))
            upd += 1
        else:
            con.execute(f"INSERT INTO raw_events VALUES ({','.join('?'*len(row))})", row)
            ins += 1
    con.commit()
    return ins, upd


def save_classifications(con, pairs, rules_version: str):
    now = _now()
    for eid, c in pairs:
        con.execute("""INSERT OR REPLACE INTO security_classifications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            eid, c["security_category"], c["security_subtype"],
            json.dumps(c["overlay_categories"]), int(c["counts_as_danger"]), int(c["impact_realised"]),
            c["classification_method"], c["classification_confidence"],
            json.dumps(c["classification_rationale"], ensure_ascii=False),
            c["verification_status"], rules_version, now))
    con.commit()


def save_incidents(con, incidents: list[dict]):
    now = _now()
    for i in incidents:
        prev = con.execute("SELECT first_seen_at FROM incidents WHERE incident_id=?",
                           (i["incident_id"],)).fetchone()
        first_seen = prev["first_seen_at"] if prev else now
        con.execute("""INSERT OR REPLACE INTO incidents VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            i["incident_id"], i["event_date"], i["country"], i["admin1"], i["location"],
            i["latitude"], i["longitude"], i["geo_precision"], i["geo_precision_label"],
            i["security_category"], i["security_subtype"], json.dumps(i["overlay_categories"]),
            i["native_category"], i["native_subcategory"], i["title"], i["summary"],
            json.dumps(i["actors"], ensure_ascii=False), json.dumps(i["organizations"], ensure_ascii=False),
            i["fatalities"], json.dumps(i["fatalities_reported_range"]), int(i["fatalities_disputed"]),
            i["significance"], i["goldstein_scale"], i["confidence"], i["article_count"],
            int(i["impact_realised"]), i["verification_status"],
            i["event_count"], i["dedup_confidence"], i["dedup_method"],
            json.dumps(i["dedup_evidence"], ensure_ascii=False), first_seen, now))
        for eid in i["event_ids"]:
            con.execute("INSERT OR IGNORE INTO incident_event_links VALUES (?,?)", (i["incident_id"], eid))
        for s in i["sources"]:
            con.execute("INSERT OR IGNORE INTO incident_sources VALUES (?,?,?,?,?)",
                        (i["incident_id"], s["url"], s["title"], s["domain"], s["via_event_id"]))
        for u in i["story_urls"]:
            con.execute("INSERT OR IGNORE INTO incident_stories VALUES (?,?)", (i["incident_id"], u))
    con.commit()


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def windows(as_of: date) -> dict[str, tuple[date, date]]:
    return {
        "d7":    (as_of - timedelta(days=6),  as_of),
        "d30":   (as_of - timedelta(days=29), as_of),
        "prev30":(as_of - timedelta(days=59), as_of - timedelta(days=30)),
        "d90":   (as_of - timedelta(days=89), as_of),
    }


def _in(d: str | None, lo: date, hi: date) -> bool:
    if not d:
        return False
    try:
        dt = date.fromisoformat(d)
    except ValueError:
        return False
    return lo <= dt <= hi


def rebuild_daily_metrics(con):
    """country_daily_metrics is the auditable bridge between an incident and a
    country total: every row is a plain GROUP BY over stored incidents."""
    con.execute("DELETE FROM country_daily_metrics")
    con.execute("""INSERT INTO country_daily_metrics
        SELECT country, event_date, security_category,
               COUNT(*), COALESCE(SUM(fatalities),0), COALESCE(SUM(event_count),0)
        FROM incidents WHERE country IS NOT NULL
        GROUP BY country, event_date, security_category""")
    con.commit()


def country_rollup(incidents: list[dict], as_of: date, categories: list[str],
                   store_earliest: str | None = None) -> list[dict]:
    """store_earliest is the oldest event date actually ingested. It is what lets
    the rollup tell 'nothing happened' apart from 'we have not fetched that yet'."""
    w = windows(as_of)
    # Group on the canonical name so an ISO code and its spelled-out name, or
    # two casings of the same water body, become one row instead of two.
    by_country: dict[str, list[dict]] = defaultdict(list)
    scope_of: dict[str, str] = {}
    for i in incidents:
        disp, scope = geo_scope.canonical(i.get("country"))
        i["country_display"] = disp
        i["geo_scope"] = scope
        by_country[disp].append(i)
        scope_of[disp] = scope

    out = []
    for country, items in by_country.items():
        row: dict[str, Any] = {"country": country, "geo_scope": scope_of[country],
                               "raw_names": sorted({i["country"] for i in items if i.get("country")})}
        for wk, (lo, hi) in w.items():
            sel = [i for i in items if _in(i["event_date"], lo, hi)]
            row[f"{wk}_incidents"] = len(sel)
            row[f"{wk}_fatalities"] = sum(i["fatalities"] or 0 for i in sel)
            row[f"{wk}_events"] = sum(i["event_count"] for i in sel)
        lo, hi = w["d30"]
        cur = [i for i in items if _in(i["event_date"], lo, hi)]
        row["categories"] = {c: sum(1 for i in cur if i["security_category"] == c) for c in categories}
        row["overlays"] = {}
        for c in categories:
            row["overlays"][c] = sum(1 for i in cur if c in (i["overlay_categories"] or []))
        row["trend"] = trend(row, cur, [i for i in items if _in(i["event_date"], *w["prev30"])],
                             prev_window=w["prev30"], store_earliest=store_earliest)
        row["prev30_covered"] = not row["trend"].get("no_coverage", False)
        dated = sorted((i for i in items if i["event_date"]), key=lambda i: i["event_date"])
        row["latest_event_date"] = dated[-1]["event_date"] if dated else None
        sig = sorted(cur, key=lambda i: (i["fatalities"] or 0, i["significance"] or 0), reverse=True)
        row["last_significant"] = (
            {"incident_id": sig[0]["incident_id"], "title": sig[0]["title"],
             "date": sig[0]["event_date"], "fatalities": sig[0]["fatalities"]} if sig else None)
        row["danger_status"] = "UNRATED"     # calibrated later, see danger_scoring_config
        row["geo_quality"] = _geo_quality(cur)
        out.append(row)
    out.sort(key=lambda r: (-r["d30_incidents"], -r["d30_fatalities"]))
    return out


HIGH_SEVERITY = {"AIR_MISSILE_DRONE", "TERRORISM_IED", "ARMED_CONFLICT",
                 "VIOLENCE_AGAINST_CIVILIANS", "STATE_CONTROL_BREAKDOWN"}


def trend(row: dict, cur: list[dict], prev: list[dict],
          prev_window: tuple[date, date] | None = None,
          store_earliest: str | None = None) -> dict:
    """Composition-weighted, not a bare count ratio: an extra armed clash and an
    extra looting report are not the same escalation.

    A previous window that the store has not ingested is NOT zero activity. If
    the comparison window falls outside what has actually been backfilled, the
    trend is reported as INSUFFICIENT HISTORY rather than manufacturing an
    escalation out of missing data.
    """
    if prev_window and store_earliest:
        p_lo, p_hi = prev_window
        if p_hi.isoformat() < store_earliest:
            return {"label": "INSUFFICIENT HISTORY", "change_pct": None,
                    "basis": f"comparison window {p_lo}..{p_hi} predates ingested data "
                             f"(earliest stored event {store_earliest}); no comparison is possible",
                    "no_coverage": True}
        if p_lo.isoformat() < store_earliest:
            partial = True
        else:
            partial = False
    else:
        partial = False
    def weight(items):
        w = 0.0
        for i in items:
            base = 1.0
            if i["security_category"] in HIGH_SEVERITY:
                base += 0.6
            base += min(2.0, (i["fatalities"] or 0) / 10.0)
            if not i["impact_realised"]:
                base *= 0.4
            w += base
        return w

    wc, wp = weight(cur), weight(prev)
    nc, npv = len(cur), len(prev)
    if npv == 0 and nc == 0:
        return {"label": "NO ACTIVITY", "change_pct": None, "basis": "no incidents in either window"}
    if npv == 0:
        return {"label": "DETERIORATING", "change_pct": None,
                "basis": f"{nc} incidents this period against zero in a fully ingested prior period",
                "partial_coverage": partial}
    pct = round(100.0 * (nc - npv) / npv, 1)
    wpct = 100.0 * (wc - wp) / wp if wp else 0.0
    if wpct >= 75 and nc >= 5:
        label = "SIGNIFICANT ESCALATION"
    elif wpct >= 20:
        label = "DETERIORATING"
    elif wpct <= -20:
        label = "IMPROVING"
    else:
        label = "STABLE"
    return {"label": label, "change_pct": pct, "severity_change_pct": round(wpct, 1),
            "basis": f"severity-weighted {wp:.1f} to {wc:.1f}; counts {npv} to {nc}"}


def _geo_quality(items: list[dict]) -> dict:
    tot = len(items) or 1
    exact = sum(1 for i in items if i["geo_precision"] == 1)
    approx = sum(1 for i in items if i["geo_precision"] == 2)
    country = sum(1 for i in items if i["geo_precision"] == 3 or i["geo_precision"] is None)
    return {"exact": exact, "nearby": approx, "country_only": country,
            "exact_pct": round(100 * exact / tot)}


PROXIMITY_BANDS = [("INSIDE_CITY", 0, 8), ("KM_0_25", 8, 25), ("KM_25_100", 25, 100)]


def city_proximity(incidents: list[dict], city_lat: float, city_lon: float,
                   country: str) -> list[dict]:
    """Classify incidents relative to one city. Incidents whose coordinates are a
    country centroid (geo_precision 3) are never claimed to be near the city;
    they are reported separately as geographically unresolved."""
    from .dedupe import haversine_km
    out = []
    for i in incidents:
        if i["country"] != country:
            continue
        if i["latitude"] is None or i["longitude"] is None or i["geo_precision"] == 3:
            band, dist = "UNRESOLVED_GEOGRAPHY", None
        else:
            dist = haversine_km((city_lat, city_lon), (i["latitude"], i["longitude"]))
            band = "ELSEWHERE_IN_COUNTRY"
            for name, lo, hi in PROXIMITY_BANDS:
                if lo <= dist < hi:
                    band = name
                    break
        out.append({**i, "proximity_band": band,
                    "distance_km": round(dist, 1) if dist is not None else None})
    return out


def seed_scoring_config(con) -> None:
    """Put the tunable policy into the database on first run.

    The spec required scoring to be configurable rather than hard-coded, so the
    weights and the committee override map live here where they can be edited
    without touching Python. Existing rows are never overwritten: a committee
    change must survive the next deploy.
    """
    from pipeline import scoring
    defaults = {
        "category_weights": scoring.DEFAULT_WEIGHTS,
        "committee_overrides": {},
        "measurement_bands": {name: cut for cut, name in scoring.BANDS},
        "fatality_weight": scoring.FATALITY_WEIGHT,
    }
    for k, v in defaults.items():
        con.execute(
            "INSERT INTO danger_scoring_config(key, value_json, updated_at) "
            "VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
            (k, json.dumps(v, ensure_ascii=False), _now()))
    con.commit()
