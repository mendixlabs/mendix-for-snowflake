from __future__ import annotations

import json

from app import deploy_history
from app.models import AppRecord


def _record(**overrides) -> AppRecord:
    defaults = dict(
        name="myapp",
        service_name="MYAPP_SERVICE",
        app_schema="MXAPP_MYAPP",
        pg_database="myapp_db",
        resource_tier="medium",
        use_caller_rights=False,
        constants={"Mod.B": "v1", "Mod.A": "v2"},
        owner_role="OWNER_ROLE",
        pad_stage_path="apps/myapp/current.zip",
        endpoint_url=None,
        last_deploy_status="READY",
        created_at=None,
        last_deployed_at=None,
    )
    defaults.update(overrides)
    return AppRecord(**defaults)


class TestInitTable:
    def test_creates_table_in_configured_schema(self, fake_execute_sql):
        deploy_history.init_table()
        sql, params = fake_execute_sql.calls[0]
        assert "CREATE TABLE IF NOT EXISTS TESTDB.PUBLIC.MENDIX_DEPLOY_HISTORY" in sql


class TestRecord:
    def test_insert_then_prune_two_statements(self, fake_execute_sql):
        deploy_history.record("myapp", "deploy", _record(), "READY")
        assert len(fake_execute_sql.calls) == 2

        insert_sql, params = fake_execute_sql.calls[0]
        assert "INSERT INTO" in insert_sql
        assert params[0] == "myapp"
        assert params[1] == "deploy"
        assert params[2] == "apps/myapp/current.zip"
        assert params[3] == "medium"
        assert params[4] is False
        assert json.loads(params[5]) == ["Mod.A", "Mod.B"]  # names only, sorted
        assert params[6] is None  # license_id
        assert json.loads(params[7]) == {}  # role_mapping
        assert json.loads(params[8]) == []  # external_access
        assert params[9] == "READY"
        assert params[10] is None  # detail

        prune_sql, prune_params = fake_execute_sql.calls[1]
        assert "DELETE FROM" in prune_sql
        assert "LIMIT 20" in prune_sql
        assert prune_params == ("myapp", "myapp")

    def test_constant_values_never_appear_in_params(self, fake_execute_sql):
        deploy_history.record("myapp", "constants", _record(constants={"Mod.A": "super-secret-value"}), "READY")
        _, params = fake_execute_sql.calls[0]
        assert "super-secret-value" not in json.dumps(params)

    def test_detail_passed_through_on_failure(self, fake_execute_sql):
        deploy_history.record("myapp", "deploy", _record(), "FAILED", detail="boom")
        _, params = fake_execute_sql.calls[0]
        assert params[9] == "FAILED"
        assert params[10] == "boom"

    def test_role_mapping_and_external_access_encoded(self, fake_execute_sql):
        rec = _record(role_mapping={"ROLE_A": "Administrator"}, external_access=["app_eai_1"])
        deploy_history.record("myapp", "role_mapping", rec, "READY")
        _, params = fake_execute_sql.calls[0]
        assert json.loads(params[7]) == {"ROLE_A": "Administrator"}
        assert json.loads(params[8]) == ["app_eai_1"]

    def test_none_role_mapping_encoded_as_empty_object(self, fake_execute_sql):
        # AppRecord.role_mapping normally defaults to {}, but main.py's
        # _run_delete_role_mapping/_run_rollback snapshot a record whose
        # role_mapping was set to None via model_copy(update=...) (bypasses
        # validation, same pattern main.py itself uses) - must not raise on
        # json.dumps(None or {}).
        rec = _record().model_copy(update={"role_mapping": None})
        deploy_history.record("myapp", "role_mapping", rec, "READY")
        _, params = fake_execute_sql.calls[0]
        assert json.loads(params[7]) == {}


class TestListForApp:
    def test_orders_newest_first_and_binds_limit(self, fake_execute_sql):
        deploy_history.list_for_app("myapp", limit=5)
        sql, params = fake_execute_sql.calls[0]
        assert "ORDER BY ts DESC, id DESC LIMIT 5" in sql
        assert params == ("myapp",)

    def test_default_limit_is_20(self, fake_execute_sql):
        deploy_history.list_for_app("myapp")
        sql, _ = fake_execute_sql.calls[0]
        assert "LIMIT 20" in sql

    def test_decodes_variant_columns(self, fake_execute_sql):
        fake_execute_sql.returns = [[{
            "ID": 1, "TS": "2026-01-01", "OPERATION": "deploy",
            "PAD_STAGE_PATH": "apps/myapp/pad.zip", "RESOURCE_TIER": "medium",
            "USE_CALLER_RIGHTS": True, "CONSTANT_NAMES": '["Mod.A"]',
            "LICENSE_ID": "LIC-1", "ROLE_MAPPING": '{"ROLE_A": "Administrator"}',
            "EXTERNAL_ACCESS": '["app_eai_1"]', "STATUS": "READY", "DETAIL": None,
        }]]
        rows = deploy_history.list_for_app("myapp")
        assert rows[0]["constant_names"] == ["Mod.A"]
        assert rows[0]["role_mapping"] == {"ROLE_A": "Administrator"}
        assert rows[0]["external_access"] == ["app_eai_1"]
        assert rows[0]["use_caller_rights"] is True
        assert rows[0]["ts"] == "2026-01-01"

    def test_none_variant_columns_become_empty(self, fake_execute_sql):
        fake_execute_sql.returns = [[{
            "ID": 1, "TS": None, "OPERATION": "deploy",
            "PAD_STAGE_PATH": None, "RESOURCE_TIER": None,
            "USE_CALLER_RIGHTS": None, "CONSTANT_NAMES": None,
            "LICENSE_ID": None, "ROLE_MAPPING": None,
            "EXTERNAL_ACCESS": None, "STATUS": "FAILED", "DETAIL": "boom",
        }]]
        rows = deploy_history.list_for_app("myapp")
        assert rows[0]["constant_names"] == []
        assert rows[0]["role_mapping"] == {}
        assert rows[0]["external_access"] == []
        assert rows[0]["use_caller_rights"] is False
        assert rows[0]["ts"] is None
        assert rows[0]["detail"] == "boom"


class TestLastSuccess:
    def test_filters_to_ready_and_limits_one(self, fake_execute_sql):
        deploy_history.last_success("myapp")
        sql, params = fake_execute_sql.calls[0]
        assert "status = 'READY'" in sql
        assert "LIMIT 1" in sql
        assert params == ("myapp",)

    def test_none_when_no_rows(self, fake_execute_sql):
        fake_execute_sql.returns = [[]]
        assert deploy_history.last_success("myapp") is None

    def test_returns_row_when_present(self, fake_execute_sql):
        fake_execute_sql.returns = [[{
            "ID": 1, "TS": "2026-01-01", "OPERATION": "constants",
            "PAD_STAGE_PATH": "apps/myapp/pad.zip", "RESOURCE_TIER": "large",
            "USE_CALLER_RIGHTS": False, "CONSTANT_NAMES": "[]",
            "LICENSE_ID": None, "ROLE_MAPPING": None,
            "EXTERNAL_ACCESS": None, "STATUS": "READY", "DETAIL": None,
        }]]
        row = deploy_history.last_success("myapp")
        assert row["operation"] == "constants"
        assert row["resource_tier"] == "large"


class TestGetEntry:
    def test_binds_id_and_app_name(self, fake_execute_sql):
        deploy_history.get_entry("myapp", 7)
        sql, params = fake_execute_sql.calls[0]
        assert "id = %s" in sql
        assert "UPPER(app_name) = UPPER(%s)" in sql
        assert params == (7, "myapp")

    def test_none_when_no_match(self, fake_execute_sql):
        fake_execute_sql.returns = [[]]
        assert deploy_history.get_entry("myapp", 7) is None

    def test_returns_row_when_present(self, fake_execute_sql):
        fake_execute_sql.returns = [[{
            "ID": 7, "TS": "2026-01-01", "OPERATION": "deploy",
            "PAD_STAGE_PATH": "apps/myapp/old.zip", "RESOURCE_TIER": "large",
            "USE_CALLER_RIGHTS": True, "CONSTANT_NAMES": "[]",
            "LICENSE_ID": None, "ROLE_MAPPING": None,
            "EXTERNAL_ACCESS": None, "STATUS": "READY", "DETAIL": None,
        }]]
        row = deploy_history.get_entry("myapp", 7)
        assert row["id"] == 7
        assert row["pad_stage_path"] == "apps/myapp/old.zip"


class TestDeleteForApp:
    def test_binds_app_name(self, fake_execute_sql):
        deploy_history.delete_for_app("myapp")
        sql, params = fake_execute_sql.calls[0]
        assert "DELETE FROM" in sql
        assert "UPPER(app_name) = UPPER(%s)" in sql
        assert params == ("myapp",)
