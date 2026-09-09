#!/usr/bin/env python3
"""Extract injury counts from the coded summary.

The source codes fatalities as a structured field but says nothing about
injuries; wounded figures appear only inside the free-text summary. This reads
them out, and is careful to stay distinguishable from coded data:

  * Every number keeps the exact phrase it came from, so any figure on screen
    can be checked against the sentence that produced it.
  * A summary that does not mention injuries yields None, never 0. Silence is
    not a finding of no injuries.
  * "No injuries reported" yields 0 with a qualifier, because that IS a finding.
  * Where a summary gives conflicting figures the highest is kept and the record
    is flagged disputed, with every phrase retained — the same treatment merged
    fatality figures already get.
  * Anything the patterns cannot parse confidently is left unset rather than
    guessed at.

This is derived data. It is labelled as such wherever it is displayed, and it is
never mixed into the fatality field.
"""
from __future__ import annotations

import re

# Words the coder actually uses for injury, seen across the corpus.
INJ = r"(?:wounded|injured|injuries|hurt|casualties\s+wounded)"

WORD_NUM = {
    "no": 0, "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "hundred": 100,
}
# A bare number must not be read when a multiplier follows it: "two dozen" is
# 24, not 2. Multipliers are handled by their own rule below.
MULT = {"dozen": 12, "dozens": None, "score": 20, "scores": None,
        "hundred": 100, "hundreds": None, "thousand": 1000, "thousands": None}
_MULT_RE = "|".join(MULT)
NUM = (r"(?:\d[\d,]*(?:\s*[-–]\s*\d[\d,]*)?|" + "|".join(WORD_NUM) + r")"
       rf"(?!\s+(?:{_MULT_RE})\b)")
# "two dozen", "three hundred" — an exact multiple, still approximate in tone.
NUM_MULT = (r"(?:\d[\d,]*|" + "|".join(WORD_NUM) + rf")\s+(?:{_MULT_RE})")

# Hedges the source uses. Kept so a figure is never presented as more precise
# than the sentence it came from.
QUAL = r"(?:at\s+least|more\s+than|over|around|about|approximately|some|nearly|up\s+to)"

# Words that end the clause a number belongs to. Without this the gap between a
# number and an injury word could swallow a conjunction and bind the death count
# to the injury word: "killed at least 14 Houthi members and wounded 19" was
# read as 14 injured, when 14 is the number killed and 19 the number injured.
BREAK = r"and|but|while|though|;|,|killed|kill|dead|died|die|deaths?|fatalities|fatal"
GAP = rf"(?:(?!\b(?:{BREAK})\b)\w+\s+){{0,4}}?"

# number then injury word: "seven wounded", "67-68 wounded", "at least 12 injured",
# "seven people were wounded", "four residents were injured"
PAT_NUM_FIRST = re.compile(
    rf"(?P<qual>{QUAL})?\s*(?P<num>{NUM_MULT}|{NUM})\s+{GAP}(?P<inj>{INJ})", re.I)
# injury word then number, both participle and gerund forms:
# "injuring four residents", "wounding 12 people", "wounded 19", "injured 15 others"
PAT_INJ_FIRST = re.compile(
    rf"(?:wounding|injuring|wounded|injured|hurt)\s+(?P<qual>{QUAL})?\s*(?P<num>{NUM_MULT}|{NUM})\b", re.I)
# explicit absence — a real finding, unlike silence
PAT_NONE = re.compile(
    rf"(?:no|without)\s+(?:\w+\s+){{0,2}}?(?:{INJ})|"
    rf"(?:{INJ})\s*:?\s*(?:none|zero)\b|no\s+one\s+was\s+(?:{INJ})", re.I)

# Guards against reading a number that belongs to a different fact.
NOT_INJURY = re.compile(r"\b(?:killed|dead|deaths?|fatalities|died)\b", re.I)


def _to_int(tok: str) -> int | None:
    tok = tok.strip().lower().replace(",", "")
    m = re.match(rf"^(\S+)\s+({_MULT_RE})$", tok)
    if m:
        base, mult = m.group(1), MULT[m.group(2)]
        # "dozens" / "scores" with no count is a quantity the source did not
        # give. Left unparsed rather than invented.
        if mult is None:
            return None
        n = int(base) if base.isdigit() else WORD_NUM.get(base)
        return n * mult if n is not None else None
    if re.match(r"^\d+\s*[-–]\s*\d+$", tok):          # "67-68" -> upper bound
        return max(int(x) for x in re.split(r"[-–]", tok))
    if tok.isdigit():
        return int(tok)
    return WORD_NUM.get(tok)


def _phrase(text: str, m: re.Match, pad: int = 34) -> str:
    a = max(0, m.start() - pad)
    b = min(len(text), m.end() + pad)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def extract(summary: str | None) -> dict:
    """Return {injuries, injuries_phrases, injuries_qualifier, injuries_disputed}.

    injuries is None when the summary says nothing about injuries.
    """
    out = {"injuries": None, "injuries_phrases": [], "injuries_qualifier": None,
           "injuries_disputed": False, "injuries_source": "summary_text"}
    if not summary:
        return out
    text = " ".join(str(summary).split())

    found: list[tuple[int, str, str | None]] = []
    for pat in (PAT_NUM_FIRST, PAT_INJ_FIRST):
        for m in pat.finditer(text):
            n = _to_int(m.group("num"))
            if n is None:
                continue
            span = m.group(0)
            # "two killed and seven wounded" — reject a match whose own span has
            # swallowed the death clause, which would attach the wrong number.
            if pat is PAT_NUM_FIRST and NOT_INJURY.search(span):
                continue
            found.append((n, _phrase(text, m), (m.group("qual") or "").strip().lower() or None))

    if not found:
        if PAT_NONE.search(text):
            out.update(injuries=0, injuries_qualifier="reported_none",
                       injuries_phrases=[_phrase(text, PAT_NONE.search(text))])
        return out

    nums = {n for n, _, _ in found}
    out["injuries"] = max(nums)
    out["injuries_phrases"] = [p for _, p, _ in found]
    out["injuries_disputed"] = len(nums) > 1
    quals = [q for _, _, q in found if q]
    if quals:
        out["injuries_qualifier"] = quals[0]
    return out


def annotate(incidents: list[dict]) -> dict[str, int]:
    tally = {"with_figure": 0, "reported_none": 0, "not_mentioned": 0, "disputed": 0}
    for i in incidents:
        r = extract(i.get("summary"))
        i.update(r)
        if r["injuries"] is None:
            tally["not_mentioned"] += 1
        elif r["injuries_qualifier"] == "reported_none":
            tally["reported_none"] += 1
        else:
            tally["with_figure"] += 1
        if r["injuries_disputed"]:
            tally["disputed"] += 1
    return tally
