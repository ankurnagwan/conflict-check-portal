"""
matching.py - conflict screening across ALL service lines.

Three signals, merged per existing record:
  1. Identifier match  (same PAN / GSTIN / CIN / Tax ID)             -> 100%, always "likely duplicate"
  2. Entity-name similarity   (rapidfuzz WRatio on normalised names)  -> >= FUZZY_THRESHOLD
  3. Related-party similarity (new entity / new related parties vs. existing entities AND their related parties)

Names are normalised first ("A.B.C. Private Limited" == "ABC Pvt Ltd"), so legal suffixes never drive a score.
This module is deliberately NOT service-line scoped: a conflict check is only meaningful firm-wide.
What a given user may SEE of a match is decided by workflow.partition_matches().
"""
from __future__ import annotations

from typing import Iterable, Optional

from rapidfuzz import fuzz, process
from sqlalchemy import or_, select

import config
from database import ConflictCheck, RelatedParty, ServiceLine, normalize_id, normalize_name

LEVEL_HIGH, LEVEL_POSSIBLE = "LIKELY DUPLICATE", "POSSIBLE MATCH"


def _pool(s, exclude_id: Optional[int]):
    """Every non-deleted entity name and related-party name in the firm, as parallel lists."""
    meta, names = [], []
    q = (select(ConflictCheck.id, ConflictCheck.reference_no, ConflictCheck.party_name, ConflictCheck.normalized_name,
                ServiceLine.id, ServiceLine.code, ConflictCheck.conflict_status)
         .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
         .where(ConflictCheck.is_deleted.is_(False)))
    if exclude_id:
        q = q.where(ConflictCheck.id != exclude_id)
    for cid, ref, pname, norm, slid, slcode, st in s.execute(q):
        if len(norm or "") >= config.FUZZY_MIN_LEN:
            meta.append((cid, ref, pname, slid, slcode, st, "entity", pname))
            names.append(norm)
    rq = (select(ConflictCheck.id, ConflictCheck.reference_no, ConflictCheck.party_name, RelatedParty.name,
                 RelatedParty.normalized_name, ServiceLine.id, ServiceLine.code, ConflictCheck.conflict_status)
          .join(ConflictCheck, ConflictCheck.id == RelatedParty.check_id)
          .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
          .where(ConflictCheck.is_deleted.is_(False)))
    if exclude_id:
        rq = rq.where(ConflictCheck.id != exclude_id)
    for cid, ref, pname, rname, rnorm, slid, slcode, st in s.execute(rq):
        if len(rnorm or "") >= config.FUZZY_MIN_LEN:
            meta.append((cid, ref, pname, slid, slcode, st, "related", rname))
            names.append(rnorm)
    return meta, names


def find_matches(s, *, name: str, related_names: Iterable[str] = (), pan=None, gstin=None, cin=None, tax_id=None,
                 exclude_id: Optional[int] = None, threshold: Optional[int] = None, limit: int = 25) -> list[dict]:
    """Returns one dict per matching existing record, best score first. Call inside any session."""
    threshold = threshold or config.FUZZY_THRESHOLD
    found: dict[int, dict] = {}

    def note(cid, ref, pname, slid, slcode, status, score, reason):
        m = found.setdefault(cid, {"check_id": cid, "reference_no": ref, "party_name": pname, "service_line_id": slid,
                                   "service_line": slcode, "status": status, "score": 0, "reasons": []})
        m["score"] = max(m["score"], score)
        if reason not in m["reasons"]:
            m["reasons"].append(reason)

    # 1) identifiers
    ids = {"PAN": (ConflictCheck.pan, normalize_id(pan)), "GSTIN": (ConflictCheck.gstin, normalize_id(gstin)),
           "CIN": (ConflictCheck.cin, normalize_id(cin)), "Tax ID": (ConflictCheck.tax_id, normalize_id(tax_id))}
    conds = {label: col == val for label, (col, val) in ids.items() if val}
    if conds:
        q = (select(ConflictCheck.id, ConflictCheck.reference_no, ConflictCheck.party_name, ServiceLine.id,
                    ServiceLine.code, ConflictCheck.conflict_status, ConflictCheck.pan, ConflictCheck.gstin,
                    ConflictCheck.cin, ConflictCheck.tax_id)
             .join(ServiceLine, ServiceLine.id == ConflictCheck.service_line_id)
             .where(ConflictCheck.is_deleted.is_(False), or_(*conds.values())))
        if exclude_id:
            q = q.where(ConflictCheck.id != exclude_id)
        for cid, ref, pname, slid, slcode, st, e_pan, e_gst, e_cin, e_tax in s.execute(q):
            row = {"PAN": e_pan, "GSTIN": e_gst, "CIN": e_cin, "Tax ID": e_tax}
            for label in conds:
                if row[label] == ids[label][1]:
                    note(cid, ref, pname, slid, slcode, st, 100, f"Same {label}")

    # 2) + 3) names
    meta, names = _pool(s, exclude_id)
    queries = [(normalize_name(name), "Entity name")]
    queries += [(normalize_name(r), f"Related party '{r.strip()}'") for r in related_names if r and r.strip()]
    for qn, qlabel in queries:
        if len(qn) < config.FUZZY_MIN_LEN or not names:
            continue
        for _choice, score, idx in process.extract(qn, names, scorer=fuzz.WRatio, score_cutoff=threshold, limit=limit):
            cid, ref, pname, slid, slcode, st, kind, shown = meta[idx]
            target = "entity" if kind == "entity" else f"related party '{shown}'"
            note(cid, ref, pname, slid, slcode, st, round(score), f"{qlabel} ~ {target} ({round(score)}%)")

    out = sorted(found.values(), key=lambda m: (-m["score"], m["reference_no"]))[:limit]
    for m in out:
        m["level"] = LEVEL_HIGH if m["score"] >= config.FUZZY_HIGH else LEVEL_POSSIBLE
    return out
