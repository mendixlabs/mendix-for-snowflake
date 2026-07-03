from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    RESOURCE_TIERS,
    AppRecord,
    CreateAppRequest,
    ResourceTier,
    UpdateComputePoolRequest,
    UpdateConstantsRequest,
    UpdateLicenseRequest,
    UpdateRoleMappingRequest,
)


def _make_record(**overrides):
    defaults = dict(
        name="myapp", service_name="MYAPP_SERVICE", app_schema="MXAPP_MYAPP",
        pg_database="myapp_db", resource_tier="medium", use_caller_rights=False,
        constants={}, owner_role="OWNER_ROLE",
        pad_stage_path=None, endpoint_url=None, last_deploy_status="READY",
        created_at=None, last_deployed_at=None,
    )
    defaults.update(overrides)
    return AppRecord(**defaults)


def _make(**overrides):
    defaults = dict(name="myapp", pg_database="myapp_db", admin_password="pw")
    defaults.update(overrides)
    return CreateAppRequest(**defaults)


class TestCreateAppRequest:
    def test_valid_minimal_payload(self):
        req = _make()
        assert req.name == "myapp"
        assert req.resource_tier == ResourceTier.medium
        assert req.use_caller_rights is False
        assert req.owner_role == "MENDIX_ADMIN_OPERATOR_ROLE"

    def test_name_with_hyphen_rejected(self):
        with pytest.raises(ValidationError):
            _make(name="my-app")

    def test_name_with_leading_digit_rejected(self):
        with pytest.raises(ValidationError):
            _make(name="1app")

    def test_name_with_semicolon_rejected(self):
        with pytest.raises(ValidationError):
            _make(name="app;drop")

    def test_pg_database_with_hyphen_rejected(self):
        with pytest.raises(ValidationError):
            _make(pg_database="my-db")

    def test_pg_database_with_semicolon_rejected(self):
        with pytest.raises(ValidationError):
            _make(pg_database="db;drop")

    def test_owner_role_sql_injection_rejected(self):
        with pytest.raises(ValidationError):
            _make(owner_role="X'; DROP TABLE users; --")


class TestValidateConstantNames:
    def test_dotted_name_accepted(self):
        req = _make(constants={"MyModule.MyConst": "value"})
        assert req.constants == {"MyModule.MyConst": "value"}

    def test_quote_rejected(self):
        with pytest.raises(ValidationError):
            _make(constants={"bad'name": "v"})

    def test_space_rejected(self):
        with pytest.raises(ValidationError):
            _make(constants={"bad name": "v"})

    def test_semicolon_rejected(self):
        with pytest.raises(ValidationError):
            _make(constants={"bad;name": "v"})

    def test_update_constants_request_validates_names(self):
        with pytest.raises(ValidationError):
            UpdateConstantsRequest(constants={"bad;name": "v"})


class TestUpdateComputePoolRequest:
    def test_min_nodes_zero_rejected(self):
        with pytest.raises(ValidationError):
            UpdateComputePoolRequest(min_nodes=0)

    def test_max_nodes_eleven_rejected(self):
        with pytest.raises(ValidationError):
            UpdateComputePoolRequest(max_nodes=11)

    def test_auto_suspend_secs_negative_rejected(self):
        with pytest.raises(ValidationError):
            UpdateComputePoolRequest(auto_suspend_secs=-1)

    def test_all_none_allowed(self):
        req = UpdateComputePoolRequest()
        assert req.min_nodes is None
        assert req.max_nodes is None
        assert req.auto_suspend_secs is None


def test_resource_tiers_has_exactly_three_keys():
    assert set(RESOURCE_TIERS.keys()) == set(ResourceTier)
    assert len(RESOURCE_TIERS) == 3


class TestCreateAppRequestLicense:
    def test_both_fields_accepted(self):
        req = _make(license_id="LIC-1", license_key="key-value")
        assert req.license_id == "LIC-1"
        assert req.license_key == "key-value"

    def test_neither_field_accepted(self):
        req = _make()
        assert req.license_id is None
        assert req.license_key is None

    def test_license_id_without_key_rejected(self):
        with pytest.raises(ValidationError):
            _make(license_id="LIC-1")

    def test_license_key_without_id_rejected(self):
        with pytest.raises(ValidationError):
            _make(license_key="key-value")


class TestUpdateLicenseRequest:
    def test_valid(self):
        req = UpdateLicenseRequest(license_id="LIC-1", license_key="key-value")
        assert req.license_id == "LIC-1"
        assert req.license_key == "key-value"

    def test_empty_license_id_rejected(self):
        with pytest.raises(ValidationError):
            UpdateLicenseRequest(license_id="", license_key="key-value")

    def test_empty_license_key_rejected(self):
        with pytest.raises(ValidationError):
            UpdateLicenseRequest(license_id="LIC-1", license_key="")


class TestAppRecordLicensed:
    def test_licensed_false_when_no_license_id(self):
        record = _make_record()
        assert record.license_id is None
        assert record.licensed is False

    def test_licensed_true_when_license_id_set(self):
        record = _make_record(license_id="LIC-1")
        assert record.licensed is True

    def test_serialized_record_has_license_id_and_licensed_no_license_key(self):
        record = _make_record(license_id="LIC-1")
        dumped = record.model_dump()
        assert dumped["license_id"] == "LIC-1"
        assert dumped["licensed"] is True
        assert "license_key" not in dumped


class TestAppRecordRoleMappingDefaults:
    def test_user_roles_defaults_to_empty_list(self):
        record = _make_record()
        assert record.user_roles == []

    def test_role_mapping_defaults_to_empty_dict(self):
        record = _make_record()
        assert record.role_mapping == {}

    def test_explicit_values_round_trip(self):
        record = _make_record(user_roles=["User", "Administrator"], role_mapping={"ROLE_A": "Administrator"})
        assert record.user_roles == ["User", "Administrator"]
        assert record.role_mapping == {"ROLE_A": "Administrator"}


class TestUpdateRoleMappingRequest:
    def test_keys_uppercased(self):
        req = UpdateRoleMappingRequest(role_mapping={"my_role": "Administrator"})
        assert req.role_mapping == {"MY_ROLE": "Administrator"}

    def test_values_not_uppercased(self):
        req = UpdateRoleMappingRequest(role_mapping={"role_a": "Administrator"})
        assert req.role_mapping["ROLE_A"] == "Administrator"

    def test_strips_whitespace(self):
        req = UpdateRoleMappingRequest(role_mapping={" role_a ": " Administrator "})
        assert req.role_mapping == {"ROLE_A": "Administrator"}

    def test_empty_mapping_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={})

    def test_more_than_fifty_entries_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={f"role_{i}": "User" for i in range(51)})

    def test_empty_key_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"": "User"})

    def test_empty_value_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"role_a": ""})

    def test_key_too_long_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"x" * 256: "User"})

    def test_value_too_long_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"role_a": "x" * 201})

    def test_control_char_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"role_a": "bad\nrole"})

    def test_single_quote_in_value_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"role_a": "bad'role"})

    def test_double_quote_in_value_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"role_a": 'bad"role'})

    def test_duplicate_after_uppercasing_rejected(self):
        with pytest.raises(ValidationError):
            UpdateRoleMappingRequest(role_mapping={"role_a": "User", "ROLE_A": "Administrator"})
