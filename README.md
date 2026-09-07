# Global Security & Danger Dashboard

A daily-refreshing, fully auditable view of physical-security incidents worldwide,
built on GDELT Cloud coded events.

Every number on the dashboard is clickable and opens the exact incidents that
produced it. From there you reach the underlying GDELT event records, the GDELT
Cloud story pages, and the original source articles. There is no figure anywhere
in the system that cannot be traced back to stored records.

```
WORLD → COUNTRY → SECURITY CATEGORY → INCIDENT → GDELT EVENT → SOURCE ARTICLE
```

---

## What is already working

Open `site/dashboard.html` in a browser. It is a single self-contained file
containing real GDELT data pulled on 2026-09-07, and needs no server, no
install and no account.

The bundled sample holds 70 real GDELT event records covering the Middle East
and Europe over the last 30 days, consolidated into 62 physical incidents with
121 source-article links. The scheduled pipeline replaces this with full global
coverage on its first run.

---

## The three things you have to do yourself

Everything else is automated. These need your account, so they cannot be done
for you.

### 1. Get a GDELT Cloud API key

Sign in at <https://gdeltcloud.com>, open **API keys**, create one. It looks
like `gdelt_sk_...`.

Do not paste it into any file in this project. It goes in one place only, below.

### 2. Put the project on GitHub and add the key as a secret

Create a new **private** repository and upload this folder to it.

Then in that repository: **Settings → Secrets and variables → Actions → New
repository secret**

- Name: `GDELT_API_KEY`
- Secret: your `gdelt_sk_...` key

GitHub encrypts it. It is passed to the pipeline as an environment variable at
run time, is never written to the database, never appears in the published site,
and is never printed in logs.

### 3. Turn on Pages and run it once

- **Settings → Pages → Source → GitHub Actions**
- **Actions → Daily security refresh → Run workflow**, tick **backfill**, run it.

The first run backfills from 2026-03-01 (the start of GDELT coded coverage) and
publishes the dashboard. After that it refreshes itself every day at 05:20 UTC.

---

## Running it on your own machine instead

```bash
pip install -r requirements.txt

python -m pipeline.run_pipeline --seed        # replay the bundled sample, no key needed
python pipeline/build_site.py                 # produces site/dashboard.html

export GDELT_API_KEY=gdelt_sk_...             # live modes only
python -m pipeline.run_pipeline --backfill    # full history
python -m pipeline.run_pipeline --days 3      # daily increment
```

---

## How it decides what counts as danger

The purpose is physical security, so article volume is never treated as danger.
Peaceful protest, speeches, elections, arrests, diplomatic disagreement and
negative coverage are ingested and stored but never counted.

Classification runs in four gates, all defined in
`pipeline/classification_rules.json` so the policy can be changed without
touching code:

1. **Relevance.** Only `Battles`, `Explosions/Remote violence`, `Violence against
   civilians` and `Riots` pass wholesale. `Strategic developments` is a mixed
   bucket, so only its physically meaningful subtypes pass and administrative
   ones such as `Arrests` and `Agreement` are excluded.
2. **Mapping.** The GDELT native category and subcategory map onto the eleven
   security categories. The native values are always kept alongside.
3. **Overlays.** Keyword flags for diplomatic targeting, transport disruption,
   coup, kidnapping, armed groups and terrorism. Three of these can *promote* an
   event, because an attack on an embassy is more operationally meaningful than
   "an explosion".
4. **Verification.** Confidence and corroborating-source count decide between
   `VERIFIED_MULTI_SOURCE`, `VERIFIED_SINGLE_SOURCE` and
   `INSUFFICIENT_EVENT_LEVEL_VERIFICATION`. Nothing is upgraded on article count.

Promoting overlays read only the title, summary and named actors — never the
linked entities. GDELT documents entity links as *coverage*, warning that a
mention does not make an entity a party to the event. Without that restriction a
US strike on a wedding in Iran was being promoted to "attack on an international
organisation" purely because the Iranian Red Crescent was quoted as a responder.

## How duplicates are handled

Raw events are never modified or deleted. Consolidation writes a *separate*
incident record that links back to every contributing event, story URL and
source article, with a stored confidence and the reason for the merge.

Candidates are compared only within the same country, within two days, and
within 75 km. Scoring combines geography, time, text, actors, subtype, category
compatibility and casualty agreement.

Three details matter more than the weights:

- **A centroid is not a location.** When a record is coded at country or region
  precision, matching coordinates prove only "same country", so the geographic
  contribution is capped and the merge must be carried by other evidence.
  Without this, two unrelated Kyiv-region events sharing a centroid scored as
  duplicates.
- **Category is not a blocker.** The commonest real duplicate is one strike coded
  once as `Explosions/Remote violence` and once as `Violence against civilians`.
  Blocking on category would make those permanently invisible to each other.
  Where merged records disagree, the incident is flagged `category_disputed`.
- **An identical casualty figure is near-conclusive.** Wire services repeat the
  toll verbatim while headlines vary, which is exactly where text similarity is
  weakest, so an exact non-trivial match within a day adds a corroboration bonus.

Where fatality figures differ across merged records the incident stores the full
reported range and is flagged as disputed rather than silently picking one.

## Missing data is never zero

GDELT coded coverage begins **2026-03-01**. Any earlier period is reported as
having no coverage, never as zero events.

The same rule applies to the trend comparison. If the previous 30-day window has
not been ingested yet, the country shows **insufficient history**, not
"deteriorating". Before the first backfill every country is in this state, which
is correct — a fresh store genuinely cannot tell you a direction of travel.

Geography is treated the same way. Incidents coded at country-centroid precision
are withheld from the map and are shown as *geography unresolved* in the city
proximity bands, rather than being placed near a capital they may be hundreds of
kilometres from. This is the point of city-level analysis: fighting elsewhere in
a large country is not danger at a specific mission.

## Danger scoring

Deliberately **UNRATED** in version 1. The weights and modifiers exist in
`danger_scoring_config` and can be tuned there, but no LOW/MODERATE/HIGH bands
are asserted until the empirical base is judged sufficient. The event record is
the product; an uncalibrated score would only obscure it.

---

## Layout

```
db/001_schema.sql              Postgres + PostGIS schema (Supabase-ready)
pipeline/
  classification_rules.json    The danger policy, as data
  classify.py                  Four-gate deterministic classifier
  dedupe.py                    Incident consolidation
  gdelt_client.py              REST client: 30-day window splitting, cursor
                               paging, correct 429 handling
  core.py                      Store + rolling windows, trends, proximity
  run_pipeline.py              Daily entrypoint
  build_site.py                Emits the standalone dashboard
site/index.html                Dashboard (loads data.json)
site/dashboard.html            Standalone build, data inlined
.github/workflows/             Daily scheduler
```

## Moving to Supabase

The bundled SQLite store handles this volume comfortably and keeps the whole
system to one dependency. If you want Postgres, run `db/001_schema.sql` in the
Supabase SQL editor: the columns match, so only the connection in
`pipeline/core.py` changes. Keep the service-role key server-side; it must never
appear in `site/` or in any variable a browser can read.

## Known limits

- Incidents are consolidated within a country. The same Strait of Hormuz tanker
  attack coded once under Oman, once under Iran and once under Saudi Arabia stays
  as three records, because merging across countries would corrupt per-country
  counts. They appear adjacent in date order for an analyst to judge.
- The GDELT `country` filter matches location *or* actor origin, so a wide query
  returns some events that merely involve a country's forces. Bucketing here is
  by event location, taken from `geo.country`.
- Keyword overlays are English-only. Source articles are multilingual, but GDELT
  titles and summaries are coded in English, which is what the overlays read.
