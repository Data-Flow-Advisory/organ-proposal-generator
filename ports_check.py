#!/usr/bin/env python3
"""Ports checker for the organ-proposal-generator organ (the connection-standard gate).

This is the port half of conformance, per CONNECTORS.md ("Conformance gains a
port check"). It asserts, with NO arguments:

  1. ``ports.json`` parses and has the standard shape — ``inputs`` is a list of
     ``{name, type, required}`` and ``outputs`` is a list of ``{name, type}``.
  2. Every ``type`` named in ports.json exists in the shared type vocabulary
     (``types.json``, a vendored snapshot of the orchestrator vocabulary plus
     this organ's proposed additions).
  3. ``decide`` actually READS each declared input name and WRITES each declared
     output name — sampled against the organ's own ``samples/*.json``:
       * read-check: each declared input name appears under ``state`` in at least
         one input sample (a sample whose ``state`` carries ``proposal``);
       * write-check: running ``decide`` on every input sample, each declared
         output name is a top-level key of the returned ``output`` dict.

Exits non-zero with a clear message on any violation, so the conformance
workflow goes RED if the organ's ports ever drift from its real decide() I/O or
reference a type outside the vocabulary.

Usage (in CI):  python3 ports_check.py
"""
from __future__ import annotations

import json
import pathlib
import sys

import organ

_HERE = pathlib.Path(__file__).resolve().parent


def _fail(msg: str) -> int:
    print(f"ports violation: {msg}")
    return 1


def _load_json(name: str):
    path = _HERE / name
    if not path.exists():
        raise FileNotFoundError(f"{name} not found beside organ.py")
    with path.open() as fh:
        return json.load(fh)


def main() -> int:
    # --- 1. ports.json parses + has the standard shape -----------------------
    try:
        ports = _load_json("ports.json")
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return _fail(f"ports.json did not parse: {exc}")
    if not isinstance(ports, dict):
        return _fail("ports.json top-level is not an object")

    inputs = ports.get("inputs")
    outputs = ports.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        return _fail("ports.json must carry list `inputs` and list `outputs`")

    for port in inputs:
        if not isinstance(port, dict) or "name" not in port or "type" not in port:
            return _fail(f"input port missing name/type: {port!r}")
        if "required" not in port:
            return _fail(f"input port {port.get('name')!r} missing `required`")
        if not isinstance(port["required"], bool):
            return _fail(f"input port {port['name']!r} `required` must be bool")
    for port in outputs:
        if not isinstance(port, dict) or "name" not in port or "type" not in port:
            return _fail(f"output port missing name/type: {port!r}")

    # --- 2. every referenced type exists in the vocabulary -------------------
    try:
        vocab = _load_json("types.json")
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return _fail(f"types.json did not parse: {exc}")
    known_types = set((vocab.get("types") or {}).keys())
    if not known_types:
        return _fail("types.json carries no `types`")
    for port in inputs + outputs:
        t = port["type"]
        if t not in known_types:
            return _fail(
                f"port {port['name']!r} references type {t!r} "
                f"which is not in the vocabulary (types.json)"
            )

    # --- 3. decide reads/writes the declared names (sampled) -----------------
    sample_paths = sorted((_HERE / "samples").glob("*.json"))
    # An input sample is one whose top-level `state` is a dict carrying `proposal`.
    input_samples = []
    for p in sample_paths:
        try:
            doc = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            return _fail(f"sample {p.name} did not parse: {exc}")
        st = doc.get("state")
        if isinstance(st, dict) and "proposal" in st:
            input_samples.append((p.name, doc))
    if not input_samples:
        return _fail("no input samples (state carrying `proposal`) to validate ports against")

    # read-check: every declared input name appears in at least one sample's state.
    state_keys_seen: set = set()
    for _name, doc in input_samples:
        state_keys_seen.update((doc.get("state") or {}).keys())
    for port in inputs:
        if port["name"] not in state_keys_seen:
            return _fail(
                f"declared input {port['name']!r} is never present under `state` "
                f"in any input sample — cannot prove decide() reads it"
            )

    # write-check: decide()'s output carries every declared output name on every sample.
    for sname, doc in input_samples:
        result = organ.decide(doc.get("state") or {}, doc.get("context") or {})
        out = result.get("output")
        if not isinstance(out, dict):
            return _fail(f"decide() on {sname} returned no `output` dict")
        for port in outputs:
            if port["name"] not in out:
                return _fail(
                    f"declared output {port['name']!r} absent from decide() "
                    f"output on sample {sname} — cannot prove decide() writes it"
                )

    print(
        f"  ports OK: {len(inputs)} input(s) + {len(outputs)} output(s), "
        f"all types in vocabulary, all names read/written across "
        f"{len(input_samples)} input sample(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
