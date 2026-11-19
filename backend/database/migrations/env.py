from alembic import context
from sqlalchemy import create_engine

from backend.database.models import Base

config = context.config
target = Base.metadata
conn = config.attributes.get("connection")

if conn is None:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.begin() as c:
        context.configure(connection=c, target_metadata=target, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
else:
    context.configure(connection=conn, target_metadata=target, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
