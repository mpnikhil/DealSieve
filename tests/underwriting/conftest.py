from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.schemas import ExpenseClaims, TenantClaim, WorkingValues


@pytest.fixture
def demo_values() -> WorkingValues:
    rents = [
        Decimal("34200"),
        Decimal("25200"),
        Decimal("23400"),
        Decimal("21600"),
        Decimal("20700"),
        Decimal("19800"),
        Decimal("18000"),
        Decimal("17100"),
    ]
    return WorkingValues(
        asking_price=Decimal("1550000"),
        gross_scheduled_income=sum(rents, Decimal(0)),
        stated_expenses=ExpenseClaims(
            property_tax=Decimal("15500"),
            insurance=Decimal("8400"),
            repairs_maintenance=Decimal("12000"),
            utilities=Decimal("11000"),
            cam_other=Decimal("7100"),
            total=Decimal("54000"),
        ),
        stated_noi=Decimal("126000"),
        building_sqft=20000,
        tenant_count=8,
        largest_tenant_pct=Decimal("0.19"),
        occupancy_pct=Decimal("1"),
        tenants=[
            TenantClaim(name=f"Tenant {index}", annual_rent=rent)
            for index, rent in enumerate(rents, start=1)
        ],
        property_type="small_bay_industrial",
    )
