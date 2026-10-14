from __future__ import with_statement
import os
import sys
from logging.config import fileConfig

from alembic import context

# allow importing project modules by adding `src` to sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC_DIR = os.path.join(BASE_DIR, 'src')
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from utilities.dbconfig import Base

# Importing `utilities.dbmodels` will import all model modules listed there
# so SQLAlchemy model classes register on `Base.metadata` for Alembic autogenerate.
try:
    import utilities.dbmodels  # noqa: F401
except Exception as _err:
    # Print but do not raise to avoid breaking alembic autogenerate
    print(f"env.py: failed to import utilities.dbmodels: {_err}")

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
fileConfig(config.config_file_name)

# set sqlalchemy.url from environment if available
db_url = os.getenv('SQLALCHEMY_DATABASE_URL') or os.getenv('DATABASE_URL')
if db_url:
    # configparser treats '%' specially; escape percent signs to avoid interpolation errors
    safe_db_url = db_url.replace('%', '%%')
    config.set_main_option('sqlalchemy.url', safe_db_url)

target_metadata = Base.metadata

# Created with raw SQL in src/app.py (expression and partial unique indexes the models cannot express).
STARTUP_MANAGED_INDEXES = {"uq_products_user_sub_external", "uq_product_images_match_code"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "index" and name in STARTUP_MANAGED_INDEXES:
        return False
    return True


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    json_types = {"JSON", "JSONB"}
    if type(inspected_type).__name__ in json_types and type(metadata_type).__name__ in json_types:
        return False
    return None


def run_migrations_offline():
    """Run migrations in 'offline' mode."""
    url = config.get_main_option('sqlalchemy.url')
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode."""
    connectable = None

    # Prefer an existing connection from alembic invocation
    connectable = config.attributes.get('connection', None)

    if connectable is None:
        from sqlalchemy import engine_from_config
        connectable = engine_from_config(
            config.get_section(config.config_ini_section),
            prefix='sqlalchemy.',
            poolclass=None,
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=compare_type,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
