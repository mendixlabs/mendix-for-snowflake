from __future__ import annotations

from app import activity


# ---------------------------------------------------------------------------
# Pure half: derive_action
# ---------------------------------------------------------------------------

class TestDeriveAction:
    def test_create(self):
        assert activity.derive_action("POST", "/apps") == ("create", None)

    def test_trigger_deploy(self):
        assert activity.derive_action("POST", "/apps/myapp/trigger-deploy") == ("deploy", "myapp")

    def test_suspend(self):
        assert activity.derive_action("POST", "/apps/myapp/suspend") == ("suspend", "myapp")

    def test_resume(self):
        assert activity.derive_action("POST", "/apps/myapp/resume") == ("resume", "myapp")

    def test_update_constants(self):
        assert activity.derive_action("PUT", "/apps/myapp/constants") == ("update_constants", "myapp")

    def test_update_spec(self):
        assert activity.derive_action("PUT", "/apps/myapp/spec") == ("update_spec", "myapp")

    def test_update_license_put(self):
        assert activity.derive_action("PUT", "/apps/myapp/license") == ("update_license", "myapp")

    def test_update_license_delete(self):
        assert activity.derive_action("DELETE", "/apps/myapp/license") == ("update_license", "myapp")

    def test_update_role_mapping_put(self):
        assert activity.derive_action("PUT", "/apps/myapp/role-mapping") == ("update_role_mapping", "myapp")

    def test_update_role_mapping_delete(self):
        assert activity.derive_action("DELETE", "/apps/myapp/role-mapping") == ("update_role_mapping", "myapp")

    def test_delete(self):
        assert activity.derive_action("DELETE", "/apps/myapp") == ("delete", "myapp")

    def test_resize_compute_pool(self):
        # The compute-pool pattern has no app-name capture group.
        assert activity.derive_action("PATCH", "/system/compute-pool") == \
            ("resize_compute_pool", None)

    def test_acknowledge_egress(self):
        assert activity.derive_action("POST", "/system/egress-ack") == \
            ("acknowledge_egress", None)

    def test_set_egress_alert_config(self):
        assert activity.derive_action("POST", "/system/egress-alert-config") == \
            ("set_egress_alert_config", None)

    def test_unknown_path(self):
        assert activity.derive_action("GET", "/nonsense") == ("unknown", None)


# ---------------------------------------------------------------------------
# DB half: init_table / insert / query, over fake_execute_sql
# ---------------------------------------------------------------------------

class TestInitTable:
    def test_creates_table_in_configured_schema(self, fake_execute_sql):
        activity.init_table()
        sql, params = fake_execute_sql.calls[0]
        assert "CREATE TABLE IF NOT EXISTS TESTDB.PUBLIC.MENDIX_ACTIVITY" in sql


class TestInsert:
    def test_detail_json_encoded_into_params(self, fake_execute_sql):
        activity.insert(operator="bob", action="suspend", app_name="myapp",
                        detail={"path": "/x"}, result="accepted")
        sql, params = fake_execute_sql.calls[0]
        assert params[0] == "bob"
        assert params[1] == "suspend"
        assert params[2] == "myapp"
        assert params[3] == '{"path": "/x"}'
        assert params[4] == "accepted"


class TestQuery:
    def test_no_filters_no_where(self, fake_execute_sql):
        activity.query()
        sql, params = fake_execute_sql.calls[0]
        assert "WHERE" not in sql
        assert params == ()

    def test_app_filter_only(self, fake_execute_sql):
        activity.query(app="myapp")
        sql, params = fake_execute_sql.calls[0]
        assert "WHERE app_name = %s" in sql
        assert params == ("myapp",)

    def test_both_filters_anded_params_in_order(self, fake_execute_sql):
        activity.query(app="myapp", operator="bob")
        sql, params = fake_execute_sql.calls[0]
        assert "app_name = %s AND operator = %s" in sql
        assert params == ("myapp", "bob")

    def test_limit_int_coerced(self, fake_execute_sql):
        activity.query(limit="50")
        sql, params = fake_execute_sql.calls[0]
        assert "LIMIT 50" in sql

    def test_limit_hostile_string_raises(self, fake_execute_sql):
        import pytest
        with pytest.raises(ValueError):
            activity.query(limit="50; DROP TABLE x")

    def test_string_detail_round_trips_to_dict(self, fake_execute_sql):
        fake_execute_sql.returns = [[{
            "ID": 1, "TS": "2026-01-01", "OPERATOR": "bob", "ACTION": "suspend",
            "APP_NAME": "myapp", "DETAIL": '{"path": "/x"}', "RESULT": "accepted",
        }]]
        rows = activity.query()
        assert rows[0]["detail"] == {"path": "/x"}

    def test_undecodable_detail_left_as_is(self, fake_execute_sql):
        fake_execute_sql.returns = [[{
            "ID": 1, "TS": None, "OPERATOR": "bob", "ACTION": "suspend",
            "APP_NAME": "myapp", "DETAIL": "not json", "RESULT": "accepted",
        }]]
        rows = activity.query()
        assert rows[0]["detail"] == "not json"
        assert rows[0]["ts"] is None
