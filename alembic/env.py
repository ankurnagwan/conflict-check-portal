"""Alembic environment: uses the app's own engine (WAL, FK pragmas) and models."""
from alembic import context

import database

target_metadata = database.Base.metadata


def run_migrations_online() -> None:
    with database.engine.connect() as connection:
        # SQLite cannot ALTER most things in place: batch mode rebuilds the table.
        # FK enforcement must be off while a table is rebuilt.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            context.configure(connection=connection, target_metadata=target_metadata,
                              render_as_batch=True, compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
            # A rebuilt table loses its triggers: always re-assert them.
            database.install_triggers(connection)
            connection.commit()
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) mode is not supported for this project.")
run_migrations_online()
