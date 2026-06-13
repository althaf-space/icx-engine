"""Shared helpers for graph resolvers."""


def make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver: str) -> dict:
    """Build a resolver edge dict in the common shape used by all resolvers."""
    return {
        "source": src_id, "target": tgt_id,
        "source_file": src_file, "target_file": tgt_file,
        "relation": etype, "type": etype, "confidence": confidence,
        "resolver": resolver, "fix_confidence_delta": 0.0, "resolution_weight": 0.0,
    }
