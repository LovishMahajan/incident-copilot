"""Artifact 9.2, 13.5 T1.10–T1.12: the Evidence Collector.

`sources` holds the read-only source adapters, chosen per tool by configuration; `collector`
runs a plan of tool calls through the ToolGate and those adapters, then redacts, caps,
fingerprints and flags each result as an evidence record, or records a gap; `facts` computes
the ADR-53 observations that set each record's strength tier; `snapshots` keeps the raw
(redacted) results in the object store. Storage is aisre.record.evidence. No model call.
"""
