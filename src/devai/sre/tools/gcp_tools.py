"""GCP monitoring tools for SRE agents.

Provides read-only access to:
- Cloud Monitoring (metrics: CPU, memory, disk, network, custom)
- Cloud Logging (log queries with filters)
- Billing (cost breakdown by service)
- Recommender (cost, performance, security recommendations)
- Project info and quotas
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Graceful imports — these may not be installed in all environments
try:
    from google.cloud import monitoring_v3

    _HAS_MONITORING = True
except ImportError:
    _HAS_MONITORING = False
    logger.debug("google-cloud-monitoring not installed — gcp_get_monitoring_metrics unavailable")

try:
    from google.cloud import logging as cloud_logging

    _HAS_LOGGING = True
except ImportError:
    _HAS_LOGGING = False
    logger.debug("google-cloud-logging not installed — gcp_get_logs unavailable")

try:
    from google.cloud import recommender_v1

    _HAS_RECOMMENDER = True
except ImportError:
    _HAS_RECOMMENDER = False
    logger.debug("google-cloud-recommender not installed — gcp_get_recommendations unavailable")

try:
    from google.cloud import resourcemanager_v3

    _HAS_RESOURCE_MANAGER = True
except ImportError:
    _HAS_RESOURCE_MANAGER = False
    logger.debug("google-cloud-resource-manager not installed — gcp_get_project_info unavailable")

try:
    import httpx

    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    import google.auth
    import google.auth.transport.requests

    _HAS_GOOGLE_AUTH = True
except ImportError:
    _HAS_GOOGLE_AUTH = False


GCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "gcp_get_monitoring_metrics",
        "description": (
            "Query Google Cloud Monitoring time series data. Returns metrics like CPU utilization, "
            "memory usage, disk I/O, network traffic, or custom metrics for a given resource type "
            "and metric over a configurable time window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metric_type": {
                    "type": "string",
                    "description": (
                        "Full metric type, e.g. 'compute.googleapis.com/instance/cpu/utilization', "
                        "'kubernetes.io/container/memory/used_bytes', "
                        "'kubernetes.io/node/cpu/allocatable_utilization'"
                    ),
                },
                "resource_type": {
                    "type": "string",
                    "description": (
                        "Monitored resource type, e.g. 'gce_instance', 'k8s_container', "
                        "'k8s_node', 'k8s_pod'. Leave empty to auto-detect."
                    ),
                },
                "filter_extra": {
                    "type": "string",
                    "description": (
                        "Additional filter conditions, e.g. "
                        "'resource.labels.namespace_name=\"homechef\"'. "
                        "Appended to the base metric filter."
                    ),
                },
                "minutes": {
                    "type": "integer",
                    "description": "Look-back window in minutes (default 30, max 1440).",
                },
                "alignment_period_seconds": {
                    "type": "integer",
                    "description": "Alignment period in seconds for aggregation (default 300).",
                },
                "per_series_aligner": {
                    "type": "string",
                    "description": (
                        "Aligner: ALIGN_MEAN, ALIGN_MAX, ALIGN_MIN, ALIGN_SUM, ALIGN_RATE (default ALIGN_MEAN)."
                    ),
                },
            },
            "required": ["metric_type"],
        },
    },
    {
        "name": "gcp_get_logs",
        "description": (
            "Query Google Cloud Logging entries with a filter expression. "
            "Returns recent log entries matching the filter, formatted for readability."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter_str": {
                    "type": "string",
                    "description": (
                        "Cloud Logging filter expression, e.g. "
                        "'resource.type=\"k8s_container\" severity>=ERROR', "
                        '\'resource.labels.namespace_name="homechef" "OOMKilled"\''
                    ),
                },
                "minutes": {
                    "type": "integer",
                    "description": "Look-back window in minutes (default 60, max 1440).",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum log entries to return (default 50, max 200).",
                },
            },
            "required": ["filter_str"],
        },
    },
    {
        "name": "gcp_get_billing_summary",
        "description": (
            "Get a cost breakdown for the GCP project over the last N days. "
            "Uses Cloud Billing Budgets API or BigQuery billing export when available, "
            "with a fallback to project-level cost estimation via resource metadata."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to look back (default 7, max 90).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "gcp_get_recommendations",
        "description": (
            "Retrieve active Google Cloud Recommender insights and recommendations. "
            "Covers cost optimization, performance tuning, security hardening, and "
            "reliability improvements for Compute Engine, GKE, IAM, and more."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recommender_id": {
                    "type": "string",
                    "description": (
                        "Specific recommender ID, e.g. "
                        "'google.compute.instance.MachineTypeRecommender', "
                        "'google.container.DiagnosisRecommender'. "
                        "Leave empty to query common recommenders."
                    ),
                },
                "zone": {
                    "type": "string",
                    "description": "Zone for zonal recommenders (default: asia-south1-a).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "gcp_get_project_info",
        "description": (
            "Get basic GCP project information including project ID, name, labels, "
            "lifecycle state, and active service APIs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "gcp_get_storage_buckets",
        "description": (
            "List Cloud Storage buckets with size, location, storage class, lifecycle rules, "
            "and access control settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "gcp_get_sql_instances",
        "description": (
            "List Cloud SQL instances with version, tier, CPU/memory, disk, HA status, "
            "backup config, maintenance window, and connection info."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "gcp_get_compute_instances",
        "description": (
            "List Compute Engine VM instances with machine type, zone, status, network, disks, and labels."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone": {
                    "type": "string",
                    "description": "Zone filter (default: all zones in region). e.g. 'asia-south1-a'",
                },
            },
            "required": [],
        },
    },
    {
        "name": "gcp_get_quotas",
        "description": (
            "Get compute and API quota usage vs limits for the project. Highlights quotas approaching their limits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "Region for regional quotas (default: asia-south1).",
                },
            },
            "required": [],
        },
    },
]


class GCPToolExecutor:
    """Executes GCP monitoring tools using official Python clients with ADC auth."""

    def __init__(self) -> None:
        self._project_id = os.environ.get("GCP_PROJECT") or os.environ.get("DEVAI_GKE_PROJECT", "tesseracthub-480811")

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{tool_name}", None)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        try:
            result = await handler(tool_input)
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2, default=str)
        except Exception as exc:
            logger.warning("GCP tool %s failed: %s", tool_name, exc, exc_info=True)
            return f"Error executing {tool_name}: {exc}"

    # ------------------------------------------------------------------
    # Cloud Monitoring
    # ------------------------------------------------------------------

    async def _handle_gcp_get_monitoring_metrics(self, inp: dict[str, Any]) -> str:
        if not _HAS_MONITORING:
            return "google-cloud-monitoring is not installed. Install with: pip install google-cloud-monitoring"

        import asyncio

        metric_type = inp["metric_type"]
        resource_type = inp.get("resource_type", "")
        filter_extra = inp.get("filter_extra", "")
        minutes = min(inp.get("minutes", 30), 1440)
        alignment_seconds = inp.get("alignment_period_seconds", 300)
        aligner_name = inp.get("per_series_aligner", "ALIGN_MEAN").upper()

        # Build filter
        filter_str = f'metric.type = "{metric_type}"'
        if resource_type:
            filter_str += f' AND resource.type = "{resource_type}"'
        if filter_extra:
            filter_str += f" AND {filter_extra}"

        # Resolve aligner enum
        aligner_map = {
            "ALIGN_MEAN": monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
            "ALIGN_MAX": monitoring_v3.Aggregation.Aligner.ALIGN_MAX,
            "ALIGN_MIN": monitoring_v3.Aggregation.Aligner.ALIGN_MIN,
            "ALIGN_SUM": monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
            "ALIGN_RATE": monitoring_v3.Aggregation.Aligner.ALIGN_RATE,
        }
        aligner = aligner_map.get(aligner_name, monitoring_v3.Aggregation.Aligner.ALIGN_MEAN)

        now = datetime.now(UTC)
        start = now - timedelta(minutes=minutes)

        def _query() -> list[Any]:
            client = monitoring_v3.MetricServiceClient()
            interval = monitoring_v3.TimeInterval(
                start_time=start,
                end_time=now,
            )
            aggregation = monitoring_v3.Aggregation(
                alignment_period={"seconds": alignment_seconds},
                per_series_aligner=aligner,
            )
            request = monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{self._project_id}",
                filter=filter_str,
                interval=interval,
                aggregation=aggregation,
                view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            )
            return list(client.list_time_series(request=request))

        series_list = await asyncio.get_event_loop().run_in_executor(None, _query)

        if not series_list:
            return f"No time series data found for metric '{metric_type}' in the last {minutes} minutes."

        lines = [
            f"Metric: {metric_type}",
            f"Window: last {minutes} minutes (aligned {alignment_seconds}s, {aligner_name})",
            f"Series count: {len(series_list)}",
            "",
        ]

        for ts in series_list[:20]:  # Cap output
            labels = dict(ts.resource.labels)
            label_str = ", ".join(f"{k}={v}" for k, v in labels.items())
            lines.append(f"  Resource: {ts.resource.type} ({label_str})")

            for point in ts.points[:10]:  # Last 10 points per series
                value = point.value
                val = (
                    value.double_value
                    if value.double_value
                    else value.int64_value
                    if value.int64_value
                    else value.distribution_value.mean
                    if hasattr(value, "distribution_value") and value.distribution_value
                    else "N/A"
                )
                ts_str = point.interval.end_time.isoformat() if point.interval.end_time else "?"
                lines.append(f"    [{ts_str}] {val}")

            lines.append("")

        if len(series_list) > 20:
            lines.append(f"  ... and {len(series_list) - 20} more series (truncated)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cloud Logging
    # ------------------------------------------------------------------

    async def _handle_gcp_get_logs(self, inp: dict[str, Any]) -> str:
        if not _HAS_LOGGING:
            return "google-cloud-logging is not installed. Install with: pip install google-cloud-logging"

        import asyncio

        filter_str = inp["filter_str"]
        minutes = min(inp.get("minutes", 60), 1440)
        max_entries = min(inp.get("max_entries", 50), 200)

        # Add timestamp bound
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        full_filter = f'timestamp >= "{cutoff_str}" AND {filter_str}'

        def _query() -> list[Any]:
            client = cloud_logging.Client(project=self._project_id)
            entries = list(
                client.list_entries(
                    filter_=full_filter,
                    order_by=cloud_logging.DESCENDING,
                    max_results=max_entries,
                    resource_names=[f"projects/{self._project_id}"],
                )
            )
            return entries

        entries = await asyncio.get_event_loop().run_in_executor(None, _query)

        if not entries:
            return f"No log entries found matching filter in the last {minutes} minutes.\nFilter: {full_filter}"

        lines = [
            f"Log entries: {len(entries)} (last {minutes} minutes)",
            f"Filter: {filter_str}",
            "",
        ]

        for entry in entries:
            severity = getattr(entry, "severity", "DEFAULT")
            timestamp = getattr(entry, "timestamp", "?")
            resource = getattr(entry, "resource", None)
            resource_str = ""
            if resource:
                labels = dict(resource.labels) if resource.labels else {}
                resource_str = f" [{resource.type}: {labels.get('namespace_name', labels.get('project_id', ''))}]"

            payload = entry.payload
            if isinstance(payload, dict):
                msg = payload.get("message", payload.get("textPayload", json.dumps(payload, default=str)[:300]))
            elif isinstance(payload, str):
                msg = payload
            else:
                msg = str(payload)[:300]

            lines.append(f"  [{severity}] {timestamp}{resource_str}")
            lines.append(f"    {msg[:500]}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------

    async def _handle_gcp_get_billing_summary(self, inp: dict[str, Any]) -> str:
        days = min(inp.get("days", 7), 90)

        # Try Cloud Billing via REST API with ADC token
        if _HAS_GOOGLE_AUTH and _HAS_HTTPX:
            try:
                return await self._billing_via_rest(days)
            except Exception as exc:
                logger.debug("Billing REST API failed: %s", exc)

        # Fallback: return project info with a note
        return (
            f"Billing API not accessible for project '{self._project_id}'.\n"
            f"Requested: cost breakdown for last {days} days.\n\n"
            "To enable billing queries, ensure one of:\n"
            "  1. The service account has roles/billing.viewer on the billing account\n"
            "  2. BigQuery billing export is configured (dataset: billing_export)\n"
            "  3. Cloud Billing Budgets API is enabled\n\n"
            "You can check current billing in the GCP Console:\n"
            f"  https://console.cloud.google.com/billing?project={self._project_id}"
        )

    async def _billing_via_rest(self, days: int) -> str:
        """Query billing info via the Cloud Billing REST API using ADC."""
        import asyncio

        def _get_token() -> str:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-billing.readonly"])
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
            return credentials.token

        token = await asyncio.get_event_loop().run_in_executor(None, _get_token)

        async with httpx.AsyncClient(timeout=30) as client:
            # Get billing account linked to the project
            resp = await client.get(
                f"https://cloudbilling.googleapis.com/v1/projects/{self._project_id}/billingInfo",
                headers={"Authorization": f"Bearer {token}"},
            )

            if resp.status_code != 200:
                return (
                    f"Could not fetch billing info (HTTP {resp.status_code}).\n"
                    f"Ensure the service account has roles/billing.viewer."
                )

            billing_info = resp.json()
            billing_account = billing_info.get("billingAccountName", "unknown")
            billing_enabled = billing_info.get("billingEnabled", False)

            # Get budgets if available
            budgets_resp = await client.get(
                f"https://billingbudgets.googleapis.com/v1/{billing_account}/budgets",
                headers={"Authorization": f"Bearer {token}"},
            )

            lines = [
                f"Project: {self._project_id}",
                f"Billing Account: {billing_account}",
                f"Billing Enabled: {billing_enabled}",
                f"Period: last {days} days",
                "",
            ]

            if budgets_resp.status_code == 200:
                budgets = budgets_resp.json().get("budgets", [])
                if budgets:
                    lines.append(f"Active Budgets: {len(budgets)}")
                    for b in budgets[:10]:
                        display_name = b.get("displayName", "unnamed")
                        amount = b.get("amount", {})
                        specified = amount.get("specifiedAmount", {})
                        budget_amount = f"{specified.get('currencyCode', 'USD')} {specified.get('units', '?')}"
                        lines.append(f"  - {display_name}: {budget_amount}")
                else:
                    lines.append("No budgets configured.")
            else:
                lines.append("Budget details unavailable (Billing Budgets API may not be enabled).")

            lines.extend(
                [
                    "",
                    "Note: For detailed cost-per-service breakdown, configure BigQuery billing export",
                    "and query the billing_export dataset directly.",
                    f"Console: https://console.cloud.google.com/billing?project={self._project_id}",
                ]
            )

            return "\n".join(lines)

    # ------------------------------------------------------------------
    # Recommender
    # ------------------------------------------------------------------

    async def _handle_gcp_get_recommendations(self, inp: dict[str, Any]) -> str:
        if not _HAS_RECOMMENDER:
            return "google-cloud-recommender is not installed. Install with: pip install google-cloud-recommender"

        import asyncio

        zone = inp.get("zone", "asia-south1-a")
        specific_recommender = inp.get("recommender_id", "")

        # Common recommenders to query when none specified
        default_recommenders = [
            "google.compute.instance.MachineTypeRecommender",
            "google.compute.instance.IdleResourceRecommender",
            "google.container.DiagnosisRecommender",
            "google.iam.policy.Recommender",
        ]

        recommender_ids = [specific_recommender] if specific_recommender else default_recommenders

        def _query_recommender(rec_id: str) -> list[dict[str, Any]]:
            client = recommender_v1.RecommenderClient()
            parent = f"projects/{self._project_id}/locations/{zone}/recommenders/{rec_id}"
            try:
                recommendations = list(client.list_recommendations(request={"parent": parent}))
                results = []
                for rec in recommendations:
                    results.append(
                        {
                            "name": rec.name.split("/")[-1] if rec.name else "unknown",
                            "recommender": rec_id.split(".")[-1],
                            "priority": str(rec.priority) if hasattr(rec, "priority") else "unspecified",
                            "state": str(rec.state_info.state) if rec.state_info else "unknown",
                            "description": rec.description or "",
                            "last_refresh": str(rec.last_refresh_time) if rec.last_refresh_time else "",
                            "primary_impact": (
                                f"{rec.primary_impact.category}: "
                                f"{rec.primary_impact.cost_projection.cost.units if rec.primary_impact.cost_projection else 'N/A'}"
                            )
                            if rec.primary_impact
                            else "N/A",
                        }
                    )
                return results
            except Exception as exc:
                logger.debug("Recommender %s failed: %s", rec_id, exc)
                return []

        all_recommendations: list[dict[str, Any]] = []
        for rec_id in recommender_ids:
            recs = await asyncio.get_event_loop().run_in_executor(None, _query_recommender, rec_id)
            all_recommendations.extend(recs)

        if not all_recommendations:
            queried = ", ".join(r.split(".")[-1] for r in recommender_ids)
            return (
                f"No active recommendations found.\n"
                f"Project: {self._project_id}, Zone: {zone}\n"
                f"Queried recommenders: {queried}"
            )

        lines = [
            f"Active Recommendations: {len(all_recommendations)}",
            f"Project: {self._project_id}, Zone: {zone}",
            "",
        ]

        for rec in all_recommendations:
            lines.append(f"  [{rec['recommender']}] {rec['description']}")
            lines.append(f"    Priority: {rec['priority']} | State: {rec['state']}")
            lines.append(f"    Impact: {rec['primary_impact']}")
            if rec["last_refresh"]:
                lines.append(f"    Last refresh: {rec['last_refresh']}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Project Info
    # ------------------------------------------------------------------

    async def _handle_gcp_get_project_info(self, inp: dict[str, Any]) -> str:
        if not _HAS_RESOURCE_MANAGER:
            return (
                "google-cloud-resource-manager is not installed. "
                "Install with: pip install google-cloud-resource-manager"
            )

        import asyncio

        def _query() -> dict[str, Any]:
            client = resourcemanager_v3.ProjectsClient()
            project = client.get_project(name=f"projects/{self._project_id}")
            return {
                "project_id": project.project_id,
                "display_name": project.display_name,
                "state": str(project.state),
                "create_time": str(project.create_time) if project.create_time else "",
                "labels": dict(project.labels) if project.labels else {},
                "parent": project.parent or "",
            }

        try:
            info = await asyncio.get_event_loop().run_in_executor(None, _query)
        except Exception as exc:
            logger.warning("Failed to get project info: %s", exc)
            return (
                f"Could not retrieve project info for '{self._project_id}': {exc}\n"
                "Ensure the service account has roles/resourcemanager.projects.get permission."
            )

        lines = [
            f"Project ID: {info['project_id']}",
            f"Display Name: {info['display_name']}",
            f"State: {info['state']}",
            f"Created: {info['create_time']}",
            f"Parent: {info['parent']}",
        ]

        if info["labels"]:
            lines.append("Labels:")
            for k, v in info["labels"].items():
                lines.append(f"  {k}: {v}")

        # Try to get enabled APIs via REST
        if _HAS_GOOGLE_AUTH and _HAS_HTTPX:
            try:
                api_info = await self._get_enabled_apis()
                if api_info:
                    lines.extend(["", f"Enabled APIs ({len(api_info)}):", ""])
                    for api in api_info[:30]:
                        lines.append(f"  - {api}")
                    if len(api_info) > 30:
                        lines.append(f"  ... and {len(api_info) - 30} more")
            except Exception as exc:
                logger.debug("Failed to list APIs: %s", exc)

        return "\n".join(lines)

    async def _get_enabled_apis(self) -> list[str]:
        """List enabled APIs for the project via Service Usage REST API."""
        import asyncio

        def _get_token() -> str:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"])
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
            return credentials.token

        token = await asyncio.get_event_loop().run_in_executor(None, _get_token)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://serviceusage.googleapis.com/v1/projects/{self._project_id}/services",
                params={"filter": "state:ENABLED", "pageSize": 100},
                headers={"Authorization": f"Bearer {token}"},
            )

            if resp.status_code != 200:
                return []

            services = resp.json().get("services", [])
            return sorted(svc.get("config", {}).get("title", svc.get("name", "").split("/")[-1]) for svc in services)

    # ------------------------------------------------------------------
    # Cloud Storage
    # ------------------------------------------------------------------

    async def _handle_gcp_get_storage_buckets(self, tool_input: dict[str, Any]) -> str:
        """List Cloud Storage buckets with metadata."""

        token = await self._get_adc_token()
        if not token:
            return "Cannot authenticate to GCP — ADC token unavailable."

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://storage.googleapis.com/storage/v1/b",
                params={"project": self._project_id, "projection": "full"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return f"Failed to list buckets (HTTP {resp.status_code}): {resp.text[:200]}"

            buckets = resp.json().get("items", [])

        if not buckets:
            return "No Cloud Storage buckets found in this project."

        lines = [f"## Cloud Storage Buckets ({len(buckets)})\n"]
        for b in buckets:
            name = b.get("name", "")
            location = b.get("location", "")
            storage_class = b.get("storageClass", "")
            created = b.get("timeCreated", "")[:10]
            versioning = "enabled" if b.get("versioning", {}).get("enabled") else "disabled"
            lifecycle_rules = len(b.get("lifecycle", {}).get("rule", []))

            lines.append(f"**{name}**")
            lines.append(f"  Location: {location} | Class: {storage_class} | Created: {created}")
            lines.append(f"  Versioning: {versioning} | Lifecycle rules: {lifecycle_rules}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cloud SQL
    # ------------------------------------------------------------------

    async def _handle_gcp_get_sql_instances(self, tool_input: dict[str, Any]) -> str:
        """List Cloud SQL instances with configuration details."""
        token = await self._get_adc_token()
        if not token:
            return "Cannot authenticate to GCP — ADC token unavailable."

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://sqladmin.googleapis.com/v1/projects/{self._project_id}/instances",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return f"Failed to list SQL instances (HTTP {resp.status_code}): {resp.text[:200]}"

            instances = resp.json().get("items", [])

        if not instances:
            return "No Cloud SQL instances found in this project."

        lines = [f"## Cloud SQL Instances ({len(instances)})\n"]
        for inst in instances:
            name = inst.get("name", "")
            db_version = inst.get("databaseVersion", "")
            state = inst.get("state", "")
            tier = inst.get("settings", {}).get("tier", "")
            region = inst.get("region", "")
            ha = inst.get("settings", {}).get("availabilityType", "ZONAL")
            disk_size = inst.get("settings", {}).get("dataDiskSizeGb", "?")
            disk_type = inst.get("settings", {}).get("dataDiskType", "?")
            backup = inst.get("settings", {}).get("backupConfiguration", {})
            backup_enabled = "enabled" if backup.get("enabled") else "disabled"
            ip_addresses = [ip.get("ipAddress", "") for ip in inst.get("ipAddresses", [])]
            maint = inst.get("settings", {}).get("maintenanceWindow", {})
            maint_day = maint.get("day", "?")
            maint_hour = maint.get("hour", "?")

            lines.append(f"**{name}** ({db_version})")
            lines.append(f"  State: {state} | Region: {region} | Tier: {tier}")
            lines.append(f"  HA: {ha} | Disk: {disk_size}GB ({disk_type})")
            lines.append(f"  Backup: {backup_enabled} | Maintenance: day {maint_day}, hour {maint_hour}")
            if ip_addresses:
                lines.append(f"  IPs: {', '.join(ip_addresses)}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Compute Engine instances
    # ------------------------------------------------------------------

    async def _handle_gcp_get_compute_instances(self, tool_input: dict[str, Any]) -> str:
        """List Compute Engine VM instances."""
        zone = tool_input.get("zone", "")
        token = await self._get_adc_token()
        if not token:
            return "Cannot authenticate to GCP — ADC token unavailable."

        if zone:
            url = f"https://compute.googleapis.com/compute/v1/projects/{self._project_id}/zones/{zone}/instances"
        else:
            url = f"https://compute.googleapis.com/compute/v1/projects/{self._project_id}/aggregated/instances"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                return f"Failed to list instances (HTTP {resp.status_code}): {resp.text[:200]}"

            data = resp.json()

        # Handle aggregated response
        instances: list[dict[str, Any]] = []
        if "items" in data and isinstance(data["items"], dict):
            for zone_data in data["items"].values():
                instances.extend(zone_data.get("instances", []))
        elif "items" in data and isinstance(data["items"], list):
            instances = data["items"]

        if not instances:
            return "No Compute Engine instances found."

        lines = [f"## Compute Engine Instances ({len(instances)})\n"]
        for vm in instances:
            name = vm.get("name", "")
            status = vm.get("status", "")
            machine_type = vm.get("machineType", "").split("/")[-1]
            vm_zone = vm.get("zone", "").split("/")[-1]
            labels = vm.get("labels", {})
            network = ""
            for nic in vm.get("networkInterfaces", []):
                network = nic.get("network", "").split("/")[-1]

            lines.append(f"**{name}** — {status}")
            lines.append(f"  Zone: {vm_zone} | Type: {machine_type} | Network: {network}")
            if labels:
                lines.append(f"  Labels: {', '.join(f'{k}={v}' for k, v in labels.items())}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Quotas
    # ------------------------------------------------------------------

    async def _handle_gcp_get_quotas(self, tool_input: dict[str, Any]) -> str:
        """Get compute quota usage and limits."""
        region = tool_input.get("region", "asia-south1")
        token = await self._get_adc_token()
        if not token:
            return "Cannot authenticate to GCP — ADC token unavailable."

        lines = ["## Quota Usage\n"]

        async with httpx.AsyncClient(timeout=30) as client:
            # Project-level quotas
            resp = await client.get(
                f"https://compute.googleapis.com/compute/v1/projects/{self._project_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                quotas = resp.json().get("quotas", [])
                critical = [q for q in quotas if q.get("usage", 0) > 0]
                critical.sort(key=lambda q: q.get("usage", 0) / max(q.get("limit", 1), 1), reverse=True)

                if critical:
                    lines.append("### Project Quotas (in use)\n")
                    lines.append("| Metric | Usage | Limit | % Used |")
                    lines.append("|--------|-------|-------|--------|")
                    for q in critical[:20]:
                        metric = q.get("metric", "")
                        usage = q.get("usage", 0)
                        limit = q.get("limit", 0)
                        pct = (usage / limit * 100) if limit > 0 else 0
                        flag = " ⚠️" if pct > 80 else ""
                        lines.append(f"| {metric} | {usage:.0f} | {limit:.0f} | {pct:.1f}%{flag} |")
                    lines.append("")

            # Regional quotas
            resp = await client.get(
                f"https://compute.googleapis.com/compute/v1/projects/{self._project_id}/regions/{region}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                quotas = resp.json().get("quotas", [])
                critical = [q for q in quotas if q.get("usage", 0) > 0]
                critical.sort(key=lambda q: q.get("usage", 0) / max(q.get("limit", 1), 1), reverse=True)

                if critical:
                    lines.append(f"### Regional Quotas ({region})\n")
                    lines.append("| Metric | Usage | Limit | % Used |")
                    lines.append("|--------|-------|-------|--------|")
                    for q in critical[:20]:
                        metric = q.get("metric", "")
                        usage = q.get("usage", 0)
                        limit = q.get("limit", 0)
                        pct = (usage / limit * 100) if limit > 0 else 0
                        flag = " ⚠️" if pct > 80 else ""
                        lines.append(f"| {metric} | {usage:.0f} | {limit:.0f} | {pct:.1f}%{flag} |")

        return "\n".join(lines) if len(lines) > 2 else "No quota usage data available."

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _get_adc_token(self) -> str | None:
        """Get ADC access token for REST API calls."""
        if not _HAS_GOOGLE_AUTH:
            return None
        import asyncio

        def _get_token() -> str:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform.read-only"])
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
            return credentials.token

        try:
            return await asyncio.get_event_loop().run_in_executor(None, _get_token)
        except Exception as exc:
            logger.warning("Failed to get ADC token: %s", exc)
            return None
