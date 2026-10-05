import pytest

from app.analyze.scorer import (
    ParamScore,
    ai_params,
    links_score,
    load_rubric,
    opportunity_level,
    rubric_version,
    speed_score,
    total_score,
)
from app.models import OpportunityLevel


def test_rubric_keeps_joaquins_ten_parameters():
    """La rúbrica es fija (sección 8.2): 10 parámetros, 2 por código y 8 por la IA."""
    params = load_rubric()["parametros"]
    assert [p["id"] for p in params] == list(range(1, 11))
    assert [p["id"] for p in params if p["evalua"] == "codigo"] == [5, 9]
    assert [p["id"] for p in ai_params()] == [1, 2, 3, 4, 6, 7, 8, 10]
    for param in ai_params():
        assert set(param["tramos"]) == {"1-3", "4-6", "7-8", "9-10"}
    assert rubric_version()


@pytest.mark.parametrize(
    ("load_ms", "expected"),
    [(900, 10), (1499, 10), (1500, 8), (2400, 8), (3900, 6), (5900, 4), (8900, 2), (9000, 1), (30000, 1)],
)
def test_speed_table_by_measured_time(load_ms, expected):
    score = speed_score(load_ms, None)
    assert score.puntaje == expected
    assert "referencial" in score.justificacion
    assert score.fuente == "codigo"


@pytest.mark.parametrize(("psi", "expected"), [(95, 10), (54, 5), (3, 1), (0, 1)])
def test_speed_with_pagespeed(psi, expected):
    assert speed_score(9999, psi).puntaje == expected


def test_speed_without_measurement():
    assert speed_score(None, None) is None


@pytest.mark.parametrize(("errors", "expected"), [(0, 10), (1, 8), (2, 6), (3, 6), (4, 4), (6, 4), (7, 2), (20, 2)])
def test_links_table(errors, expected):
    assert links_score([f"error {i}" for i in range(errors)]).puntaje == expected


def test_total_needs_all_ten_parameters():
    partial = [ParamScore(id=i, nombre="x", puntaje=5, justificacion="", fuente="ia") for i in range(1, 10)]
    assert total_score(partial) is None
    full = partial + [ParamScore(id=10, nombre="x", puntaje=7, justificacion="", fuente="ia")]
    assert total_score(full) == 52


@pytest.mark.parametrize(
    ("score", "own_web", "rating", "reviews", "expected"),
    [
        (42, True, 4.7, 183, OpportunityLevel.ALTA),
        (42, True, 4.1, 183, OpportunityLevel.MEDIA),  # decisión: score < 50 sin buena reputación
        (42, True, 4.8, 5, OpportunityLevel.MEDIA),
        (55, True, 4.9, 300, OpportunityLevel.MEDIA),
        (69, True, 4.9, 300, OpportunityLevel.MEDIA),
        (70, True, 4.9, 300, OpportunityLevel.BAJA),
        (None, True, 4.9, 300, None),  # sin IA no hay score
        (None, False, 4.5, 40, OpportunityLevel.ALTA),  # sin web propia y buena reputación
        (None, False, 3.9, 40, OpportunityLevel.MEDIA),
    ],
)
def test_opportunity_level(score, own_web, rating, reviews, expected):
    assert opportunity_level(score, has_own_website=own_web, rating=rating, reviews=reviews) == expected
