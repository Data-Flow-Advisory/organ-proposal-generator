#!/usr/bin/env python3
"""Proposal Generator Organ.

A pure, stdlib-only decision organ extracted from discovery-engine's
``app/services/proposal_generator.py``.

CONTRACT (orchestrator pure-organ protocol)
--------------------------------------------
    decide(state, context) -> {"output", "rationale", "self_metric"}

  * Pure: no DB, no network, no filesystem, no env reads, no clock — every
    input arrives via ``state`` / ``context``.
  * Deterministic: same (state, context) always yields the same result.
  * Fail-safe: never raises. Bad/empty input returns a valid structure with a
    low ``confidence`` and an explanatory ``rationale``.
  * Stdlib-only: imports nothing outside the Python standard library.
  * ``self_metric.confidence`` is a float in ``[0.0, 1.0]``.

WHAT THIS ORGAN DECIDES
-----------------------
Whether a generated proposal is **ready to send**, needs **revision**, or
should be **rejected** — by checking it against the live wire's *core
discipline*:

    Every claim traces to the prospect's own words or figures from the
    discovery interviews.

The AI call that drafts the proposal lives in the impure service; what this
organ extracts is the deterministic *gate* the service depends on:

  * **Completeness** — the required proposal sections (exec_summary,
    problem_statement, solution, roi_case) must be present and non-empty.
  * **Grounding** — the proposal must cite discovery evidence, and every
    citation must point at an interview/excerpt that actually exists in the
    discovery pool. Citations to evidence not in the pool are *orphans* — the
    hallucination failure mode the discipline exists to catch.

Decision values:
  * ``send``   — complete AND grounded; safe to deliver.
  * ``revise`` — recoverable gaps (missing sections, too few citations, or
    orphan citations); send back for another pass.
  * ``reject`` — empty / unparseable proposal; nothing to revise.

The CLI (``python organ.py < input.json``) reads ``{state, context}`` on
stdin (or ``$ORGAN_INPUT``) and writes ``{output, rationale, self_metric}`` to
stdout, so the orchestrator can shell out to it like any other organ.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

# The proposal sections the live wire guarantees (minus the bookkeeping
# ``version`` / ``evidence_links`` keys). Mirrors ``_PROPOSAL_KEYS`` and the
# ``setdefault`` block in the source's ``_parse_proposal_json``.
_DEFAULT_REQUIRED_SECTIONS = ("exec_summary", "problem_statement", "solution", "roi_case")

# A grounded proposal must cite at least this many discovery excerpts. The
# discipline is "every quantified claim links to an excerpt"; one citation is
# the floor below which the proposal is ungrounded by construction.
_DEFAULT_MIN_EVIDENCE_LINKS = 1


# ---------------------------------------------------------------------------
# Pure helpers — no IO, deterministic, testable in isolation.
# ---------------------------------------------------------------------------

def _norm_id(value: Any) -> Optional[str]:
    """Normalise an interview/question id to a comparable string.

    Ints and strings collapse to the same key (``7`` and ``"7"`` match), so a
    citation written as a number grounds against an excerpt keyed as a string.
    Returns ``None`` for empty / unusable ids.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return None


def section_completeness(proposal: Any, required: List[str]) -> tuple:
    """Return ``(present, missing)`` lists for the required proposal sections.

    A section counts as present when the key exists and its value is a
    non-empty, non-whitespace string (or any non-empty value for non-strings).
    Defensive: a non-dict proposal yields everything missing.
    """
    present: List[str] = []
    missing: List[str] = []
    if not isinstance(proposal, dict):
        return present, list(required)
    for key in required:
        val = proposal.get(key)
        if isinstance(val, str):
            ok = bool(val.strip())
        else:
            ok = bool(val)
        (present if ok else missing).append(key)
    return present, missing


def citation_interview_id(link: Any) -> Optional[str]:
    """Extract the cited interview id from one evidence link.

    Handles three shapes the source produces / consumes:
      * dict  — ``{"interview_id": <id>, ...}``
      * string — ``"[interview_id:7 question_id:3]"`` (the prompt's notation),
        or a bare id.
      * int   — a bare interview id.

    Returns the normalised interview id, or ``None`` when none can be found
    (which makes the link an orphan).
    """
    if isinstance(link, dict):
        return _norm_id(link.get("interview_id"))
    if isinstance(link, int) and not isinstance(link, bool):
        return _norm_id(link)
    if isinstance(link, str):
        m = re.search(r"interview_id\s*[:=]\s*([A-Za-z0-9_-]+)", link)
        if m:
            return _norm_id(m.group(1))
        s = link.strip()
        return s or None
    return None


def classify_grounding(evidence_links: Any, available_ids: set) -> tuple:
    """Split evidence links into ``(grounded, orphan)`` interview-id lists.

    A link is *grounded* when its interview id is present in
    ``available_ids`` (the discovery excerpt pool). A link with no extractable
    id, or one citing an interview not in the pool, is an *orphan* — the
    hallucinated-evidence failure mode.
    """
    grounded: List[str] = []
    orphan: List[str] = []
    if not isinstance(evidence_links, list):
        return grounded, orphan
    for link in evidence_links:
        iid = citation_interview_id(link)
        if iid is not None and iid in available_ids:
            grounded.append(iid)
        else:
            orphan.append(iid if iid is not None else "<no-interview-id>")
    return grounded, orphan


def available_interview_ids(excerpts: Any) -> set:
    """Build the set of interview ids present in the discovery excerpt pool.

    Each excerpt may be a dict with ``interview_id`` or a bare id. Malformed
    entries are skipped.
    """
    ids: set = set()
    if not isinstance(excerpts, list):
        return ids
    for e in excerpts:
        if isinstance(e, dict):
            iid = _norm_id(e.get("interview_id"))
        else:
            iid = _norm_id(e)
        if iid is not None:
            ids.add(iid)
    return ids


# ---------------------------------------------------------------------------
# The decision — the pure organ entry point.
# ---------------------------------------------------------------------------

def _empty_report() -> Dict[str, Any]:
    return {
        "output": {
            "decision": "reject",
            "missing_sections": [],
            "orphan_citations": [],
            "blockers": [],
        },
        "rationale": "",
        "self_metric": {
            "confidence": 0.0,
            "sections_present": 0,
            "sections_required": 0,
            "completeness_pct": 0.0,
            "evidence_links_count": 0,
            "grounded_links_count": 0,
            "orphan_links_count": 0,
            "decision": "reject",
        },
    }


def decide(state: Optional[Dict[str, Any]],
           context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide whether a proposal is ready to send, needs revision, or rejected.

    The canonical pure-organ contract function: ``decide(state, context)``.

    ``state`` keys (all optional; defensive defaults applied):
      - ``proposal`` (dict)              — the generated proposal sections, e.g.
        ``{exec_summary, problem_statement, solution, roi_case,
        evidence_links, version}``; default {}.
      - ``available_excerpts`` (list)    — discovery evidence pool the proposal
        may cite, each ``{"interview_id", "question_id", ...}`` or a bare id;
        default [].
      - ``required_sections`` (list[str])— sections that must be present;
        default the live wire's four.
      - ``min_evidence_links`` (int)     — minimum citations for grounding;
        default 1.
      - ``require_grounding`` (bool)      — enforce the evidence gate at all;
        default True.

    ``output``:
      - ``decision``           — ``send`` | ``revise`` | ``reject``.
      - ``missing_sections``   — required sections absent / empty.
      - ``orphan_citations``   — cited interview ids not in the discovery pool.
      - ``blockers``           — human-readable reasons the proposal isn't sendable.

    Returns ``{output, rationale, self_metric}``. Never raises.
    """
    report = _empty_report()
    try:
        if not isinstance(state, dict):
            state = {}

        proposal = state.get("proposal", {})
        if not isinstance(proposal, dict):
            proposal = {}

        raw_required = state.get("required_sections", _DEFAULT_REQUIRED_SECTIONS)
        if isinstance(raw_required, (list, tuple)):
            required = [s for s in raw_required if isinstance(s, str) and s.strip()]
        else:
            required = []
        if not required:
            required = list(_DEFAULT_REQUIRED_SECTIONS)

        min_links = state.get("min_evidence_links", _DEFAULT_MIN_EVIDENCE_LINKS)
        if not isinstance(min_links, int) or isinstance(min_links, bool) or min_links < 0:
            min_links = _DEFAULT_MIN_EVIDENCE_LINKS

        require_grounding = bool(state.get("require_grounding", True))

        # ---- compute the deterministic facts ----
        present, missing = section_completeness(proposal, required)
        completeness_pct = round(100.0 * len(present) / len(required), 1) if required else 0.0

        evidence_links = proposal.get("evidence_links", [])
        if not isinstance(evidence_links, list):
            evidence_links = []
        available_ids = available_interview_ids(state.get("available_excerpts", []))
        grounded, orphan = classify_grounding(evidence_links, available_ids)

        report["output"]["missing_sections"] = list(missing)
        report["output"]["orphan_citations"] = list(orphan)
        report["self_metric"].update({
            "sections_present": len(present),
            "sections_required": len(required),
            "completeness_pct": completeness_pct,
            "evidence_links_count": len(evidence_links),
            "grounded_links_count": len(grounded),
            "orphan_links_count": len(orphan),
        })

        blockers: List[str] = []

        # Gate 1 — REJECT: nothing usable. An empty proposal isn't revisable.
        if len(present) == 0:
            report["output"]["decision"] = "reject"
            report["output"]["blockers"] = ["empty_proposal"]
            report["rationale"] = (
                "Proposal has no populated sections; nothing to revise — reject "
                "and re-generate from the discovery artifact."
            )
            report["self_metric"].update({"confidence": 0.95, "decision": "reject"})
            return report

        # Gate 2 — REVISE: required sections missing.
        if missing:
            blockers.append("missing_sections:" + ",".join(missing))

        # Gate 3 + 4 — grounding (only when enforced).
        if require_grounding:
            if len(evidence_links) < min_links:
                blockers.append(f"insufficient_evidence:{len(evidence_links)}<{min_links}")
            if orphan:
                blockers.append("orphan_citations:" + ",".join(orphan))

        if blockers:
            report["output"]["decision"] = "revise"
            report["output"]["blockers"] = blockers
            parts = []
            if missing:
                parts.append(f"{len(missing)} required section(s) missing ({', '.join(missing)})")
            if require_grounding and len(evidence_links) < min_links:
                parts.append(f"only {len(evidence_links)} citation(s), need {min_links}")
            if require_grounding and orphan:
                parts.append(f"{len(orphan)} orphan citation(s) not in the discovery pool")
            report["rationale"] = (
                "Revise: " + "; ".join(parts) + ". Every claim must trace to a "
                "discovery excerpt before this proposal is sendable."
            )
            # Confidence scales with how complete the proposal already is.
            report["self_metric"].update({
                "confidence": round(0.6 + 0.3 * (len(present) / len(required)), 2),
                "decision": "revise",
            })
            return report

        # All gates pass — SEND.
        report["output"]["decision"] = "send"
        report["output"]["blockers"] = []
        if require_grounding:
            ground_txt = f"{len(grounded)} grounded citation(s)"
        else:
            ground_txt = "grounding not enforced"
        report["rationale"] = (
            f"Send: all {len(required)} required section(s) present and "
            f"{ground_txt}; proposal is complete and grounded in discovery evidence."
        )
        report["self_metric"].update({"confidence": 0.95, "decision": "send"})
        return report

    except Exception as exc:  # fail-safe — a broken decision must never raise.
        return {
            "output": {
                "decision": "reject",
                "missing_sections": [],
                "orphan_citations": [],
                "blockers": ["error_fail_open"],
            },
            "rationale": f"decide() failed open: {exc}",
            "self_metric": {
                "confidence": 0.0,
                "sections_present": 0,
                "sections_required": 0,
                "completeness_pct": 0.0,
                "evidence_links_count": 0,
                "grounded_links_count": 0,
                "orphan_links_count": 0,
                "decision": "error",
                "error": str(exc),
            },
        }


# Backward-compatible aliases for callers expecting alternate names.
decide_proposal = decide
evaluate_proposal = decide


# ---------------------------------------------------------------------------
# CLI adapter — stdin JSON in, stdout JSON out. Not part of the pure contract.
# ---------------------------------------------------------------------------

def _read_input() -> Dict[str, Any]:
    text = sys.stdin.read()
    if not text.strip():
        import os
        text = os.getenv("ORGAN_INPUT", "{}")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def main() -> int:
    try:
        data = _read_input()
        result = decide(data.get("state") or {}, data.get("context") or {})
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        json.dump({
            "output": {
                "decision": "reject",
                "missing_sections": [],
                "orphan_citations": [],
                "blockers": ["error_fail_open"],
            },
            "rationale": f"CLI fatal error: {exc}",
            "self_metric": {"confidence": 0.0, "decision": "error", "error": str(exc)},
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
