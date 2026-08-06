from dataclasses import asdict, dataclass
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import re
from typing import Any, Optional


MONEY_ZERO = Decimal("0.00")


@dataclass(frozen=True)
class CostBreakdown:
    compute: Decimal = MONEY_ZERO
    network: Decimal = MONEY_ZERO
    storage: Decimal = MONEY_ZERO
    database: Decimal = MONEY_ZERO
    other: Decimal = MONEY_ZERO

    @property
    def total(self) -> Decimal:
        return (
            self.compute
            + self.network
            + self.storage
            + self.database
            + self.other
        )


@dataclass(frozen=True)
class AggregateUsage:
    requests: int = 0
    network_bytes: int = 0
    storage_byte_days: int = 0
    compute_units: Decimal = MONEY_ZERO
    database_units: Decimal = MONEY_ZERO
    turn_relay_bytes: int = 0


@dataclass(frozen=True)
class OrganizationBillingSummary:
    legal_name: str
    designator: str
    hostname: str
    primary_admin_name: str
    primary_admin_email: str
    account_status: str
    provisioning_status: str
    billing_mode: str
    trial_ends_at: Optional[datetime]
    credit_balance: Decimal
    month_to_date_cost: CostBreakdown
    primary_admin_postal_address: str = ""
    primary_admin_phone: str = ""
    aggregate_usage: AggregateUsage = AggregateUsage()


@dataclass(frozen=True)
class PlatformBillingSnapshot:
    generated_at: datetime
    billing_data_through: datetime
    actual_cost_mtd: Decimal
    actual_cost_breakdown_mtd: CostBreakdown
    attributed_cost_mtd: Decimal
    unallocated_cost_mtd: Decimal
    forecast_cost: Decimal
    collected_mtd: Decimal
    organizations: tuple[OrganizationBillingSummary, ...]
    is_illustrative: bool = True
    source_status: str = "illustrative"
    source_name: str = "Illustrative prototype"
    source_message: str = ""
    organizations_are_illustrative: bool = True
    billing_period: str = ""
    billing_period_is_current: bool = True
    billing_data_stale: bool = False
    billing_data_age_hours: Optional[int] = None


PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
DATASET_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
BILLING_TABLE_PREFIXES = (
    "gcp_billing_export_resource_v1_",
    "gcp_billing_export_v1_",
)


def build_illustrative_platform_snapshot(
    now: Optional[datetime] = None,
) -> PlatformBillingSnapshot:
    """Return non-production data for reviewing the platform-admin workflow."""
    generated_at = now or datetime.now(tz=UTC)
    organizations = (
        OrganizationBillingSummary(
            legal_name="North County Search and Rescue",
            designator="NCSSAR",
            hostname="r2c-tracker.com/ncssar",
            primary_admin_name="Site administrator",
            primary_admin_email="admin@example.invalid",
            account_status="trial",
            provisioning_status="ready",
            billing_mode="shadow billing",
            trial_ends_at=generated_at + timedelta(days=18),
            credit_balance=Decimal("0.00"),
            month_to_date_cost=CostBreakdown(
                compute=Decimal("0.18"),
                network=Decimal("0.05"),
                storage=Decimal("0.03"),
                database=Decimal("0.11"),
                other=Decimal("0.02"),
            ),
        ),
        OrganizationBillingSummary(
            legal_name="Example County SAR",
            designator="EXSAR",
            hostname="r2c-tracker.com/exsar",
            primary_admin_name="Pending activation",
            primary_admin_email="new-admin@example.invalid",
            account_status="pending",
            provisioning_status="activation pending",
            billing_mode="30-day trial",
            trial_ends_at=None,
            credit_balance=Decimal("0.00"),
            month_to_date_cost=CostBreakdown(),
        ),
        OrganizationBillingSummary(
            legal_name="Regional Search and Rescue Demonstration",
            designator="DEMO",
            hostname="r2c-tracker.com/demo",
            primary_admin_name="Billing administrator",
            primary_admin_email="billing@example.invalid",
            account_status="active",
            provisioning_status="ready",
            billing_mode="prepaid usage",
            trial_ends_at=None,
            credit_balance=Decimal("42.75"),
            month_to_date_cost=CostBreakdown(
                compute=Decimal("1.32"),
                network=Decimal("4.88"),
                storage=Decimal("0.44"),
                database=Decimal("0.69"),
                other=Decimal("0.17"),
            ),
        ),
    )
    attributed = sum(
        (organization.month_to_date_cost.total for organization in organizations),
        MONEY_ZERO,
    )
    unallocated = Decimal("0.84")
    actual = attributed + unallocated
    return PlatformBillingSnapshot(
        generated_at=generated_at,
        billing_data_through=generated_at - timedelta(hours=8),
        actual_cost_mtd=actual,
        actual_cost_breakdown_mtd=CostBreakdown(
            compute=Decimal("1.50"),
            network=Decimal("4.93"),
            storage=Decimal("0.47"),
            database=Decimal("0.80"),
            other=actual - Decimal("7.70"),
        ),
        attributed_cost_mtd=attributed,
        unallocated_cost_mtd=unallocated,
        forecast_cost=Decimal("18.40"),
        collected_mtd=Decimal("10.00"),
        organizations=organizations,
        billing_period=generated_at.strftime("%Y-%m"),
    )


def build_pending_platform_snapshot(
    message: str,
    now: Optional[datetime] = None,
    *,
    source_status: str = "pending",
) -> PlatformBillingSnapshot:
    """Return an honest zero-value snapshot while live billing is unavailable."""
    generated_at = now or datetime.now(tz=UTC)
    return PlatformBillingSnapshot(
        generated_at=generated_at,
        billing_data_through=generated_at,
        actual_cost_mtd=MONEY_ZERO,
        actual_cost_breakdown_mtd=CostBreakdown(),
        attributed_cost_mtd=MONEY_ZERO,
        unallocated_cost_mtd=MONEY_ZERO,
        forecast_cost=MONEY_ZERO,
        collected_mtd=MONEY_ZERO,
        organizations=(),
        is_illustrative=False,
        source_status=source_status,
        source_name="Google Cloud Billing export",
        source_message=message,
        organizations_are_illustrative=False,
        billing_period=generated_at.strftime("%Y-%m"),
        billing_period_is_current=False,
        billing_data_stale=True,
    )


def _decimal(value: Any) -> Decimal:
    if value is None:
        return MONEY_ZERO
    return Decimal(str(value)).quantize(Decimal("0.000001"))


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name)


def _forecast_cost(actual: Decimal, through: datetime) -> Decimal:
    days_in_month = monthrange(through.year, through.month)[1]
    elapsed_days = max(
        Decimal("1"),
        Decimal(through.day - 1)
        + (
            Decimal(through.hour * 3600 + through.minute * 60 + through.second)
            / Decimal(86400)
        ),
    )
    return (actual * Decimal(days_in_month) / elapsed_days).quantize(
        Decimal("0.01")
    )


def allocate_platform_costs(
    actual_costs: CostBreakdown,
    usage_by_organization: dict[str, AggregateUsage],
) -> tuple[dict[str, CostBreakdown], Decimal]:
    """Allocate live costs by usage, with registered organizations sharing overhead.

    Metered categories are proportional to privacy-safe usage. Categories with
    no measured usage, plus miscellaneous costs, are split equally. The result
    is intentionally not written to the billing ledger: operators can compare
    the shadow allocation with the Google bill before it affects credit.
    """
    if not usage_by_organization:
        return {}, actual_costs.total

    weights = {
        "compute": {
            organization_id: Decimal(usage.compute_units)
            for organization_id, usage in usage_by_organization.items()
        },
        "network": {
            organization_id: Decimal(
                usage.network_bytes + usage.turn_relay_bytes
            )
            for organization_id, usage in usage_by_organization.items()
        },
        "storage": {
            organization_id: Decimal(usage.storage_byte_days)
            for organization_id, usage in usage_by_organization.items()
        },
        "database": {
            organization_id: Decimal(usage.database_units)
            for organization_id, usage in usage_by_organization.items()
        },
    }
    allocated_values = {
        organization_id: {
            "compute": MONEY_ZERO,
            "network": MONEY_ZERO,
            "storage": MONEY_ZERO,
            "database": MONEY_ZERO,
            "other": MONEY_ZERO,
        }
        for organization_id in usage_by_organization
    }
    organization_ids = sorted(usage_by_organization)

    def assign_category(category: str, category_weights: dict[str, Decimal]) -> None:
        total_weight = sum(category_weights.values(), Decimal("0"))
        category_cost = getattr(actual_costs, category)
        if total_weight > 0:
            effective_weights = category_weights
            effective_total = total_weight
        else:
            effective_weights = {
                organization_id: Decimal("1")
                for organization_id in organization_ids
            }
            effective_total = Decimal(len(organization_ids))
        for organization_id, weight in effective_weights.items():
            allocated_values[organization_id][category] = (
                category_cost * weight / effective_total
            ).quantize(Decimal("0.000001"))

        # Keep the category fully reconciled despite sub-cent rounding.
        assigned = sum(
            (
                allocated_values[organization_id][category]
                for organization_id in organization_ids
            ),
            MONEY_ZERO,
        )
        allocated_values[organization_ids[0]][category] += category_cost - assigned

    for category, category_weights in weights.items():
        assign_category(category, category_weights)
    assign_category(
        "other",
        {organization_id: Decimal("1") for organization_id in organization_ids},
    )

    allocations = {
        organization_id: CostBreakdown(**values)
        for organization_id, values in allocated_values.items()
    }
    attributed = sum(
        (allocation.total for allocation in allocations.values()),
        MONEY_ZERO,
    )
    return allocations, actual_costs.total - attributed


class BigQueryBillingSnapshotProvider:
    """Read aggregate R2C costs without accessing tenant operational data."""

    def __init__(
        self,
        client: Any,
        export_project: str,
        export_dataset: str,
        included_project_ids: tuple[str, ...],
    ):
        if not PROJECT_ID_RE.fullmatch(export_project):
            raise ValueError("Invalid billing export project ID")
        if not DATASET_ID_RE.fullmatch(export_dataset):
            raise ValueError("Invalid billing export dataset ID")
        if not included_project_ids:
            raise ValueError("At least one included R2C project ID is required")
        if any(not PROJECT_ID_RE.fullmatch(value) for value in included_project_ids):
            raise ValueError("Invalid included R2C project ID")
        self.client = client
        self.export_project = export_project
        self.export_dataset = export_dataset
        self.included_project_ids = included_project_ids

    @property
    def dataset_id(self) -> str:
        return f"{self.export_project}.{self.export_dataset}"

    def _billing_table_id(self) -> Optional[str]:
        table_names = {table.table_id for table in self.client.list_tables(self.dataset_id)}
        for prefix in BILLING_TABLE_PREFIXES:
            candidates = sorted(
                table_name
                for table_name in table_names
                if table_name.startswith(prefix)
            )
            if candidates:
                return candidates[0]
        return None

    def _cost_query(self, table_id: str) -> str:
        projects = ", ".join(
            f"'{project_id}'" for project_id in self.included_project_ids
        )
        return f"""
WITH scoped_costs AS (
  SELECT
    invoice.month AS billing_period,
    usage_end_time,
    LOWER(service.description) AS service_name,
    cost + IFNULL(
      (SELECT SUM(credit.amount) FROM UNNEST(credits) AS credit),
      0
    ) AS net_cost
  FROM `{self.dataset_id}.{table_id}`
  WHERE project.id IN ({projects})
), latest_period AS (
  SELECT MAX(billing_period) AS billing_period
  FROM scoped_costs
), net_costs AS (
  SELECT scoped_costs.*
  FROM scoped_costs
  JOIN latest_period USING (billing_period)
)
SELECT
  MAX(billing_period) AS billing_period,
  MAX(usage_end_time) AS billing_data_through,
  SUM(net_cost) AS actual_cost_mtd,
  SUM(CASE
    WHEN REGEXP_CONTAINS(service_name, r'(sql|database|spanner|firestore)')
      THEN net_cost ELSE 0 END) AS database_cost,
  SUM(CASE
    WHEN REGEXP_CONTAINS(service_name, r'(network|cdn|dns|load balanc)')
      THEN net_cost ELSE 0 END) AS network_cost,
  SUM(CASE
    WHEN REGEXP_CONTAINS(service_name, r'(storage|artifact registry)')
      THEN net_cost ELSE 0 END) AS storage_cost,
  SUM(CASE
    WHEN REGEXP_CONTAINS(
      service_name,
      r'(compute|cloud run|cloud functions|app engine)'
    ) THEN net_cost ELSE 0 END) AS compute_cost
FROM net_costs
""".strip()

    def load_snapshot(
        self,
        now: Optional[datetime] = None,
    ) -> PlatformBillingSnapshot:
        generated_at = now or datetime.now(tz=UTC)
        table_id = self._billing_table_id()
        if table_id is None:
            return build_pending_platform_snapshot(
                "Google has accepted the export configuration; its first "
                "billing table has not arrived yet.",
                generated_at,
            )

        rows = list(self.client.query(self._cost_query(table_id)).result())
        if not rows or _row_value(rows[0], "billing_data_through") is None:
            return build_pending_platform_snapshot(
                "The billing table is present, but it has no current-month "
                "records for the configured R2C projects.",
                generated_at,
            )

        row = rows[0]
        through = _row_value(row, "billing_data_through")
        if through.tzinfo is None:
            through = through.replace(tzinfo=UTC)
        actual = _decimal(_row_value(row, "actual_cost_mtd"))
        raw_period = str(_row_value(row, "billing_period") or "")
        billing_period = (
            f"{raw_period[:4]}-{raw_period[4:6]}"
            if re.fullmatch(r"\d{6}", raw_period)
            else through.strftime("%Y-%m")
        )
        current_period = generated_at.strftime("%Y-%m")
        age_hours = max(
            0,
            int((generated_at - through.astimezone(UTC)).total_seconds() // 3600),
        )
        period_is_current = billing_period == current_period
        data_is_stale = not period_is_current or age_hours > 72
        breakdown = CostBreakdown(
            compute=_decimal(_row_value(row, "compute_cost")),
            network=_decimal(_row_value(row, "network_cost")),
            storage=_decimal(_row_value(row, "storage_cost")),
            database=_decimal(_row_value(row, "database_cost")),
        )
        breakdown = CostBreakdown(
            compute=breakdown.compute,
            network=breakdown.network,
            storage=breakdown.storage,
            database=breakdown.database,
            other=actual - breakdown.total,
        )
        return PlatformBillingSnapshot(
            generated_at=generated_at,
            billing_data_through=through,
            actual_cost_mtd=actual,
            actual_cost_breakdown_mtd=breakdown,
            attributed_cost_mtd=MONEY_ZERO,
            unallocated_cost_mtd=actual,
            forecast_cost=(
                _forecast_cost(actual, through)
                if period_is_current
                else actual.quantize(Decimal("0.01"))
            ),
            collected_mtd=MONEY_ZERO,
            organizations=(),
            is_illustrative=False,
            source_status="stale" if data_is_stale else "ready",
            source_name="Google Cloud Billing export",
            source_message=(
                (
                    f"Latest available billing period is {billing_period}; "
                    f"the export is {age_hours} hours behind. Values remain "
                    "visible while Google finishes the export backlog."
                    if data_is_stale
                    else (
                        "Live month-to-date net cost for the explicitly "
                        "configured R2C Google Cloud projects."
                    )
                )
            ),
            organizations_are_illustrative=False,
            billing_period=billing_period,
            billing_period_is_current=period_is_current,
            billing_data_stale=data_is_stale,
            billing_data_age_hours=age_hours,
        )


def public_snapshot_dict(snapshot: PlatformBillingSnapshot) -> dict:
    """Prepare a template-safe aggregate payload with no tenant-content fields."""
    payload = asdict(snapshot)
    for organization in payload["organizations"]:
        costs = organization["month_to_date_cost"]
        organization["month_to_date_cost"]["total"] = sum(
            costs.values(),
            MONEY_ZERO,
        )
    return payload
