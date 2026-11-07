"""baseline: schema created by database.init_db() (create_all + triggers)

Revision ID: 0001
Revises:
Create Date: 2026-11-05
"""
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass   # fresh installs are built from the models and stamped to head


def downgrade() -> None:
    pass
