#!/usr/bin/env python3
"""Daily entrypoint.

  python -m pipeline.run_pipeline --seed          # replay the bundled sample
  python -m pipeline.run_pipeline --days 3        # incremental daily refresh
  python -m pipeline.run_pipeline --backfill      # full history from 2026-03-01

Live modes require GDELT_API_KEY in the environment. The key is read once, held
in memory, and never written to the database or the emitted site data.
"""
from __future__ import annotations
import argparse, json, os, sys, traceback
from datetime import date, timedelta
from pipeline.gdelt_client import BACKFILL_CHUNK_DAYS
from pipeline import geo_scope, scoring, stories, injuries

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import core, dedupe
from pipeline.classify import classify, CATEGORIES, RULES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "security.db")
SEED_PATH = os.path.join(ROOT, "data", "seed_events.json")
SITE_DATA = os.path.join(ROOT, "site", "data.json")
COVERAGE_START = date(2026, 3, 1)


def fetch_live(con, run_id: int, start: date, end: date,
               chunk_days: int) -> tuple[int, int, int]:
    """Fetch and persist chunk by chunk.

    Each chunk is committed as it arrives rather than buffered to the end, so a
    failure late in a long backfill keeps everything already retrieved. Re-running
    resumes cheaply because upserts are keyed on the GDELT event id.
    """
    from pipeline.gdelt_client import GdeltClient, DANGER_CATEGORIES
    client = GdeltClient()
    total = ins_t = upd_t = 0
    for w_start, w_end in GdeltClient.split_window(start, end, chunk_days):
        rows = list(client.search_events(w_start, w_end, chunk_days=chunk_days,
                                         category=DANGER_CATEGORIES,
                                         include_images=False,
                                         include_entity_images=False))
        ins, upd = core.upsert_events(con, rows, run_id)
        # Stories for the same window. Their linked_events carry exact event ids,
        # which is how they attach to incidents later.
        try:
            srows = stories.fetch_window(client, w_start, w_end, chunk_days,
                                         DANGER_CATEGORIES)
            si, su = stories.upsert_stories(con, srows, core._now())
            print(f"    stories: {len(srows)} ({si} new)", flush=True)
        except Exception as e:
            # Story coverage is an enhancement. If it fails the incident data is
            # still correct, so the run continues with a visible note.
            print(f"    [warn] story walk failed: {e}", flush=True)
        con.commit()
        total, ins_t, upd_t = total + len(rows), ins_t + ins, upd_t + upd
        print(f"  {w_start}..{w_end}: {len(rows):5d} events ({ins} new, {upd} updated)",
              flush=True)
    print(f"[ingest] {total} fetched, {ins_t} new, {upd_t} updated")
    return total, ins_t, upd_t


def load_seed() -> list[dict]:
    with open(SEED_PATH, encoding="utf-8") as fh:
        return json.load(fh)["events"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="replay bundled sample, no API key needed")
    ap.add_argument("--days", type=int, default=3, help="trailing days to refresh")
    ap.add_argument("--backfill", action="store_true", help="full history from coverage start")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD, defaults to today")
    args = ap.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()

    con = core.connect(DB_PATH)

    if args.backfill:
        w_start, w_end = COVERAGE_START, as_of
    else:
        w_start, w_end = as_of - timedelta(days=args.days - 1), as_of
        # The store lives in a best-effort cache and scheduled runs can be missed,
        # so continuity is verified rather than assumed. Whatever is genuinely
        # absent gets fetched, which makes an evicted cache self-healing instead
        # of silently truncating history to the last few days.
        if not args.seed:
            latest = con.execute("SELECT MAX(event_date) m FROM raw_events").fetchone()["m"]
            if latest is None:
                w_start = COVERAGE_START
                print("[gap] store is empty; widening this run to a full backfill")
            else:
                resume = date.fromisoformat(latest) - timedelta(days=1)
                if resume < w_start:
                    print(f"[gap] latest stored event is {latest}; widening window "
                          f"from {w_start} to {resume} to close the gap")
                    w_start = max(resume, COVERAGE_START)

    core.seed_scoring_config(con)
    stories.ensure_tables(con)
    run_id = core.start_run(con, w_start, w_end)
    errors: list[str] = []

    try:
        if args.seed:
            raw = load_seed()
            ins, upd = core.upsert_events(con, raw, run_id)
            fetched = len(raw)
            print(f"[ingest] {fetched} fetched, {ins} new, {upd} updated")
        else:
            span = (w_end - w_start).days + 1
            chunk = BACKFILL_CHUNK_DAYS if span > 30 else 30
            fetched, ins, upd = fetch_live(con, run_id, w_start, w_end, chunk)

        # Classify every stored event, not only this run's, so a rules change is
        # applied retroactively and old decisions can be revised.
        stored = [json.loads(r["raw_payload"]) for r in
                  con.execute("SELECT raw_payload FROM raw_events")]
        pairs = []
        for e in stored:
            c = classify(e)
            e["_cls"] = c
            pairs.append((e["id"], c))
        core.save_classifications(con, pairs, RULES["version"])
        danger = [e for e in stored if e["_cls"]["counts_as_danger"]]
        print(f"[classify] {len(stored)} events, {len(danger)} count as physical danger, "
              f"{len(stored)-len(danger)} excluded as non-physical")

        incidents = dedupe.consolidate(stored)
        merged = sum(1 for i in incidents if i["event_count"] > 1)
        core.save_incidents(con, incidents)
        print(f"[dedupe] {len(danger)} events -> {len(incidents)} incidents "
              f"({merged} consolidated from multiple records)")

        core.rebuild_daily_metrics(con)
        cats = list(CATEGORIES.keys())
        earliest = con.execute('SELECT MIN(event_date) m FROM raw_events').fetchone()['m']
        countries = core.country_rollup(incidents, as_of, cats, store_earliest=earliest)
        print(f"[metrics] {len(countries)} countries rolled up")

        build_site_data(con, incidents, countries, as_of, run_id)
        core.finish_run(con, run_id, events_fetched=fetched, events_inserted=ins,
                        events_updated=upd, incidents_built=len(incidents),
                        errors_json=json.dumps(errors), notes="seed" if args.seed else "live")
        print(f"[done] wrote {SITE_DATA}")
        return 0
    except Exception:
        traceback.print_exc()
        con.execute("UPDATE refresh_runs SET status='FAILED', finished_at=?, errors_json=? "
                    "WHERE run_id=?", (core._now(), json.dumps([traceback.format_exc()]), run_id))
        con.commit()
        return 1


def build_site_data(con, incidents, countries, as_of, run_id):
    dates = sorted(i["event_date"] for i in incidents if i["event_date"])
    runs = [dict(r) for r in con.execute(
        "SELECT * FROM refresh_runs ORDER BY run_id DESC LIMIT 5")]
    excluded = con.execute(
        "SELECT COUNT(*) c FROM security_classifications WHERE counts_as_danger=0").fetchone()["c"]

    # Label every incident with what its country value actually refers to. Done
    # before the payload is assembled so both the tally and the per-incident
    # field are available to the dashboard.
    scope_tally = geo_scope.annotate(incidents)
    # Derived from summary text, not coded by the source. Reparsed on every run,
    # so a rule change applies to the whole store without any refetch.
    inj_tally = injuries.annotate(incidents)
    print(f"[injuries] {inj_tally['with_figure']} with a figure \u00b7 "
          f"{inj_tally['not_mentioned']} not mentioned \u00b7 "
          f"{inj_tally['reported_none']} explicitly none \u00b7 "
          f"{inj_tally['disputed']} disputed")
    n_story_links = stories.attach(con, incidents)
    print(f"[stories] {n_story_links} story links attached to incidents")

    # Measurement, official designation, and their disagreement. Computed after
    # the rollup so percentiles rank exactly the numbers the table displays.
    cfg = con.execute("SELECT value_json AS v FROM danger_scoring_config WHERE key='category_weights'").fetchone()
    weights = json.loads(cfg["v"]) if cfg else None
    ovr = con.execute("SELECT value_json AS v FROM danger_scoring_config WHERE key='committee_overrides'").fetchone()
    overrides = json.loads(ovr["v"]) if ovr else {}
    designations = scoring.load_designations()
    score_tally = scoring.apply(countries, weights, overrides, designations)
    print(f"[score] {score_tally['designated']} designated \u00b7 "
          f"{score_tally['measured_high_undesignated']} measured high, not designated \u00b7 "
          f"{score_tally['designated_quiet']} designated but quiet")

    payload = {
        "meta": {
            "generated_at": core._now(),
            "as_of": as_of.isoformat(),
            "last_refresh": core._now(),
            "latest_event_date": dates[-1] if dates else None,
            "coverage_start": COVERAGE_START.isoformat(),
            "store_earliest_event": dates[0] if dates else None,
            "rules_version": RULES["version"],
            "run_id": run_id,
            "total_incidents": len(incidents),
            "scope_tally": scope_tally,
            "injury_tally": inj_tally,
            "score_tally": score_tally,
            "designation_sources": designations["sources"],
            "total_raw_events": con.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"],
            "excluded_non_physical": excluded,
            "coverage_note": (
                "Coded event coverage from the upstream source begins 2026-03-01. Periods before that "
                "date have NO COVERAGE and are never reported as zero events."),
        },
        "categories": CATEGORIES,
        "countries": countries,
        "incidents": incidents,
        "runs": runs,
    }
    os.makedirs(os.path.dirname(SITE_DATA), exist_ok=True)
    with open(SITE_DATA, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    raise SystemExit(main())
