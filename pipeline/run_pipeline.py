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
    if args.backfill:
        w_start, w_end = COVERAGE_START, as_of
    else:
        w_start, w_end = as_of - timedelta(days=args.days - 1), as_of

    con = core.connect(DB_PATH)
    run_id = core.start_run(con, w_start, w_end)
    errors: list[str] = []

    try:
        if args.seed:
            raw = load_seed()
            ins, upd = core.upsert_events(con, raw, run_id)
            fetched = len(raw)
            print(f"[ingest] {fetched} fetched, {ins} new, {upd} updated")
        else:
            chunk = BACKFILL_CHUNK_DAYS if args.backfill else 30
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
            "total_raw_events": con.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"],
            "excluded_non_physical": excluded,
            "coverage_note": (
                "GDELT Cloud coded event coverage begins 2026-03-01. Periods before that "
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
