from __future__ import annotations

import json
import os

import pytest
import yaml

from app import main
from app.models import ResourceTier, RESOURCE_TIERS
from app.pad_parser import PadConstant


def test_const_secret_name():
    assert main._const_secret_name("MyModule.Secret") == "MX_CONST_MYMODULE_SECRET"


def test_service_name():
    assert main._service_name("myapp") == "MYAPP_SERVICE"


def test_app_schema_name():
    assert main._app_schema_name("myapp") == "MXAPP_MYAPP"


def test_schema_fqn_uses_db_prefix():
    # DB_SCHEMA = "TESTDB.PUBLIC" (conftest) -> prefix "TESTDB."
    assert main._schema_fqn("MXAPP_MYAPP") == "TESTDB.MXAPP_MYAPP"


def test_filestorage_stage():
    assert main._filestorage_stage("MXAPP_MYAPP") == "TESTDB.MXAPP_MYAPP.FILESTORAGE_STAGE"


def test_secret_fqn():
    assert main._secret_fqn("MXAPP_MYAPP", "pg_pass") == "TESTDB.MXAPP_MYAPP.PG_PASS"


class TestEndpointIsReal:
    def test_real_host(self):
        assert main._endpoint_is_real("abc123.snowflakecomputing.app") is True

    def test_none(self):
        assert main._endpoint_is_real(None) is False

    def test_empty(self):
        assert main._endpoint_is_real("") is False

    def test_provisioning_message_with_spaces(self):
        assert main._endpoint_is_real("Endpoints provisioning in progress. Please wait.") is False

    def test_dotless(self):
        assert main._endpoint_is_real("localhost") is False


def test_constants_from_dict():
    result = main._constants_from_dict({"MyModule.MyConst": "value"})
    assert len(result) == 1
    c = result[0]
    assert c.name == "MyModule.MyConst"
    assert c.default == "value"
    assert c.secret_name == "MX_CONST_MYMODULE_MYCONST"


class TestBuildSpec:
    def _spec(self, **kwargs):
        defaults = dict(
            app_name="myapp",
            app_schema="MXAPP_MYAPP",
            pg_database="myapp_db",
            resource_tier=ResourceTier.medium,
            constants=[],
            use_caller_rights=False,
        )
        defaults.update(kwargs)
        raw = main._build_spec(**defaults)
        return yaml.safe_load(raw)

    def test_image_default_derived_from_image_repo(self):
        spec = self._spec()
        assert spec["spec"]["containers"][0]["image"] == "/testdb/public/test_repo:latest"

    def test_pad_stage_path_under_deploy_stage_mount(self):
        spec = self._spec()
        assert spec["spec"]["containers"][0]["env"]["PAD_STAGE_PATH"] == (
            f"{main.DEPLOY_STAGE_MOUNT}/apps/myapp/current.zip"
        )

    def test_pad_relative_path_override_used_verbatim(self):
        # The container's entrypoint has no filename fallback of its own, so
        # whatever _resolve_staged_pad actually found must be exactly what
        # PAD_STAGE_PATH points to - not the current.zip placeholder.
        spec = self._spec(pad_relative_path="apps/myapp/MyReleasePad_20260706.zip")
        assert spec["spec"]["containers"][0]["env"]["PAD_STAGE_PATH"] == (
            f"{main.DEPLOY_STAGE_MOUNT}/apps/myapp/MyReleasePad_20260706.zip"
        )

    def test_pg_host_from_env_fallback(self):
        spec = self._spec()
        assert spec["spec"]["containers"][0]["env"]["RUNTIME_PARAMS_DATABASEHOST"] == "pg.test.local:5432"

    def test_per_constant_secret_entries(self):
        constants = [PadConstant(name="Mod.A", env_var="", default="v", secret_name="MX_CONST_MOD_A")]
        spec = self._spec(constants=constants)
        secrets = spec["spec"]["containers"][0]["secrets"]
        entry = next(s for s in secrets if s["snowflakeSecret"].endswith("MX_CONST_MOD_A"))
        assert entry["directoryPath"] == "/secrets/mx_const_mod_a"
        assert entry["snowflakeSecret"] == "TESTDB.MXAPP_MYAPP.MX_CONST_MOD_A"

    def test_pg_pass_and_admin_pass_secrets_present(self):
        spec = self._spec()
        secrets = spec["spec"]["containers"][0]["secrets"]
        fqns = {s["snowflakeSecret"] for s in secrets}
        assert "TESTDB.MXAPP_MYAPP.PG_PASS" in fqns
        assert "TESTDB.MXAPP_MYAPP.ADMIN_PASS" in fqns

    def test_filestorage_volume_uid_gid(self):
        spec = self._spec()
        volumes = spec["spec"]["volumes"]
        filestorage = next(v for v in volumes if v["name"] == "filestorage")
        assert filestorage["uid"] == 999
        assert filestorage["gid"] == 999

    def test_execute_as_caller_present_when_true(self):
        spec = self._spec(use_caller_rights=True)
        assert spec["capabilities"]["securityContext"]["executeAsCaller"] is True

    def test_execute_as_caller_absent_when_false(self):
        spec = self._spec(use_caller_rights=False)
        assert "capabilities" not in spec

    def test_resource_tier_values(self):
        spec = self._spec(resource_tier=ResourceTier.large)
        res = RESOURCE_TIERS[ResourceTier.large]
        resources = spec["spec"]["containers"][0]["resources"]
        assert resources["requests"]["memory"] == res["mem_request"]
        assert resources["limits"]["cpu"] == res["cpu_limit"]

    def test_license_absent_by_default(self):
        spec = self._spec()
        env = spec["spec"]["containers"][0]["env"]
        assert "RUNTIME_LICENSE_ID" not in env
        secrets = spec["spec"]["containers"][0]["secrets"]
        assert all(s["directoryPath"] != "/secrets/mx_license_key" for s in secrets)

    def test_license_present_sets_env_and_secret_mount(self):
        spec = self._spec(license_id="LIC-123")
        env = spec["spec"]["containers"][0]["env"]
        assert env["RUNTIME_LICENSE_ID"] == "LIC-123"
        secrets = spec["spec"]["containers"][0]["secrets"]
        entry = next(s for s in secrets if s["directoryPath"] == "/secrets/mx_license_key")
        assert entry["snowflakeSecret"] == "TESTDB.MXAPP_MYAPP.MX_LICENSE_KEY"


class TestLoadPgCredentials:
    def test_env_fallback_and_caching(self, monkeypatch):
        host, password = main._load_pg_credentials()
        assert host == "pg.test.local:5432"
        assert password == "test-pg-password"
        # Change env after first call: cached value must not change.
        monkeypatch.setenv("PG_HOST", "changed:5432")
        monkeypatch.setenv("PG_PASS", "changed-pw")
        host2, password2 = main._load_pg_credentials()
        assert host2 == "pg.test.local:5432"
        assert password2 == "test-pg-password"

    def test_json_secret_branch(self, monkeypatch):
        payload = json.dumps({"host": "h:5432", "password": "p"})

        monkeypatch.setattr(os.path, "exists", lambda p: p == "/secrets/pg/secret_string")

        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == "/secrets/pg/secret_string":
                import io
                return io.StringIO(payload)
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        host, password = main._load_pg_credentials()
        assert host == "h:5432"
        assert password == "p"

    def test_json_secret_malformed_raises(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/secrets/pg/secret_string")

        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == "/secrets/pg/secret_string":
                import io
                return io.StringIO("not json")
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        with pytest.raises(RuntimeError):
            main._load_pg_credentials()

    def test_json_secret_missing_key_raises(self, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: p == "/secrets/pg/secret_string")

        import builtins
        real_open = builtins.open

        def fake_open(path, *a, **kw):
            if path == "/secrets/pg/secret_string":
                import io
                return io.StringIO(json.dumps({"host": "h:5432"}))
            return real_open(path, *a, **kw)

        monkeypatch.setattr(builtins, "open", fake_open)
        with pytest.raises(RuntimeError):
            main._load_pg_credentials()


class TestResolveStagedPad:
    def test_current_zip_preferred(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "DEPLOY_STAGE_MOUNT", str(tmp_path))
        app_dir = tmp_path / "apps" / "myapp"
        app_dir.mkdir(parents=True)
        (app_dir / "other.zip").write_bytes(b"x")
        (app_dir / "current.zip").write_bytes(b"y")
        result = main._resolve_staged_pad("myapp")
        assert os.path.basename(result) == "current.zip"

    def test_no_dir_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "DEPLOY_STAGE_MOUNT", str(tmp_path))
        assert main._resolve_staged_pad("myapp") is None

    def test_dir_with_no_zips_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "DEPLOY_STAGE_MOUNT", str(tmp_path))
        app_dir = tmp_path / "apps" / "myapp"
        app_dir.mkdir(parents=True)
        (app_dir / "readme.txt").write_text("x")
        assert main._resolve_staged_pad("myapp") is None

    def test_newest_zip_by_mtime_chosen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "DEPLOY_STAGE_MOUNT", str(tmp_path))
        app_dir = tmp_path / "apps" / "myapp"
        app_dir.mkdir(parents=True)
        old = app_dir / "old.zip"
        new = app_dir / "new.zip"
        old.write_bytes(b"x")
        new.write_bytes(b"y")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        result = main._resolve_staged_pad("myapp")
        assert os.path.basename(result) == "new.zip"

    def test_case_insensitive_extension(self, tmp_path, monkeypatch):
        monkeypatch.setattr(main, "DEPLOY_STAGE_MOUNT", str(tmp_path))
        app_dir = tmp_path / "apps" / "myapp"
        app_dir.mkdir(parents=True)
        (app_dir / "PAD.ZIP").write_bytes(b"x")
        result = main._resolve_staged_pad("myapp")
        assert os.path.basename(result) == "PAD.ZIP"


class TestPollStatus:
    def _clock(self, monkeypatch):
        state = {"t": 0.0, "sleeps": []}

        def fake_time():
            return state["t"]

        def fake_sleep(secs):
            state["sleeps"].append(secs)
            state["t"] += secs

        monkeypatch.setattr(main.time, "time", fake_time)
        monkeypatch.setattr(main.time, "sleep", fake_sleep)
        return state

    def test_immediate_match_zero_sleeps(self, monkeypatch):
        state = self._clock(monkeypatch)
        monkeypatch.setattr(main.sf, "show_service_status", lambda name: "RUNNING")
        assert main._poll_status("svc", "RUNNING", timeout_secs=100) is True
        assert state["sleeps"] == []

    def test_match_on_third_poll(self, monkeypatch):
        state = self._clock(monkeypatch)
        statuses = iter(["STARTING", "STARTING", "RUNNING"])
        monkeypatch.setattr(main.sf, "show_service_status", lambda name: next(statuses))
        assert main._poll_status("svc", "RUNNING", timeout_secs=100) is True
        assert len(state["sleeps"]) == 2

    def test_never_matches_returns_false_after_deadline(self, monkeypatch):
        state = self._clock(monkeypatch)
        monkeypatch.setattr(main.sf, "show_service_status", lambda name: "STARTING")
        assert main._poll_status("svc", "RUNNING", timeout_secs=25) is False
