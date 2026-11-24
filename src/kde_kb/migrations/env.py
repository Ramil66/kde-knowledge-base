from alembic import context

from kde_kb.models import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
