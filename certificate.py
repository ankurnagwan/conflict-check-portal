"""
certificate.py - builds the stamped PDF Approval Certificate (pure function: dict in, PDF bytes out).

The caller (workflow._issue_certificate) stores the bytes under PROOF_ROOT and records the file's
SHA-256 in proof_documents. A PDF cannot contain its own hash, so the certificate points the reader
to the hash recorded in the portal and prints the SHA-256 of every supporting document it relied on.

Text is passed through latin-1 (Helvetica has no Devanagari/CJK glyphs); unsupported characters
print as '?'. Names are still stored correctly in the database.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

import config

NAVY, GREEN, GREY = colors.HexColor("#1e3a5f"), colors.HexColor("#15803d"), colors.HexColor("#6b7280")


def _t(v) -> str:
    s = "-" if v in (None, "") else str(v)
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ts(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    from database import to_ist
    ist = to_ist(dt)
    return f"{ist:%d %b %Y, %H:%M:%S} IST  ({dt:%Y-%m-%d %H:%M:%S} UTC)"


def _person(p: Optional[dict]) -> str:
    return "-" if not p else f"{p['name']}  [{p['username']} | {p['role']}]"


def build_certificate(d: dict) -> bytes:
    """d keys: reference_no, certificate_no, party_name, party_type, scope, country, service_line, identifiers{},
    outcome ('APPROVED'|'CLEARED'), option ('A'|'B'), requested_by/decided_by/aligned_partner/cleared_by {name,username,role},
    created_at/decided_at/alignment_requested_at/alignment_confirmed_at/cleared_at, notes, alignment_notes,
    documents[{label, filename, sha256, uploaded_at}], audit_seq, issued_at"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=34 * mm,
                            bottomMargin=20 * mm, title=f"Approval Certificate {d['reference_no']}",
                            author=config.APP_NAME, subject="Conflict check approval certificate")
    ss = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=ss["Heading2"], textColor=NAVY, spaceBefore=10, spaceAfter=4, fontSize=11)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9, leading=12)
    mono = ParagraphStyle("m", parent=body, fontName="Courier", fontSize=6.6, leading=8)
    small = ParagraphStyle("s", parent=body, fontSize=7.5, textColor=GREY, leading=10)

    def kv(rows):
        t = Table([[Paragraph(f"<b>{_t(k)}</b>", body), Paragraph(_t(v), body)] for k, v in rows],
                  colWidths=[48 * mm, 124 * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3)]))
        return t

    ids = d.get("identifiers") or {}
    id_txt = "   ".join(f"{k}: {v}" for k, v in ids.items() if v) or "-"
    option_txt = ("Option A - Direct clearance by Risk Lead" if d["option"] == "A"
                  else "Option B - Partner alignment confirmed, then cleared by Risk Lead")
    story = [Paragraph("Entity", h), kv([
        ("Reference no.", d["reference_no"]), ("Entity name", d["party_name"]),
        ("Entity type / scope", f"{d['party_type']}  |  {d['scope']}" + (f"  |  {d['country']}" if d.get("country") else "")),
        ("Service line", d["service_line"]), ("Identifiers", id_txt)])]

    steps = [("Raised by", _person(d.get("requested_by"))), ("Raised on", _ts(d.get("created_at"))),
             ("Conflict outcome", option_txt)]
    if d["option"] == "B":
        steps += [("Alignment requested by", _person(d.get("decided_by"))),
                  ("Alignment requested on", _ts(d.get("alignment_requested_at"))),
                  ("Aligned partner", _person(d.get("aligned_partner"))),
                  ("Partner sign-off on", _ts(d.get("alignment_confirmed_at"))),
                  ("Partner sign-off notes", d.get("alignment_notes"))]
    steps += [("Cleared by (Risk Lead)", _person(d.get("cleared_by"))), ("Cleared on", _ts(d.get("cleared_at"))),
              ("Clearance notes", d.get("notes"))]
    story += [Paragraph("Decision trail", h), kv(steps)]

    story.append(Paragraph("Supporting documents relied upon (SHA-256)", h))
    docs = d.get("documents") or []
    if docs:
        rows = [[Paragraph("<b>Document</b>", small), Paragraph("<b>File / SHA-256</b>", small)]]
        for x in docs:
            rows.append([Paragraph(_t(x["label"]), body),
                         [Paragraph(_t(x["filename"]), body), Paragraph(_t(x["sha256"]), mono)]])
        t = Table(rows, colWidths=[52 * mm, 120 * mm])
        t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
                               ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
        story.append(t)
    else:
        story.append(Paragraph("No supporting documents were attached.", body))

    story += [Paragraph("Integrity", h), Paragraph(
        f"Issued {_ts(d.get('issued_at'))} at audit-trail entry #{_t(d.get('audit_seq'))}. This certificate is "
        "generated by the portal; its own SHA-256 is recorded in the portal database and can be re-verified from "
        "<i>Records &gt; Documents</i>. A certificate whose hash no longer matches must be treated as invalid.", body)]

    outcome, issued = d["outcome"], d.get("issued_at")

    def frame(c, _doc):
        w, hh = A4
        c.saveState()
        c.setFillColor(NAVY); c.rect(0, hh - 26 * mm, w, 26 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 15)
        c.drawString(18 * mm, hh - 12 * mm, "CONFLICT CHECK - APPROVAL CERTIFICATE")
        c.setFont("Helvetica", 8.5)
        c.drawString(18 * mm, hh - 19 * mm, _t(config.APP_NAME).replace("&amp;", "&"))
        c.drawRightString(w - 18 * mm, hh - 12 * mm, f"Certificate {d['certificate_no']}")
        # rubber-stamp
        c.translate(w - 50 * mm, hh - 52 * mm); c.rotate(14)
        c.setStrokeColor(GREEN); c.setFillColor(GREEN); c.setLineWidth(2.2)
        c.roundRect(-24 * mm, -9 * mm, 48 * mm, 18 * mm, 3 * mm, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 15); c.drawCentredString(0, 0.5 * mm, outcome)
        c.setFont("Helvetica", 6.8)
        from database import to_ist
        c.drawCentredString(0, -5.5 * mm, f"{to_ist(issued):%d %b %Y}" if issued else "")
        c.restoreState()
        c.saveState(); c.setFont("Helvetica", 7); c.setFillColor(GREY)
        c.drawString(18 * mm, 11 * mm, "System-generated. Valid only while its SHA-256 matches the value recorded in the portal.")
        c.drawRightString(w - 18 * mm, 11 * mm, f"Page {c.getPageNumber()}")
        c.restoreState()

    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    return buf.getvalue()
