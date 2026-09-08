#!/usr/bin/env python3
"""Fetch narrative story clusters and link them to stored events.

Why this exists: incidents carried a story URL that pointed off-site. The story
id cannot be recovered from that URL — it publishes only the first 8 hex
characters of a 12-character id (".../…-8660702d" for id "8660702d5bbd") — so
the articles behind a story could not be fetched from what was already stored.

Stories are therefore walked separately over the same window. Each story returns
`linked_events` carrying exact event ids, so stories attach to incidents by id
rather than by matching URLs, and the dashboard can render the article list
itself instead of sending the reader elsewhere.

Quota: one extra paginated walk per window. Stories are far fewer than events,
and the three top articles arrive inline at no extra cost. The full article list
(get_story_articles) is a call per story and is therefore optional and bounded.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS stories (
  story_id        TEXT PRIMARY KEY,
  title           TEXT,
  story_date      TEXT,
  category        TEXT,
  country         TEXT,
  location        TEXT,
  latitude        REAL,
  longitude       REAL,
  significance    REAL,
  article_count   INTEGER,
  entity_refs     TEXT,      -- JSON, coverage only: presence is not participation
  first_seen_at   TEXT,
  last_updated_at TEXT
);
CREATE TABLE IF NOT EXISTS story_event_links (
  story_id       TEXT NOT NULL,
  gdelt_event_id TEXT NOT NULL,
  PRIMARY KEY (story_id, gdelt_event_id)
);
CREATE TABLE IF NOT EXISTS story_articles (
  story_id       TEXT NOT NULL,
  url            TEXT NOT NULL,
  title          TEXT,
  domain         TEXT,
  published_date TEXT,
  is_top         INTEGER DEFAULT 0,
  PRIMARY KEY (story_id, url)
);
CREATE INDEX IF NOT EXISTS ix_sel_event ON story_event_links (gdelt_event_id);
CREATE INDEX IF NOT EXISTS ix_sa_story  ON story_articles (story_id);
"""


def ensure_tables(con) -> None:
    con.executescript(DDL)
    con.commit()


def upsert_stories(con, rows: list[dict], now: str) -> tuple[int, int]:
    """Store stories, their event links and their inline top articles."""
    ins = upd = 0
    for s in rows:
        sid = s.get("id")
        if not sid:
            continue
        geo = s.get("geo") or {}
        m = s.get("metrics") or {}
        exists = con.execute("SELECT 1 FROM stories WHERE story_id=?", (sid,)).fetchone()
        con.execute("""
            INSERT INTO stories(story_id,title,story_date,category,country,location,
                                latitude,longitude,significance,article_count,
                                entity_refs,first_seen_at,last_updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(story_id) DO UPDATE SET
              title=excluded.title, significance=excluded.significance,
              article_count=excluded.article_count, entity_refs=excluded.entity_refs,
              last_updated_at=excluded.last_updated_at
        """, (sid, s.get("title"), s.get("story_date"), s.get("category"),
              geo.get("country"), geo.get("location"),
              geo.get("latitude"), geo.get("longitude"),
              m.get("significance"), m.get("article_count"),
              json.dumps(s.get("entity_refs") or [], ensure_ascii=False), now, now))
        upd += 1 if exists else 0
        ins += 0 if exists else 1

        for ev in (s.get("linked_events") or []):
            if ev.get("id"):
                con.execute("INSERT OR IGNORE INTO story_event_links VALUES(?,?)",
                            (sid, ev["id"]))
        for a in (s.get("top_articles") or []):
            if a.get("url"):
                con.execute("""INSERT INTO story_articles(story_id,url,title,domain,
                                 published_date,is_top) VALUES(?,?,?,?,?,1)
                               ON CONFLICT(story_id,url) DO UPDATE SET
                                 title=excluded.title, is_top=1""",
                            (sid, a["url"], a.get("title"), a.get("domain"),
                             a.get("published_date")))
    con.commit()
    return ins, upd


def attach(con, incidents: list[dict]) -> int:
    """Hang stories and their articles onto each incident, by event id.

    Matching is on exact event ids from the story's own linked_events, not on
    URL similarity, so an incident is only ever joined to a story the source
    itself says the events belong to.
    """
    n = 0
    for inc in incidents:
        ids = inc.get("event_ids") or []
        if not ids:
            inc["stories"] = []
            continue
        marks = ",".join("?" * len(ids))
        rows = con.execute(f"""
            SELECT DISTINCT s.story_id, s.title, s.story_date, s.category,
                   s.significance, s.article_count
            FROM story_event_links l JOIN stories s USING(story_id)
            WHERE l.gdelt_event_id IN ({marks})
            ORDER BY s.significance DESC
        """, ids).fetchall()
        out = []
        for r in rows:
            arts = con.execute("""SELECT url,title,domain,published_date
                                  FROM story_articles WHERE story_id=?
                                  ORDER BY is_top DESC, domain""", (r["story_id"],)).fetchall()
            out.append({
                "story_id": r["story_id"], "title": r["title"],
                "story_date": r["story_date"], "category": r["category"],
                "significance": r["significance"],
                # article_count is what the source reports for the whole cluster;
                # `articles` is what has actually been fetched. Showing both keeps
                # a partial fetch visible instead of implying full coverage.
                "article_count_reported": r["article_count"],
                "articles": [dict(a) for a in arts],
            })
        inc["stories"] = out
        n += len(out)
    return n


def fetch_window(client, start: date, end: date, chunk_days: int,
                 categories: list[str]) -> list[dict]:
    """Walk stories for a window using the same chunking as the event walk."""
    rows: list[dict] = []
    for cat in categories:
        rows.extend(client.search_stories(start, end, chunk_days=chunk_days,
                                          category=cat, related=False,
                                          include_images=False,
                                          include_entity_images=False))
    seen, uniq = set(), []
    for r in rows:
        if r.get("id") and r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    return uniq
