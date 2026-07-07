from __future__ import annotations

import setup_checks as sc


class TestSecretShowClause:
    def test_three_part_fqn_scoped_to_db_and_schema(self):
        assert (
            sc._secret_show_clause("DB.SCHEMA.MYSECRET")
            == "SHOW SECRETS LIKE 'MYSECRET' IN SCHEMA DB.SCHEMA;"
        )

    def test_two_part_fqn_scoped_to_schema(self):
        assert (
            sc._secret_show_clause("SCHEMA.MYSECRET")
            == "SHOW SECRETS LIKE 'MYSECRET' IN SCHEMA SCHEMA;"
        )

    def test_one_part_fqn_no_scope_clause(self):
        assert sc._secret_show_clause("MYSECRET") == "SHOW SECRETS LIKE 'MYSECRET';"


class TestRenderVerifySql:
    def test_includes_all_three_show_statements(self):
        sql = sc.render_verify_sql("MENDIX_PG", "MENDIX_PG_EAI", "DB.SCHEMA.MYSECRET")
        assert "SHOW POSTGRES INSTANCES LIKE 'MENDIX_PG';" in sql
        assert "SHOW EXTERNAL ACCESS INTEGRATIONS LIKE 'MENDIX_PG_EAI';" in sql
        assert "SHOW SECRETS LIKE 'MYSECRET' IN SCHEMA DB.SCHEMA;" in sql

    def test_is_pure_no_session_or_network_access(self):
        # No caller session, no I/O - just string formatting. Calling it twice
        # with the same args is deterministic.
        args = ("inst", "eai", "db.schema.secret")
        assert sc.render_verify_sql(*args) == sc.render_verify_sql(*args)
