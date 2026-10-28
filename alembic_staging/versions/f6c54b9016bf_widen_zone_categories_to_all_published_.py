"""widen zone categories to all published TLUBN zone codes

The live schema (inspected 2026-10-28) carries drinking-water zones
IIA, IIB, IIC and IIIC and healing-spring zones (QUAL I-III, QUANT A/B)
in addition to I, II, III, IIIA and IIIB.

Downgrade fails while rows use one of the new categories, which is
intended: narrowing the check must not silently drop classified zones.

Revision ID: f6c54b9016bf
Revises: be6675c42433
Create Date: 2026-10-27 18:04:56.544031

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f6c54b9016bf"
down_revision: Union[str, Sequence[str], None] = "be6675c42433"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "iris_staging"
TABLE = "water_protection_zones"
CONSTRAINT = "ck_water_protection_zones_zone_category_allowed"

OLD_CATEGORIES = ("ZONE_I", "ZONE_II", "ZONE_III", "ZONE_IIIA", "ZONE_IIIB", "UNKNOWN")
NEW_CATEGORIES = (
    "ZONE_I",
    "ZONE_II",
    "ZONE_IIA",
    "ZONE_IIB",
    "ZONE_IIC",
    "ZONE_III",
    "ZONE_IIIA",
    "ZONE_IIIB",
    "ZONE_IIIC",
    "HQSG_QUAL_I",
    "HQSG_QUAL_II",
    "HQSG_QUAL_III",
    "HQSG_QUANT_A",
    "HQSG_QUANT_B",
    "UNKNOWN",
)


def _replace_check(categories: tuple[str, ...]) -> None:
    quoted = ", ".join(f"'{c}'" for c in categories)
    op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check", schema=SCHEMA)
    op.create_check_constraint(op.f(CONSTRAINT), TABLE, f"zone_category IN ({quoted})", schema=SCHEMA)


def upgrade() -> None:
    """Upgrade schema."""
    _replace_check(NEW_CATEGORIES)


def downgrade() -> None:
    """Downgrade schema."""
    _replace_check(OLD_CATEGORIES)
