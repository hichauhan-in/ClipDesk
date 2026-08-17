"""Notes enrichment levels stay aligned from API validation through prompts."""

import pytest
from pydantic import ValidationError

from clipdesk.analysis.prompts import (
    ENRICHMENT_LABELS,
    ENRICHMENT_LEVELS,
    ENRICHMENT_MARKER,
)
from clipdesk.server.schemas import NotesRequest


def test_every_enrichment_level_has_a_label_and_prompt():
    assert set(ENRICHMENT_LEVELS) == set(range(7))
    assert set(ENRICHMENT_LABELS) == set(ENRICHMENT_LEVELS)


def test_every_added_context_level_preserves_the_source_boundary():
    for level in range(1, 7):
        assert ENRICHMENT_MARKER in ENRICHMENT_LEVELS[level]


def test_expert_reference_requests_real_technical_depth():
    prompt = ENRICHMENT_LEVELS[6].lower()

    for expected in (
        "architecture",
        "implementation",
        "security",
        "reliability",
        "performance",
        "failure modes",
        "troubleshooting",
        "trade-offs",
    ):
        assert expected in prompt


def test_the_api_accepts_expert_reference_enrichment():
    assert NotesRequest(enrichment=6).enrichment == 6


def test_the_api_rejects_unknown_enrichment_levels():
    with pytest.raises(ValidationError):
        NotesRequest(enrichment=7)
