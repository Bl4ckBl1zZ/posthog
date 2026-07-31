from posthog.clickhouse.client.connection import NodeRole
from posthog.clickhouse.client.migration_tools import run_sql_with_exceptions
from posthog.clickhouse.query_log_archive import MV_SELECT_SQL_OPS, QUERY_LOG_ARCHIVE_OPS_MV

ALL_ROLES = [
    NodeRole.DATA,
    NodeRole.ENDPOINTS,
    NodeRole.AUX,
    NodeRole.AI_EVENTS,
    NodeRole.SESSIONS,
    NodeRole.OPS,
]


operations = [
    run_sql_with_exceptions(
        f"ALTER TABLE {QUERY_LOG_ARCHIVE_OPS_MV} MODIFY QUERY\n{MV_SELECT_SQL_OPS}",
        node_roles=ALL_ROLES,
        sharded=False,
        is_alter_on_replicated_table=False,
    ),
]
