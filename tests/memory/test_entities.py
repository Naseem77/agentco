from agentco.memory.entities import ENTITY_TYPES
from agentco.memory.scopes import Scope

# Mirrors graphiti_core.nodes.EntityNode.model_fields in the installed version.
RESERVED = {
    "uuid",
    "name",
    "group_id",
    "labels",
    "created_at",
    "summary",
    "attributes",
    "name_embedding",
}


def test_entity_types_cover_spec():
    assert set(ENTITY_TYPES) == set(Scope)
    assert set(ENTITY_TYPES[Scope.COMPANY]) == {
        "Policy",
        "Convention",
        "EscalationRule",
        "HumanDecision",
    }
    assert set(ENTITY_TYPES[Scope.PRODUCT]) == {
        "Feature",
        "Spec",
        "AcceptanceCriterion",
        "Customer",
        "Feedback",
    }
    assert set(ENTITY_TYPES[Scope.RND]) == {
        "Module",
        "File",
        "Function",
        "Class",
        "Issue",
        "PullRequest",
        "ReviewComment",
        "ReviewRule",
        "Learning",
        "Playbook",
        "Risk",
        "Decision",
        "AgentProfile",
    }
    assert set(ENTITY_TYPES[Scope.QA]) == {
        "TestCase",
        "TestRun",
        "FlakyTest",
        "EscapedBug",
        "QALearning",
        "NeighbourCase",
    }


def test_registry_keys_match_class_names():
    for types in ENTITY_TYPES.values():
        for name, model in types.items():
            assert name == model.__name__


def test_no_reserved_field_names():
    for types in ENTITY_TYPES.values():
        for model in types.values():
            assert not RESERVED & set(model.model_fields), model.__name__


def test_every_entity_has_docstring():
    for types in ENTITY_TYPES.values():
        for model in types.values():
            assert (model.__doc__ or "").strip(), model.__name__


def test_all_fields_are_optional():
    for types in ENTITY_TYPES.values():
        for model in types.values():
            for name, field in model.model_fields.items():
                assert not field.is_required(), f"{model.__name__}.{name}"


def test_defined_fields():
    rnd, company, qa = (ENTITY_TYPES[s] for s in (Scope.RND, Scope.COMPANY, Scope.QA))
    assert set(rnd["ReviewRule"].model_fields) == {"module_path", "origin"}
    assert set(company["EscalationRule"].model_fields) == {"category"}
    assert set(rnd["Issue"].model_fields) == {"number"}
    assert set(rnd["PullRequest"].model_fields) == {"number"}
    assert set(rnd["Module"].model_fields) == {"path"}
    assert set(rnd["File"].model_fields) == {"path"}
    assert set(qa["NeighbourCase"].model_fields) == {"bug_class"}


def test_literal_fields_reject_unknown_values():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ENTITY_TYPES[Scope.RND]["ReviewRule"](origin="robot")
    with pytest.raises(ValidationError):
        ENTITY_TYPES[Scope.COMPANY]["EscalationRule"](category="meh")
    assert ENTITY_TYPES[Scope.RND]["ReviewRule"](origin="lead").origin == "lead"
