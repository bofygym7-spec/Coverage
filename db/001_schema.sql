-- Global Security & Danger Dashboard — canonical schema (PostgreSQL + PostGIS)
-- Apply in Supabase: SQL Editor -> paste -> Run. Idempotent.
-- The SQLite store in pipeline/core.py mirrors these columns, so the same
-- pipeline runs against either backend.

CREATE EXTENSION IF NOT EXISTS postgis;

-- Every refresh is recorded, including failures, so the dashboard can state
-- honestly when it last succeeded.
CREATE TABLE IF NOT EXISTS refresh_runs (
  run_id            BIGSERIAL PRIMARY KEY,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  status            TEXT NOT NULL DEFAULT 'RUNNING'
                    CHECK (status IN ('RUNNING','OK','PARTIAL','FAILED')),
  window_start      DATE, window_end DATE,
  events_fetched    INT DEFAULT 0, events_inserted INT DEFAULT 0,
  events_updated    INT DEFAULT 0, incidents_built INT DEFAULT 0,
  errors            JSONB DEFAULT '[]'::jsonb,
  notes             TEXT
);

-- Immutable-by-policy record of what GDELT returned. Never deleted.
CREATE TABLE IF NOT EXISTS raw_events (
  gdelt_event_id      TEXT PRIMARY KEY,
  event_date          DATE NOT NULL,
  family              TEXT,
  native_category     TEXT,
  native_subcategory  TEXT,
  country             TEXT, region TEXT, continent TEXT, admin1 TEXT, location TEXT,
  latitude            DOUBLE PRECISION, longitude DOUBLE PRECISION,
  geom                GEOGRAPHY(Point,4326),
  geo_precision       SMALLINT,          -- 1 exact place, 2 nearby area, 3 centroid
  geo_precision_label TEXT,
  title               TEXT, summary TEXT,
  actors              JSONB DEFAULT '[]'::jsonb,
  entities            JSONB DEFAULT '[]'::jsonb,
  fatalities          INT, has_fatalities BOOLEAN, civilian_targeting BOOLEAN,
  significance        REAL, goldstein_scale REAL, confidence REAL, article_count INT,
  gdelt_event_url     TEXT, primary_story_url TEXT,
  raw_payload         JSONB NOT NULL,
  first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_run_id       BIGINT REFERENCES refresh_runs(run_id)
);
CREATE INDEX IF NOT EXISTS ix_raw_country_date ON raw_events (country, event_date DESC);
CREATE INDEX IF NOT EXISTS ix_raw_geom ON raw_events USING GIST (geom);
CREATE INDEX IF NOT EXISTS ix_raw_date ON raw_events (event_date DESC);

-- Editable policy layer. Kept separate so a rules change never mutates raw data.
CREATE TABLE IF NOT EXISTS classification_rules (
  rules_version TEXT PRIMARY KEY,
  rules         JSONB NOT NULL,
  active        BOOLEAN NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS security_classifications (
  gdelt_event_id            TEXT PRIMARY KEY REFERENCES raw_events(gdelt_event_id),
  security_category         TEXT,
  security_subtype          TEXT,
  overlay_categories        TEXT[] DEFAULT '{}',
  counts_as_danger          BOOLEAN NOT NULL DEFAULT false,
  impact_realised           BOOLEAN NOT NULL DEFAULT true,
  classification_method     TEXT CHECK (classification_method IN
                              ('deterministic','deterministic_excluded','ai_assisted','manual')),
  classification_confidence REAL,
  classification_rationale  JSONB DEFAULT '[]'::jsonb,
  verification_status       TEXT,
  rules_version             TEXT,
  classified_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cls_cat ON security_classifications (security_category)
  WHERE counts_as_danger;

-- Consolidated physical incidents. Derived, rebuildable, never authoritative
-- over raw_events.
CREATE TABLE IF NOT EXISTS incidents (
  incident_id         TEXT PRIMARY KEY,
  event_date          DATE NOT NULL,
  country             TEXT, region TEXT, continent TEXT, admin1 TEXT, location TEXT,
  latitude            DOUBLE PRECISION, longitude DOUBLE PRECISION,
  geom                GEOGRAPHY(Point,4326),
  geo_precision       SMALLINT, geo_precision_label TEXT,
  security_category   TEXT NOT NULL, security_subtype TEXT,
  overlay_categories  TEXT[] DEFAULT '{}',
  merged_categories   TEXT[] DEFAULT '{}', category_disputed BOOLEAN DEFAULT false,
  native_category     TEXT, native_subcategory TEXT,
  title               TEXT, summary TEXT,
  actors              JSONB DEFAULT '[]'::jsonb,
  organizations       JSONB DEFAULT '[]'::jsonb,
  fatalities          INT,
  fatalities_range    INT[], fatalities_disputed BOOLEAN DEFAULT false,
  significance        REAL, goldstein_scale REAL, confidence REAL, article_count INT,
  impact_realised     BOOLEAN DEFAULT true,
  verification_status TEXT,
  event_count         INT NOT NULL DEFAULT 1,
  dedup_confidence    REAL, dedup_method TEXT, dedup_evidence JSONB DEFAULT '[]'::jsonb,
  first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_inc_country_date ON incidents (country, event_date DESC);
CREATE INDEX IF NOT EXISTS ix_inc_cat_date ON incidents (security_category, event_date DESC);
CREATE INDEX IF NOT EXISTS ix_inc_geom ON incidents USING GIST (geom);

-- The audit edges. These three tables are what make a count clickable.
CREATE TABLE IF NOT EXISTS incident_event_links (
  incident_id    TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  gdelt_event_id TEXT NOT NULL REFERENCES raw_events(gdelt_event_id),
  PRIMARY KEY (incident_id, gdelt_event_id)
);
CREATE TABLE IF NOT EXISTS incident_sources (
  incident_id  TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  url          TEXT NOT NULL, title TEXT, domain TEXT, via_event_id TEXT,
  PRIMARY KEY (incident_id, url)
);
CREATE TABLE IF NOT EXISTS incident_stories (
  incident_id TEXT NOT NULL REFERENCES incidents(incident_id) ON DELETE CASCADE,
  story_url   TEXT NOT NULL,
  PRIMARY KEY (incident_id, story_url)
);

CREATE TABLE IF NOT EXISTS country_daily_metrics (
  country TEXT NOT NULL, metric_date DATE NOT NULL, security_category TEXT NOT NULL,
  incident_count INT NOT NULL DEFAULT 0, fatalities INT NOT NULL DEFAULT 0,
  event_count INT NOT NULL DEFAULT 0,
  PRIMARY KEY (country, metric_date, security_category)
);
CREATE TABLE IF NOT EXISTS city_daily_metrics (
  country TEXT NOT NULL, city TEXT NOT NULL, metric_date DATE NOT NULL,
  proximity_band TEXT NOT NULL CHECK (proximity_band IN
    ('INSIDE_CITY','KM_0_25','KM_25_100','ELSEWHERE_IN_COUNTRY','UNRESOLVED_GEOGRAPHY')),
  security_category TEXT NOT NULL,
  incident_count INT NOT NULL DEFAULT 0, fatalities INT NOT NULL DEFAULT 0,
  PRIMARY KEY (country, city, metric_date, proximity_band, security_category)
);

-- Scoring policy lives in data, not in application code, and starts UNRATED.
CREATE TABLE IF NOT EXISTS danger_scoring_config (
  key TEXT PRIMARY KEY, value JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), note TEXT
);
INSERT INTO danger_scoring_config (key, value, note) VALUES
 ('status', '{"mode":"UNRATED","reason":"Thresholds are deliberately uncalibrated until the empirical event base is judged sufficient."}'::jsonb,
  'Version 1 displays UNRATED rather than inventing bands.'),
 ('category_weights',
  '{"ARMED_CONFLICT":1.0,"AIR_MISSILE_DRONE":1.0,"TERRORISM_IED":1.0,
    "VIOLENCE_AGAINST_CIVILIANS":0.9,"KIDNAPPING":0.8,"VIOLENT_UNREST":0.5,
    "ARMED_GROUPS":0.6,"FOREIGN_DIPLOMATIC_TARGET":1.0,
    "TRANSPORT_ACCESS_DISRUPTION":0.7,"STATE_CONTROL_BREAKDOWN":1.0,
    "OTHER_SERIOUS_THREAT":0.4}'::jsonb,
  'Relative weights only. Peaceful political activity carries no weight because it never enters the danger set.'),
 ('modifiers',
  '{"fatality_weight":0.1,"unrealised_impact_multiplier":0.4,
    "recency_halflife_days":21,"acceleration_weight":0.25,
    "proximity_km_full_weight":25,"proximity_km_zero_weight":150}'::jsonb,
  'Tune here, never in application code.')
ON CONFLICT (key) DO NOTHING;

-- Auditability helper: one row per country/category/date naming the incidents
-- behind the number.
CREATE OR REPLACE VIEW v_country_category_audit AS
SELECT i.country, i.security_category, i.event_date,
       count(*) AS incident_count,
       coalesce(sum(i.fatalities),0) AS fatalities,
       array_agg(i.incident_id ORDER BY i.event_date DESC) AS incident_ids
FROM incidents i GROUP BY i.country, i.security_category, i.event_date;
