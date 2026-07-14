from __future__ import annotations

import json

import pytest

from app import registry
from app.models import HIDDEN_VALUE, AppRecord


class TestMaskConstants:
    def test_values_replaced_keys_kept(self):
        result = registry._mask_constants({"A.B": "secret1", "A.C": "secret2"})
        assert result == {"A.B": HIDDEN_VALUE, "A.C": HIDDEN_VALUE}


class TestRowToRecord:
    def _row(self, **overrides):
        row = {
            "NAME": "myapp",
            "SERVICE_NAME": "MYAPP_SERVICE",
            "APP_SCHEMA": "MXAPP_MYAPP",
            "PG_DATABASE": "myapp_db",
            "RESOURCE_TIER": "medium",
            "USE_CALLER_RIGHTS": True,
            "CONSTANTS": {"A.B": HIDDEN_VALUE},
            "PAD_STAGE_PATH": None,
            "ENDPOINT_URL": None,
            "LAST_DEPLOY_STATUS": "READY",
            "CREATED_AT": None,
            "LAST_DEPLOYED_AT": None,
            "OWNER_ROLE": "OWNER_ROLE",
            "STATUS_DETAIL": None,
            "FAILED_OPERATION": None,
            "EXTERNAL_ACCESS": None,
            "PLATFORM_IMAGE": None,
            "PLATFORM_UPDATE_AVAILABLE": False,
        }
        row.update(overrides)
        return row

    def test_string_constants_json_decoded(self):
        row = self._row(CONSTANTS='{"A.B": "hidden"}')
        record = registry._row_to_record(row)
        assert record.constants == {"A.B": "hidden"}

    def test_dict_constants_passed_through(self):
        row = self._row(CONSTANTS={"A.B": "v"})
        record = registry._row_to_record(row)
        assert record.constants == {"A.B": "v"}

    def test_none_constants_becomes_empty_dict(self):
        row = self._row(CONSTANTS=None)
        record = registry._row_to_record(row)
        assert record.constants == {}

    def test_defaults_applied(self):
        row = self._row(RESOURCE_TIER=None, OWNER_ROLE=None)
        record = registry._row_to_record(row)
        assert record.resource_tier == "medium"
        assert record.owner_role == "MENDIX_ADMIN_OPERATOR_ROLE"

    def test_timestamps_stringified(self):
        row = self._row(CREATED_AT=12345, LAST_DEPLOYED_AT=6789)
        record = registry._row_to_record(row)
        assert record.created_at == "12345"
        assert record.last_deployed_at == "6789"

    def test_use_caller_rights_truthiness(self):
        row = self._row(USE_CALLER_RIGHTS=0)
        record = registry._row_to_record(row)
        assert record.use_caller_rights is False

    def test_license_id_mapped(self):
        row = self._row(LICENSE_ID="LIC-1")
        record = registry._row_to_record(row)
        assert record.license_id == "LIC-1"

    def test_license_id_defaults_to_none(self):
        row = self._row()
        record = registry._row_to_record(row)
        assert record.license_id is None

    def test_user_roles_string_json_decoded(self):
        row = self._row(USER_ROLES='["User", "Administrator"]')
        record = registry._row_to_record(row)
        assert record.user_roles == ["User", "Administrator"]

    def test_user_roles_native_passed_through(self):
        row = self._row(USER_ROLES=["User"])
        record = registry._row_to_record(row)
        assert record.user_roles == ["User"]

    def test_user_roles_none_becomes_empty_list(self):
        row = self._row(USER_ROLES=None)
        record = registry._row_to_record(row)
        assert record.user_roles == []

    def test_role_mapping_string_json_decoded(self):
        row = self._row(ROLE_MAPPING='{"ROLE_A": "Administrator"}')
        record = registry._row_to_record(row)
        assert record.role_mapping == {"ROLE_A": "Administrator"}

    def test_role_mapping_native_passed_through(self):
        row = self._row(ROLE_MAPPING={"ROLE_A": "Administrator"})
        record = registry._row_to_record(row)
        assert record.role_mapping == {"ROLE_A": "Administrator"}

    def test_role_mapping_none_becomes_empty_dict(self):
        row = self._row(ROLE_MAPPING=None)
        record = registry._row_to_record(row)
        assert record.role_mapping == {}

    def test_status_detail_and_failed_operation_mapped(self):
        row = self._row(STATUS_DETAIL="Timed out waiting for RUNNING after 120s", FAILED_OPERATION="deploy")
        record = registry._row_to_record(row)
        assert record.status_detail == "Timed out waiting for RUNNING after 120s"
        assert record.failed_operation == "deploy"

    def test_status_detail_and_failed_operation_default_to_none(self):
        row = self._row()
        record = registry._row_to_record(row)
        assert record.status_detail is None
        assert record.failed_operation is None

    def test_external_access_string_json_decoded(self):
        row = self._row(EXTERNAL_ACCESS='["app_eai_1", "app_eai_2"]')
        record = registry._row_to_record(row)
        assert record.external_access == ["app_eai_1", "app_eai_2"]

    def test_external_access_native_passed_through(self):
        row = self._row(EXTERNAL_ACCESS=["app_eai_1"])
        record = registry._row_to_record(row)
        assert record.external_access == ["app_eai_1"]

    def test_external_access_none_becomes_empty_list(self):
        row = self._row(EXTERNAL_ACCESS=None)
        record = registry._row_to_record(row)
        assert record.external_access == []

    def test_platform_image_mapped(self):
        row = self._row(PLATFORM_IMAGE="mendix-base:1.2.3")
        record = registry._row_to_record(row)
        assert record.platform_image == "mendix-base:1.2.3"

    def test_platform_image_defaults_to_none(self):
        row = self._row()
        record = registry._row_to_record(row)
        assert record.platform_image is None

    def test_platform_update_available_truthiness(self):
        row = self._row(PLATFORM_UPDATE_AVAILABLE=True)
        record = registry._row_to_record(row)
        assert record.platform_update_available is True

    def test_platform_update_available_defaults_to_false(self):
        row = self._row(PLATFORM_UPDATE_AVAILABLE=None)
        record = registry._row_to_record(row)
        assert record.platform_update_available is False


class TestCreateApp:
    def test_insert_params_never_contain_plaintext_value(self, fake_execute_sql):
        record = AppRecord(
            name="myapp", service_name="MYAPP_SERVICE", app_schema="MXAPP_MYAPP",
            pg_database="myapp_db", resource_tier="medium", use_caller_rights=False,
            constants={"A.B": "super-secret-value"}, owner_role="OWNER_ROLE",
            pad_stage_path=None, endpoint_url=None, last_deploy_status="NOT_DEPLOYED",
            created_at=None, last_deployed_at=None,
        )
        registry.create_app(record)
        sql, params = fake_execute_sql.calls[0]
        assert "INSERT INTO" in sql
        assert "super-secret-value" not in json.dumps(params)
        constants_json = [p for p in params if isinstance(p, str) and "A.B" in p][0]
        assert json.loads(constants_json) == {"A.B": HIDDEN_VALUE}

    def test_license_id_included_as_param(self, fake_execute_sql):
        record = AppRecord(
            name="myapp", service_name="MYAPP_SERVICE", app_schema="MXAPP_MYAPP",
            pg_database="myapp_db", resource_tier="medium", use_caller_rights=False,
            constants={}, owner_role="OWNER_ROLE", license_id="LIC-1",
            pad_stage_path=None, endpoint_url=None, last_deploy_status="NOT_DEPLOYED",
            created_at=None, last_deployed_at=None,
        )
        registry.create_app(record)
        sql, params = fake_execute_sql.calls[0]
        assert "license_id" in sql
        assert "LIC-1" in params

    def test_external_access_included_as_param(self, fake_execute_sql):
        record = AppRecord(
            name="myapp", service_name="MYAPP_SERVICE", app_schema="MXAPP_MYAPP",
            pg_database="myapp_db", resource_tier="medium", use_caller_rights=False,
            constants={}, owner_role="OWNER_ROLE", external_access=["app_eai_1", "app_eai_2"],
            pad_stage_path=None, endpoint_url=None, last_deploy_status="NOT_DEPLOYED",
            created_at=None, last_deployed_at=None,
        )
        registry.create_app(record)
        sql, params = fake_execute_sql.calls[0]
        assert "external_access" in sql
        eai_json = [p for p in params if isinstance(p, str) and "app_eai_1" in p][0]
        assert json.loads(eai_json) == ["app_eai_1", "app_eai_2"]

    def test_external_access_defaults_to_empty_list_param(self, fake_execute_sql):
        record = AppRecord(
            name="myapp", service_name="MYAPP_SERVICE", app_schema="MXAPP_MYAPP",
            pg_database="myapp_db", resource_tier="medium", use_caller_rights=False,
            constants={}, owner_role="OWNER_ROLE",
            pad_stage_path=None, endpoint_url=None, last_deploy_status="NOT_DEPLOYED",
            created_at=None, last_deployed_at=None,
        )
        registry.create_app(record)
        sql, params = fake_execute_sql.calls[0]
        assert "[]" in params

    def test_platform_image_included_as_param(self, fake_execute_sql):
        record = AppRecord(
            name="myapp", service_name="MYAPP_SERVICE", app_schema="MXAPP_MYAPP",
            pg_database="myapp_db", resource_tier="medium", use_caller_rights=False,
            constants={}, owner_role="OWNER_ROLE",
            pad_stage_path=None, endpoint_url=None, last_deploy_status="NOT_DEPLOYED",
            created_at=None, last_deployed_at=None, platform_image="/repo/mendix-base:latest",
        )
        registry.create_app(record)
        sql, params = fake_execute_sql.calls[0]
        assert "platform_image" in sql
        assert "/repo/mendix-base:latest" in params


class TestGetApp:
    def test_none_on_empty_rows(self, fake_execute_sql):
        fake_execute_sql.returns = [[]]
        assert registry.get_app("myapp") is None

    def test_binds_name_as_param(self, fake_execute_sql):
        fake_execute_sql.returns = [[]]
        registry.get_app("myapp")
        sql, params = fake_execute_sql.calls[0]
        assert params == ("myapp",)
        assert "%s" in sql


class TestUpdateApp:
    def test_unknown_column_raises_no_sql(self, fake_execute_sql):
        with pytest.raises(ValueError):
            registry.update_app("myapp", {"not_a_real_column": "x"})
        assert fake_execute_sql.calls == []

    def test_constants_routed_through_parse_json_with_masking(self, fake_execute_sql):
        registry.update_app("myapp", {"constants": {"A.B": "secret"}})
        sql, params = fake_execute_sql.calls[0]
        assert "constants = PARSE_JSON(%s)" in sql
        assert json.loads(params[0]) == {"A.B": HIDDEN_VALUE}

    def test_multiple_fields_build_set_clause_name_last(self, fake_execute_sql):
        registry.update_app("myapp", {"endpoint_url": "https://x", "last_deploy_status": "READY"})
        sql, params = fake_execute_sql.calls[0]
        assert "SET endpoint_url = %s, last_deploy_status = %s" in sql
        assert params == ("https://x", "READY", "myapp")

    def test_empty_dict_no_sql(self, fake_execute_sql):
        registry.update_app("myapp", {})
        assert fake_execute_sql.calls == []

    def test_license_id_field_allowed(self, fake_execute_sql):
        registry.update_app("myapp", {"license_id": "LIC-1"})
        sql, params = fake_execute_sql.calls[0]
        assert "license_id = %s" in sql
        assert params == ("LIC-1", "myapp")

    def test_license_id_none_allowed_for_removal(self, fake_execute_sql):
        registry.update_app("myapp", {"license_id": None})
        sql, params = fake_execute_sql.calls[0]
        assert params == (None, "myapp")

    def test_license_key_rejected_as_update_column(self, fake_execute_sql):
        # The registry never has a LICENSE_KEY column to write to: the key is a
        # credential and only ever reaches sf.create_or_replace_secret.
        with pytest.raises(ValueError):
            registry.update_app("myapp", {"license_key": "x"})
        assert fake_execute_sql.calls == []

    def test_role_mapping_routed_through_parse_json_unmasked(self, fake_execute_sql):
        registry.update_app("myapp", {"role_mapping": {"ROLE_A": "Administrator"}})
        sql, params = fake_execute_sql.calls[0]
        assert "role_mapping = PARSE_JSON(%s)" in sql
        assert json.loads(params[0]) == {"ROLE_A": "Administrator"}

    def test_role_mapping_none_binds_sql_null(self, fake_execute_sql):
        registry.update_app("myapp", {"role_mapping": None})
        sql, params = fake_execute_sql.calls[0]
        assert "role_mapping = %s" in sql
        assert params == (None, "myapp")

    def test_user_roles_routed_through_parse_json(self, fake_execute_sql):
        registry.update_app("myapp", {"user_roles": ["User", "Administrator"]})
        sql, params = fake_execute_sql.calls[0]
        assert "user_roles = PARSE_JSON(%s)" in sql
        assert json.loads(params[0]) == ["User", "Administrator"]

    def test_user_roles_none_binds_sql_null(self, fake_execute_sql):
        registry.update_app("myapp", {"user_roles": None})
        sql, params = fake_execute_sql.calls[0]
        assert "user_roles = %s" in sql
        assert params == (None, "myapp")

    def test_external_access_routed_through_parse_json(self, fake_execute_sql):
        registry.update_app("myapp", {"external_access": ["app_eai_1"]})
        sql, params = fake_execute_sql.calls[0]
        assert "external_access = PARSE_JSON(%s)" in sql
        assert json.loads(params[0]) == ["app_eai_1"]

    def test_external_access_none_binds_sql_null(self, fake_execute_sql):
        registry.update_app("myapp", {"external_access": None})
        sql, params = fake_execute_sql.calls[0]
        assert "external_access = %s" in sql
        assert params == (None, "myapp")

    def test_status_detail_field_allowed(self, fake_execute_sql):
        registry.update_app("myapp", {"status_detail": "boom"})
        sql, params = fake_execute_sql.calls[0]
        assert "status_detail = %s" in sql
        assert params == ("boom", "myapp")

    def test_failed_operation_field_allowed(self, fake_execute_sql):
        registry.update_app("myapp", {"failed_operation": "deploy"})
        sql, params = fake_execute_sql.calls[0]
        assert "failed_operation = %s" in sql
        assert params == ("deploy", "myapp")

    def test_platform_image_field_allowed(self, fake_execute_sql):
        registry.update_app("myapp", {"platform_image": "mendix-base:1.2.3"})
        sql, params = fake_execute_sql.calls[0]
        assert "platform_image = %s" in sql
        assert params == ("mendix-base:1.2.3", "myapp")

    def test_platform_update_available_field_allowed(self, fake_execute_sql):
        registry.update_app("myapp", {"platform_update_available": True})
        sql, params = fake_execute_sql.calls[0]
        assert "platform_update_available = %s" in sql
        assert params == (True, "myapp")


class TestDeleteApp:
    def test_parameterized_delete(self, fake_execute_sql):
        registry.delete_app("myapp")
        sql, params = fake_execute_sql.calls[0]
        assert "DELETE FROM" in sql
        assert params == ("myapp",)
