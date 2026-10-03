"""Tests for the packet page plan and pipeline (design sections 3.3 and 4.8).

Stage B6's parts of the design section 6 list. The shared page-plan vectors
come from tests/fixtures/packet-plan.json; every source PDF is built here with
reportlab and pypdf.
"""
import json
from pathlib import Path

import pytest

from pdf_ops.packet import DEFAULT_OPTIONS, page_plan

# This file's stage. The fixture's stage order decides which vectors are later.
STAGE = "B6"
CONSUMER = "pipeline"

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "packet-plan.json"
PLAN_FIXTURE = json.loads(FIXTURE.read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# Shared page-plan vectors
# ---------------------------------------------------------------------------
def _vector_params():
    order = PLAN_FIXTURE["stage_order"]
    params = []
    for vector in PLAN_FIXTURE["vectors"]:
        tag = vector["stage"][CONSUMER]
        marks = []
        if order.index(tag) > order.index(STAGE):
            marks.append(pytest.mark.skip(reason=f"vector tagged {tag}, later than {STAGE}"))
        params.append(pytest.param(vector, id=vector["name"], marks=marks))
    return params


@pytest.mark.parametrize("vector", _vector_params())
def test_page_plan_vectors(vector):
    packet = json.loads(json.dumps(vector["packet"]))
    plan = page_plan(packet, vector["page_counts"], vector.get("contents_pages", 1))
    assert plan == vector["expect"]
    assert packet == vector["packet"]   # the plan leaves its input alone


def test_fixture_lists_its_stage_order_and_tags():
    order = PLAN_FIXTURE["stage_order"]
    assert STAGE in order
    for vector in PLAN_FIXTURE["vectors"]:
        assert set(vector["stage"]) == {"pipeline", "model"}
        assert all(tag in order for tag in vector["stage"].values())


def test_option_defaults_match_the_fixture():
    assert DEFAULT_OPTIONS == PLAN_FIXTURE["option_defaults"]
