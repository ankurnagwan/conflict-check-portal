"""clearance columns: who cleared a check and why

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ADD COLUMN: no table rebuild, so triggers and indexes are untouched.
    op.add_column("conflict_checks", sa.Column("cleared_by_id", sa.Integer(), nullable=True))
    op.add_column("conflict_checks", sa.Column("clearance_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("conflict_checks", "clearance_notes")
    op.drop_column("conflict_checks", "cleared_by_id")
