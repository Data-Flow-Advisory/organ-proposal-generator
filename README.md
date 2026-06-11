# organ-proposal-generator

A pure, stdlib-only **decision organ** extracted from discovery-engine's
`app/services/proposal_generator.py`.

## What is an organ?

A small, self-contained decision-maker conforming to the orchestrator
pure-organ contract:

```
decide(state, context) -> {"output", "rationale", "self_metric"}
```

- **Pure** — no DB, network, filesystem, env reads, or clock. Everything
  arrives via `state` / `context`.
- **Deterministic** — same input always yields the same output.
- **Fail-safe** — never raises; bad/empty input returns a valid structure with
  low `confidence` and an explanatory `rationale`.
- **Stdlib-only** — Python standard library only.
- **`self_metric.confidence`** is a float in `[0.0, 1.0]`.

## What this organ decides

Whether a generated proposal is **ready to send**, needs **revision**, or
should be **rejected** — by checking it against the live service's *core
discipline*:

> Every claim traces to the prospect's own words or figures from the discovery
> interviews.

The AI call that *drafts* the proposal lives in the impure service. What this
organ extracts is the deterministic **gate** that service depends on:

1. **Completeness** — the required sections (`exec_summary`,
   `problem_statement`, `solution`, `roi_case`) must be present and non-empty.
   Mirrors `_PROPOSAL_KEYS` and the `setdefault` block in the source's
   `_parse_proposal_json`.
2. **Grounding** — the proposal must cite discovery evidence, and every
   citation must point at an interview that actually exists in the discovery
   pool. A citation to evidence *not* in the pool is an **orphan** — the
   hallucinated-source failure mode the discipline exists to catch (the source
   prompt: "Every quantified claim must link to an excerpt above. No invented
   numbers.").

## Decision values

| `decision` | When | Meaning |
|-----------|------|---------|
| `send`   | complete **and** grounded | Safe to deliver. |
| `revise` | recoverable gaps (missing sections / too few citations / orphan citations) | Bounce back for another pass. |
| `reject` | no populated sections at all | Nothing to revise — re-generate. |

## Gate sequence

| Order | Condition | result |
|-------|-----------|--------|
| 1 | no populated sections | `reject` (`empty_proposal`) |
| 2 | required sections missing | `revise` (`missing_sections:...`) |
| 3 | citations below `min_evidence_links` (when grounding enforced) | `revise` (`insufficient_evidence:...`) |
| 4 | citation references an interview not in the discovery pool | `revise` (`orphan_citations:...`) |
| — | all gates pass | `send` |

## `state` keys (all optional; defensive defaults applied)

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `proposal` | dict | `{}` | The generated sections (`exec_summary`, `problem_statement`, `solution`, `roi_case`, `evidence_links`, `version`). |
| `available_excerpts` | list | `[]` | The discovery evidence pool the proposal may cite. Each `{interview_id, question_id, ...}` or a bare id. |
| `required_sections` | list[str] | the four above | Sections that must be present & non-empty. |
| `min_evidence_links` | int | `1` | Minimum citations for grounding. |
| `require_grounding` | bool | `true` | Enforce the evidence gates at all. |

## `output`

```json
{
  "decision": "send | revise | reject",
  "missing_sections": ["roi_case"],
  "orphan_citations": ["999"],
  "blockers": ["missing_sections:roi_case", "orphan_citations:999"]
}
```

`self_metric` additionally carries `sections_present`, `sections_required`,
`completeness_pct`, `evidence_links_count`, `grounded_links_count`,
`orphan_links_count`, and `confidence`.

## Usage

As a library:

```python
from organ import decide

result = decide(
    {
        "proposal": {
            "exec_summary": "...", "problem_statement": "...",
            "solution": "...", "roi_case": "...",
            "evidence_links": [{"interview_id": 12, "question_id": 4}],
        },
        "available_excerpts": [{"interview_id": 12, "question_id": 4}],
    },
    {},
)
print(result["output"]["decision"])  # -> "send"
```

As a CLI (stdin JSON in, stdout JSON out — for the orchestrator to shell to):

```bash
python organ.py < samples/ready_proposal_send.json
```

## Samples

| File | Expected decision |
|------|-------------------|
| `samples/ready_proposal_send.json` | `send` |
| `samples/missing_section_revise.json` | `revise` (empty `roi_case`) |
| `samples/orphan_citation_revise.json` | `revise` (cites interview not in pool) |

## Ports (the connection standard)

Per [`CONNECTORS.md`](https://raw.githubusercontent.com/Data-Flow-Advisory/orchestrator/feat/drift-gate/CONNECTORS.md),
`ports.json` declares this organ's typed studs — the wiring addresses by which a
composer snaps it to other organs:

| Direction | Port (`state`/`output` key) | Type | Required |
|-----------|-----------------------------|------|----------|
| input | `proposal` | `Proposal` | yes |
| input | `available_excerpts` | `DiscoveryExcerpts` | no |
| output | `decision` | `ProposalVerdict` | — |
| output | `missing_sections` | `ProposalVerdict` | — |
| output | `orphan_citations` | `ProposalVerdict` | — |
| output | `blockers` | `ProposalVerdict` | — |

The four output ports are the named fields of the single `ProposalVerdict` the
organ emits. The three tuning/control knobs (`required_sections`,
`min_evidence_links`, `require_grounding`) are configuration, not composition
wires, so they are intentionally **not** declared as ports.

`Proposal`, `DiscoveryExcerpts` and `ProposalVerdict` are **proposed** additions
to the shared vocabulary (proposal generation is a Stream 39 domain the
discovery→blueprint spine vocabulary doesn't yet cover) — marked `proposed: true`
in `types.json` (a vendored snapshot of the orchestrator vocabulary so
conformance can validate offline) and awaiting upstream review.

`ports_check.py` (run as a `conformance` step) asserts `ports.json` parses, every
referenced type exists in `types.json`, and `decide()` actually reads each
declared input name and writes each declared output name across the samples.

## Tests

```bash
pip install pytest
pytest test_organ.py -v
```

The `conformance` GitHub Action runs the suite on Python 3.10–3.12, plus
explicit signature / fail-safe / determinism / stdlib-only checks, the
`ports_check.py` connection-standard gate, and prints each sample's decision to
the job summary.

## Provenance

Extracted from `app/services/proposal_generator.py` (Stream 39 — prospect-to-
proposal pipeline). The impure parts of the source (the Claude call, cost
recording, DB persistence, HTML rendering of crib sheets / board summaries)
are intentionally **not** carried into the organ — only the deterministic
send/revise/reject judgement.
