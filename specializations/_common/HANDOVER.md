# Handover Contract

Every specialization in this directory declares a `handover_schema` — the shape of the dict the role MUST produce. The pipeline executor validates each role's output against this schema before passing it to the next role.

## Why this matters

The chain of roles in `alm-pipeline.yaml` is:

```
document_analyzer → tech_detector → requirements_analyst →
  product_director (epic) → product_director (stories) →
  engineering_manager → senior_developer → db_engineer →
  staff_reviewer → security_expert → ci_monitor → qa_tester →
  infra_provisioner → release_manager
```

Each downstream role reads specific keys from `agent_context`. If `requirements_analyst` doesn't produce `requirements: list[dict]` then `product_director` has nothing to work from. The handover_schema is what makes this contract explicit.

## Schema syntax

Each entry in `handover_schema` has three fields:

```yaml
handover_schema:
  branch_name:
    type: string         # string | integer | number | boolean | array | object | any
    required: true
    description: The feature branch the implementation was pushed to.
  pr_number:
    type: integer
    required: true
  committed_files:
    type: array
    required: false      # optional — present only if the role created any
```

Shorthand: `field_name: string` is sugar for `{type: string, required: true}`.

## Output key convention

Each role writes its produced dict to one key in `task.agent_context`:

```
requirements_analyst    →   requirements_analyst_output
product_director        →   product_director_output
engineering_manager     →   engineering_manager_output
senior_developer        →   senior_developer_output
...
```

The `output_key:` field in each spec sets this. The default is `<name>_output`.

## Risk levels

Roles declare `risk_level: low | medium | high | critical`. The executor reads this and:

- **low / medium** — auto-advance to the next stage on success.
- **high / critical** — pause for human review (`StateAwaitingApproval`) before continuing.

Production deploys (`release_manager`) and DB changes (`db_engineer`) are typically `high`. Pure analysis stages (`document_analyzer`, `tech_detector`) are `low`.
