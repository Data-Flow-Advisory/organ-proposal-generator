"""pytest suite for the Proposal Generator Organ.

Verifies the pure-organ contract: deterministic, fail-safe, stdlib-only, and
correct decision logic across every gate (send / revise / reject).
"""
import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from organ import (
    decide,
    decide_proposal,
    evaluate_proposal,
    section_completeness,
    citation_interview_id,
    classify_grounding,
    available_interview_ids,
    _norm_id,
)

ROOT = Path(__file__).parent


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------

def _assert_valid_report(result):
    assert isinstance(result, dict)
    assert set(result.keys()) == {"output", "rationale", "self_metric"}
    out = result["output"]
    assert isinstance(out, dict)
    assert out["decision"] in {"send", "revise", "reject"}
    assert isinstance(out["missing_sections"], list)
    assert isinstance(out["orphan_citations"], list)
    assert isinstance(out["blockers"], list)
    sm = result["self_metric"]
    assert 0.0 <= sm["confidence"] <= 1.0
    assert isinstance(result["rationale"], str)


def test_decide_signature_is_state_context():
    assert list(inspect.signature(decide).parameters.keys())[:2] == ["state", "context"]


def test_aliases():
    assert decide_proposal is decide
    assert evaluate_proposal is decide


# ---------------------------------------------------------------------------
# Fail-safe
# ---------------------------------------------------------------------------

def test_empty_state_is_failsafe():
    result = decide({}, {})
    _assert_valid_report(result)
    assert result["output"]["decision"] == "reject"
    assert "empty_proposal" in result["output"]["blockers"]


def test_none_state_is_failsafe():
    result = decide(None, None)
    _assert_valid_report(result)
    assert result["output"]["decision"] == "reject"


def test_garbage_state_never_raises():
    for bad in [42, "x", [], {"proposal": 7}, {"proposal": {"exec_summary": object()}}]:
        result = decide(bad, None)
        _assert_valid_report(result)


def test_context_is_ignored_safely():
    state = _full_grounded_state()
    assert decide(state, None) == decide(state, {"anything": 1})


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_determinism():
    state = _full_grounded_state()
    assert decide(state, {}) == decide(state, {})


# ---------------------------------------------------------------------------
# _norm_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (7, "7"),
    ("7", "7"),
    ("  7 ", "7"),
    ("abc", "abc"),
    (None, None),
    (True, None),
    (False, None),
    ("", None),
    ([], None),
])
def test_norm_id(value, expected):
    assert _norm_id(value) == expected


# ---------------------------------------------------------------------------
# section_completeness
# ---------------------------------------------------------------------------

def test_section_completeness_all_present():
    proposal = {"a": "x", "b": "y"}
    present, missing = section_completeness(proposal, ["a", "b"])
    assert present == ["a", "b"]
    assert missing == []


def test_section_completeness_empty_string_is_missing():
    proposal = {"a": "x", "b": "   "}
    present, missing = section_completeness(proposal, ["a", "b"])
    assert present == ["a"]
    assert missing == ["b"]


def test_section_completeness_non_dict():
    present, missing = section_completeness("nope", ["a", "b"])
    assert present == []
    assert missing == ["a", "b"]


# ---------------------------------------------------------------------------
# citation_interview_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("link,expected", [
    ({"interview_id": 7, "question_id": 3}, "7"),
    ({"interview_id": "9"}, "9"),
    ("[interview_id:7 question_id:3]", "7"),
    ("[interview_id=12]", "12"),
    ("bare-id", "bare-id"),
    (5, "5"),
    ({}, None),
    (None, None),
    (True, None),
])
def test_citation_interview_id(link, expected):
    assert citation_interview_id(link) == expected


# ---------------------------------------------------------------------------
# classify_grounding / available_interview_ids
# ---------------------------------------------------------------------------

def test_available_interview_ids():
    excerpts = [{"interview_id": 1}, {"interview_id": "2"}, 3, "bad-dict-missing"]
    ids = available_interview_ids(excerpts)
    assert ids == {"1", "2", "3", "bad-dict-missing"}


def test_classify_grounding_split():
    links = [{"interview_id": 1}, {"interview_id": 99}, {"no": "id"}]
    grounded, orphan = classify_grounding(links, {"1", "2"})
    assert grounded == ["1"]
    assert orphan == ["99", "<no-interview-id>"]


def test_classify_grounding_non_list():
    grounded, orphan = classify_grounding("nope", {"1"})
    assert grounded == []
    assert orphan == []


# ---------------------------------------------------------------------------
# decide — SEND
# ---------------------------------------------------------------------------

def _full_grounded_state():
    return {
        "proposal": {
            "exec_summary": "We will cut your invoice cycle from 9 days to 2.",
            "problem_statement": "Manual invoice matching takes 9 days.",
            "solution": "Automated three-way match module.",
            "roi_case": "Saves 7 days per cycle, ~£40k/yr.",
            "evidence_links": [
                {"interview_id": 1, "question_id": 3},
                {"interview_id": 2, "question_id": 5},
            ],
            "version": "json_v1",
        },
        "available_excerpts": [
            {"interview_id": 1, "question_id": 3},
            {"interview_id": 2, "question_id": 5},
        ],
    }


def test_send_when_complete_and_grounded():
    result = decide(_full_grounded_state(), {})
    _assert_valid_report(result)
    assert result["output"]["decision"] == "send"
    assert result["output"]["blockers"] == []
    assert result["self_metric"]["completeness_pct"] == 100.0
    assert result["self_metric"]["grounded_links_count"] == 2
    assert result["self_metric"]["orphan_links_count"] == 0


def test_send_with_grounding_disabled_and_no_citations():
    state = _full_grounded_state()
    state["proposal"]["evidence_links"] = []
    state["require_grounding"] = False
    result = decide(state, {})
    assert result["output"]["decision"] == "send"


# ---------------------------------------------------------------------------
# decide — REVISE
# ---------------------------------------------------------------------------

def test_revise_when_section_missing():
    state = _full_grounded_state()
    state["proposal"]["roi_case"] = ""
    result = decide(state, {})
    assert result["output"]["decision"] == "revise"
    assert "roi_case" in result["output"]["missing_sections"]
    assert any(b.startswith("missing_sections:") for b in result["output"]["blockers"])


def test_revise_when_insufficient_evidence():
    state = _full_grounded_state()
    state["proposal"]["evidence_links"] = []
    result = decide(state, {})
    assert result["output"]["decision"] == "revise"
    assert any(b.startswith("insufficient_evidence:") for b in result["output"]["blockers"])


def test_revise_when_citation_is_orphan():
    state = _full_grounded_state()
    # Cite an interview not in the discovery pool — the hallucination case.
    state["proposal"]["evidence_links"] = [{"interview_id": 999, "question_id": 1}]
    result = decide(state, {})
    assert result["output"]["decision"] == "revise"
    assert "999" in result["output"]["orphan_citations"]
    assert any(b.startswith("orphan_citations:") for b in result["output"]["blockers"])


def test_revise_confidence_scales_with_completeness():
    state = _full_grounded_state()
    state["proposal"]["evidence_links"] = []  # only the grounding gate fails
    full = decide(state, {})
    # drop a section too — less complete → lower confidence
    state["proposal"]["solution"] = ""
    partial = decide(state, {})
    assert partial["self_metric"]["confidence"] < full["self_metric"]["confidence"]


# ---------------------------------------------------------------------------
# decide — REJECT
# ---------------------------------------------------------------------------

def test_reject_when_all_sections_empty():
    state = {"proposal": {"exec_summary": "", "problem_statement": "  ",
                          "solution": "", "roi_case": ""}}
    result = decide(state, {})
    assert result["output"]["decision"] == "reject"
    assert "empty_proposal" in result["output"]["blockers"]


def test_reject_when_proposal_absent():
    result = decide({"available_excerpts": [{"interview_id": 1}]}, {})
    assert result["output"]["decision"] == "reject"


# ---------------------------------------------------------------------------
# custom required_sections / min_evidence_links
# ---------------------------------------------------------------------------

def test_custom_required_sections():
    state = {
        "proposal": {"exec_summary": "hi", "evidence_links": [{"interview_id": 1}]},
        "available_excerpts": [{"interview_id": 1}],
        "required_sections": ["exec_summary"],
    }
    result = decide(state, {})
    assert result["output"]["decision"] == "send"


def test_custom_min_evidence_links():
    state = _full_grounded_state()
    state["min_evidence_links"] = 5  # require more than provided
    result = decide(state, {})
    assert result["output"]["decision"] == "revise"
    assert any(b.startswith("insufficient_evidence:") for b in result["output"]["blockers"])


def test_bad_required_sections_falls_back_to_default():
    state = _full_grounded_state()
    state["required_sections"] = "not-a-list"
    result = decide(state, {})
    # default four sections all present → still sends
    assert result["output"]["decision"] == "send"
    assert result["self_metric"]["sections_required"] == 4


# ---------------------------------------------------------------------------
# stdlib-only enforcement
# ---------------------------------------------------------------------------

def test_organ_is_stdlib_only():
    stdlib_allowed = {"json", "re", "sys", "os", "typing", "__future__"}
    tree = ast.parse((ROOT / "organ.py").read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.append(node.module.split(".")[0])
    external = sorted({m for m in found if m not in stdlib_allowed})
    assert not external, f"non-stdlib imports: {external}"


# ---------------------------------------------------------------------------
# CLI adapter + samples
# ---------------------------------------------------------------------------

def test_cli_roundtrip_on_samples():
    for sample in sorted((ROOT / "samples").glob("*.json")):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "organ.py")],
            input=sample.read_text(),
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        _assert_valid_report(result)


def test_cli_organ_input_as_file_path():
    """ORGAN_INPUT may be a path to a sample file (fleet verify convention)."""
    sample = ROOT / "samples" / "ready_proposal_send.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "organ.py")],
        input="",  # empty stdin → fall through to ORGAN_INPUT
        capture_output=True, text=True,
        env={**os.environ, "ORGAN_INPUT": str(sample)},
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    _assert_valid_report(result)
    assert result["output"]["decision"] == "send", result


def test_cli_organ_input_as_inline_json():
    """ORGAN_INPUT still accepts inline JSON when it is not a file path."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "organ.py")],
        input="",
        capture_output=True, text=True,
        env={**os.environ, "ORGAN_INPUT": "{\"state\": {}, \"context\": {}}"},
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    _assert_valid_report(result)


def test_sample_decisions_match_filenames():
    """Each sample filename encodes its expected decision; verify it holds."""
    expected = {
        "ready_proposal_send": "send",
        "missing_section_revise": "revise",
        "orphan_citation_revise": "revise",
    }
    for name, decision in expected.items():
        data = json.loads((ROOT / "samples" / f"{name}.json").read_text())
        result = decide(data.get("state") or {}, data.get("context") or {})
        assert result["output"]["decision"] == decision, (name, result)
