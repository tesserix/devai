"""Tests for the specialization (skills) layer.

Four areas of coverage:

1. **Loader** — YAML → Specialization parsing, strict on shape, lenient on
   defaults. Rejects unknown keys, bad types, malformed handover_schema.

2. **Catalog** — every shipped YAML parses, no name collisions, every
   `legacy_python_class:` reference is at least a syntactically valid
   module path.

3. **Validator** — `validate_handover` catches missing fields, wrong
   types, and empty-when-required. Strict mode raises.

4. **Pipeline integration** — `run_specialization` stage resolves a spec
   from the registry on StageDeps.extra and degrades cleanly when the
   spec isn't found.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devai.specializations import (
    HandoverField,
    LLMProvider,
    RiskLevel,
    Specialization,
    SpecializationLoadError,
    SpecializationRegistry,
    discover_specializations,
    load_specialization_from_string,
    validate_handover,
)
from devai.specializations.validator import (
    HandoverValidationError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SPECS_DIR = REPO_ROOT / "specializations"


# ─────────────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────────────


def test_loader_minimal_yaml_parses():
    spec = load_specialization_from_string(
        """
name: tiny
description: just a name
"""
    )
    assert spec.name == "tiny"
    assert spec.output_key == "tiny_output"  # default
    assert spec.llm_provider == LLMProvider.AUTO
    assert spec.risk_level == RiskLevel.MEDIUM


def test_loader_full_yaml_parses():
    spec = load_specialization_from_string(
        """
name: full
display_name: Full Spec
description: every field exercised
category: planning
llm_provider: openai
llm_model: gpt-4.1
temperature: 0.3
max_tokens: 4096
allowed_tools:
  - scm_read_file
  - scm_create_issue
max_turns: 25
timeout: 10m
context_keys:
  - requirements
  - previous_output
output_key: my_output
risk_level: high
role_color: product
legacy_python_class: devai.agents.foo.FooAgent
system_prompt: |
  You are a test role.
handover_schema:
  title:
    type: string
    required: true
  number:
    type: integer
    required: false
  tags: array
metadata:
  owner: test
"""
    )
    assert spec.name == "full"
    assert spec.category == "planning"
    assert spec.llm_provider == LLMProvider.OPENAI
    assert spec.llm_model == "gpt-4.1"
    assert spec.temperature == 0.3
    assert spec.max_tokens == 4096
    assert spec.max_turns == 25
    assert spec.timeout_seconds == 600  # 10m
    assert spec.risk_level == RiskLevel.HIGH
    assert spec.output_key == "my_output"
    assert spec.handover_schema["title"].type == "string"
    assert spec.handover_schema["title"].required is True
    assert spec.handover_schema["number"].required is False
    assert spec.handover_schema["tags"].type == "array"
    assert spec.metadata == {"owner": "test"}


def test_loader_rejects_unknown_top_key():
    with pytest.raises(SpecializationLoadError, match="unknown top-level"):
        load_specialization_from_string("name: x\nfoo: bar\n")


def test_loader_rejects_missing_name():
    with pytest.raises(SpecializationLoadError, match="missing or invalid 'name'"):
        load_specialization_from_string("description: no name\n")


def test_loader_rejects_invalid_name_format():
    with pytest.raises(SpecializationLoadError, match="snake_case"):
        load_specialization_from_string("name: BadName\n")


def test_loader_rejects_invalid_handover_type():
    with pytest.raises(SpecializationLoadError, match="type must be one of"):
        load_specialization_from_string(
            """
name: bad
handover_schema:
  x:
    type: int   # not a valid type — should be 'integer'
    required: true
"""
        )


def test_loader_unknown_provider_falls_back_to_auto():
    spec = load_specialization_from_string("name: x\nllm_provider: invented_provider\n")
    assert spec.llm_provider == LLMProvider.AUTO


def test_loader_duration_parsing():
    spec = load_specialization_from_string("name: x\ntimeout: 2h\n")
    assert spec.timeout_seconds == 7200

    spec2 = load_specialization_from_string("name: x\ntimeout: 30s\n")
    assert spec2.timeout_seconds == 30

    spec3 = load_specialization_from_string("name: x\ntimeout: 500ms\n")
    assert spec3.timeout_seconds == 0  # int rounds down — that's fine


# ─────────────────────────────────────────────────────────────────────
# Catalog: every shipped YAML
# ─────────────────────────────────────────────────────────────────────


def test_catalog_loads_every_shipped_spec():
    specs = discover_specializations(SPECS_DIR)
    assert len(specs) >= 20, f"expected at least 20 specs, got {len(specs)}"


def test_catalog_has_expected_role_names():
    specs = discover_specializations(SPECS_DIR)
    expected_alm = {
        "document_analyzer",
        "tech_detector",
        "requirements_analyst",
        "product_director",
        "engineering_manager",
        "senior_developer",
        "db_engineer",
        "staff_reviewer",
        "security_expert",
        "ci_monitor",
        "qa_tester",
        "infra_provisioner",
        "release_manager",
    }
    expected_sre = {
        "discovery",
        "infra_monitor",
        "perf_monitor",
        "log_analyzer",
        "cost_analyzer",
        "capacity_planner",
        "incident_responder",
    }
    expected_new = {"prototyper", "intake", "negotiator", "reflector"}
    missing = (expected_alm | expected_sre | expected_new) - set(specs.keys())
    assert not missing, f"missing specs: {missing}"


def test_catalog_no_name_collisions():
    specs = discover_specializations(SPECS_DIR)
    names = list(specs.keys())
    assert len(names) == len(set(names)), "duplicate names in catalog"


def test_catalog_categories_are_known():
    specs = discover_specializations(SPECS_DIR)
    expected = {"planning", "coding", "review", "orchestration", "sre", "specialist"}
    for spec in specs.values():
        assert spec.category in expected, f"unknown category {spec.category!r} in {spec.name}"


def test_catalog_handover_schemas_typecheck():
    specs = discover_specializations(SPECS_DIR)
    for spec in specs.values():
        for field_name, field in spec.handover_schema.items():
            assert isinstance(field, HandoverField), f"{spec.name}.{field_name}"
            assert field.type in {"string", "integer", "number", "boolean", "array", "object", "any"}


def test_catalog_legacy_bridges_are_well_formed_paths():
    specs = discover_specializations(SPECS_DIR)
    for spec in specs.values():
        if not spec.legacy_python_class:
            continue
        assert "." in spec.legacy_python_class
        module, class_name = spec.legacy_python_class.rsplit(".", 1)
        assert module.startswith("devai.")
        assert class_name[0].isupper(), f"{spec.name}: expected class name, got {class_name!r}"


# ─────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────


def _spec_with_schema(**schema) -> Specialization:
    return Specialization(
        name="t",
        handover_schema={
            k: HandoverField(name=k, type=v.get("type", "any"), required=v.get("required", True))
            for k, v in schema.items()
        },
    )


def test_validate_handover_clean():
    spec = _spec_with_schema(branch_name={"type": "string"}, pr_number={"type": "integer"})
    violations = validate_handover(spec, {"branch_name": "story/42", "pr_number": 42})
    assert violations == []


def test_validate_handover_missing_required():
    spec = _spec_with_schema(branch_name={"type": "string"})
    violations = validate_handover(spec, {})
    assert len(violations) == 1
    assert violations[0].field == "branch_name"
    assert violations[0].reason == "missing"


def test_validate_handover_wrong_type():
    spec = _spec_with_schema(pr_number={"type": "integer"})
    violations = validate_handover(spec, {"pr_number": "42"})  # string, not int
    assert len(violations) == 1
    assert violations[0].reason == "wrong_type"
    assert violations[0].expected == "integer"
    assert violations[0].got == "str"


def test_validate_handover_optional_field_can_be_missing():
    spec = _spec_with_schema(
        branch_name={"type": "string"},
        committed_files={"type": "array", "required": False},
    )
    violations = validate_handover(spec, {"branch_name": "x"})
    assert violations == []


def test_validate_handover_strict_raises():
    spec = _spec_with_schema(pr_number={"type": "integer"})
    with pytest.raises(HandoverValidationError) as exc:
        validate_handover(spec, {}, strict=True)
    assert exc.value.spec_name == "t"
    assert len(exc.value.violations) == 1


def test_validate_handover_data_not_dict_when_schema_present():
    spec = _spec_with_schema(x={"type": "string"})
    violations = validate_handover(spec, "not a dict")
    assert any(v.field == "<root>" for v in violations)


def test_validate_handover_require_non_empty():
    spec = _spec_with_schema(title={"type": "string"})
    # Empty string with require_non_empty=True is a violation
    violations = validate_handover(spec, {"title": ""}, require_non_empty=True)
    assert len(violations) == 1
    assert violations[0].reason == "empty"


def test_handover_field_matches_types():
    f_int = HandoverField(name="x", type="integer")
    assert f_int.matches(5)
    assert not f_int.matches(5.0)
    assert not f_int.matches(True)  # bools aren't ints

    f_any = HandoverField(name="x", type="any")
    assert f_any.matches(None)
    assert f_any.matches({})
    assert f_any.matches("anything")


# ─────────────────────────────────────────────────────────────────────
# Registry
# ─────────────────────────────────────────────────────────────────────


def test_registry_from_directory_loads_catalog():
    reg = SpecializationRegistry.from_directory(SPECS_DIR)
    assert "senior_developer" in reg
    assert "discovery" in reg
    assert len(reg) >= 20


def test_registry_resolve_returns_spec():
    reg = SpecializationRegistry.from_directory(SPECS_DIR)
    spec = reg.resolve("senior_developer")
    assert spec.name == "senior_developer"
    assert spec.category == "coding"
    assert spec.legacy_python_class.endswith(".SeniorDeveloperAgent")


def test_registry_resolve_unknown_raises():
    from devai.specializations.registry import SpecializationRegistryError

    reg = SpecializationRegistry.from_directory(SPECS_DIR)
    with pytest.raises(SpecializationRegistryError):
        reg.resolve("no_such_role")


def test_registry_by_category_groups():
    reg = SpecializationRegistry.from_directory(SPECS_DIR)
    sre_specs = reg.by_category("sre")
    assert len(sre_specs) == 7
    coding_specs = reg.by_category("coding")
    assert {s.name for s in coding_specs} >= {"senior_developer", "db_engineer"}


# ─────────────────────────────────────────────────────────────────────
# Pipeline integration — run_specialization stage
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_specialization_stage_unknown_spec_returns_stub():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.specialization import run_specialization_stage
    from devai.pipeline.types import DevAITask

    class _Cfg:
        specializations_dir = str(SPECS_DIR)

    deps = StageDeps(config=_Cfg(), extra={"specialization_registry": SpecializationRegistry()})
    stage = run_specialization_stage(deps, {"specialization": "no_such_role"})
    task = DevAITask(intent="x")
    result = await stage.execute(task)
    assert "no_such_role_error" in result.data
    assert result.data["no_such_role_error"] == "not_in_catalog"


@pytest.mark.asyncio
async def test_run_specialization_yaml_only_degrades_without_llm():
    # A YAML-only spec now runs via the LLM adapter; with no adapter wired
    # it degrades to a clear stub rather than the old "not implemented".
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.specialization import run_specialization_stage
    from devai.pipeline.types import DevAITask

    reg = SpecializationRegistry.from_directory(SPECS_DIR)

    class _Cfg:
        specializations_dir = str(SPECS_DIR)

    deps = StageDeps(config=_Cfg(), extra={"specialization_registry": reg})  # llm=None
    stage = run_specialization_stage(deps, {"specialization": "intake"})  # yaml-only
    task = DevAITask(intent="add /health endpoint")
    result = await stage.execute(task)
    payload = result.data["intake_output"]
    assert payload["stub"] is True
    assert payload["reason"] == "no_llm_adapter"
    assert payload["spec_name"] == "intake"


@pytest.mark.asyncio
async def test_run_specialization_legacy_bridge_without_deps_returns_stub():
    """Legacy bridge spec with no SCM/StateManager available — should
    short-circuit, not crash."""
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.specialization import run_specialization_stage
    from devai.pipeline.types import DevAITask

    reg = SpecializationRegistry.from_directory(SPECS_DIR)

    class _Cfg:
        specializations_dir = str(SPECS_DIR)

    deps = StageDeps(config=_Cfg(), extra={"specialization_registry": reg})
    stage = run_specialization_stage(deps, {"specialization": "tech_detector"})
    task = DevAITask(intent="detect stack", repo="org/repo")
    result = await stage.execute(task)
    assert "tech_detector_output" in result.data
    stub = result.data["tech_detector_output"]
    assert stub.get("tech_detector_stub") is True or stub.get("stub") is True


@pytest.mark.asyncio
async def test_run_specialization_requires_config_name():
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.specialization import run_specialization_stage

    deps = StageDeps(config=type("C", (), {})())  # bare object
    with pytest.raises(ValueError, match="config.specialization"):
        run_specialization_stage(deps, {})


@pytest.mark.asyncio
async def test_run_specialization_risk_levels_drive_next_state():
    """High and critical risk roles park in AWAITING_APPROVAL."""
    from devai.pipeline.interfaces import StageDeps
    from devai.pipeline.stages.specialization import run_specialization_stage
    from devai.pipeline.types import DevAITask, TaskState

    reg = SpecializationRegistry.from_directory(SPECS_DIR)

    class _Cfg:
        specializations_dir = str(SPECS_DIR)

    deps = StageDeps(config=_Cfg(), extra={"specialization_registry": reg})

    # release_manager is critical → AWAITING_APPROVAL on stub
    stage = run_specialization_stage(deps, {"specialization": "release_manager"})
    task = DevAITask(intent="release")
    result = await stage.execute(task)
    assert result.next_state == TaskState.AWAITING_APPROVAL


# ─────────────────────────────────────────────────────────────────────
# Specialization registered as a stage in the registry
# ─────────────────────────────────────────────────────────────────────


def test_run_specialization_is_in_stage_registry():
    from devai.blueprint.registry import StageRegistry, register_defaults

    reg = StageRegistry()
    register_defaults(reg)
    assert "run_specialization" in reg.known_stages()
