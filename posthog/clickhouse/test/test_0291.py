import importlib

from unittest import TestCase

from django.test import override_settings

from posthog.clickhouse.client.connection import NodeRole

MIGRATION_PATH = "posthog.clickhouse.migrations.0291_add_ai_channel_type"


class TestChannelDefinitionMigration(TestCase):
    def test_replica_sync_precedes_dictionary_reload(self) -> None:
        migration = importlib.import_module(MIGRATION_PATH)
        sync_sql = "SYSTEM SYNC REPLICA channel_definition STRICT"
        reload_sql = "SYSTEM RELOAD DICTIONARY channel_definition_dict"

        expected_operations = {
            "US": [
                (sync_sql, [NodeRole.DATA]),
                (reload_sql, [NodeRole.DATA]),
                (sync_sql, [NodeRole.SESSIONS]),
                (reload_sql, [NodeRole.SESSIONS]),
            ],
            "EU": [
                (sync_sql, [NodeRole.DATA]),
                (reload_sql, [NodeRole.DATA]),
                (reload_sql, [NodeRole.SESSIONS]),
            ],
        }

        try:
            for deployment, expected in expected_operations.items():
                with self.subTest(deployment=deployment), override_settings(CLOUD_DEPLOYMENT=deployment):
                    migration = importlib.reload(migration)
                    system_operations = [
                        (operation._sql, operation._node_roles)
                        for operation in migration.operations
                        if operation._sql.startswith("SYSTEM ")
                    ]
                    self.assertEqual(system_operations, expected)
        finally:
            importlib.reload(migration)
