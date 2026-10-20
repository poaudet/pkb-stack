import pytest

from hub.capture import jobs, plan


def test_plan_schema_error_reports_only_paths_and_keywords():
    raw_plan = {
        "summary": "private summary " * 20,
        "questions": ["private question"] * 6,
        "ops": [
            {"id": "o1", "op": "create_note", "ref": "tremblay-move"},
            {"op": "create_entity"},
        ],
    }

    with pytest.raises(plan.PlanSchemaError) as caught:
        plan.validate(raw_plan)

    assert caught.value.issues == [
        ("summary", "maxLength"),
        ("questions", "maxItems"),
        ("ops.0.op", "enum"),
        ("ops.0.ref", "pattern"),
        ("ops.1.id", "required"),
    ]
    assert "private" not in str(caught.value)
    assert "create_note" not in str(caught.value)
    assert "tremblay-move" not in str(caught.value)


def test_parse_error_does_not_include_reply_values():
    reply = 'truncated private model reply {"name":"sensitive'

    with pytest.raises(jobs.PlanParseError) as caught:
        jobs._parse_reply(reply)

    assert "private" not in str(caught.value)
    assert "sensitive" not in str(caught.value)


def test_operation_limit_has_a_distinct_error():
    with pytest.raises(plan.OperationLimitError) as caught:
        plan.normalize(None, {"ops": [{}, {}]}, "", [], max_ops=1)

    assert caught.value.count == 2
    assert caught.value.limit == 1
