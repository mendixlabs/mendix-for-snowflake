from __future__ import annotations

import zipfile

import pytest

from app.pad_parser import (
    PadConstant,
    _build_constants,
    _parse_defaults,
    _parse_variables,
    parse_from_directory,
    parse_from_zip,
    parse_user_roles_from_zip,
)


class TestParseDefaults:
    def test_quoted_value_unquoted(self):
        assert _parse_defaults('"A.B" = "hello"') == {"A.B": "hello"}

    def test_unquoted_value_kept_verbatim(self):
        assert _parse_defaults('"A.B" = 42') == {"A.B": "42"}

    def test_leading_whitespace_tolerated(self):
        assert _parse_defaults('   "A.B" = "x"') == {"A.B": "x"}

    def test_non_matching_lines_ignored(self):
        assert _parse_defaults("not a match\nalso not") == {}

    def test_empty_text(self):
        assert _parse_defaults("") == {}


class TestParseVariables:
    def test_extracts_env_var_pairs(self):
        text = '"Mod.Const" = ${?MOD_CONST}'
        assert _parse_variables(text) == {"Mod.Const": "MOD_CONST"}

    def test_ignores_plain_assignments(self):
        text = '"Mod.Const" = "not an env ref"'
        assert _parse_variables(text) == {}


class TestBuildConstants:
    def test_only_names_in_both_dicts(self):
        defaults = {"A.B": "1", "A.C": "2"}
        env_vars = {"A.B": "ENV_B"}
        result = _build_constants(defaults, env_vars)
        assert [c.name for c in result] == ["A.B"]

    def test_secret_name_derivation(self):
        defaults = {"MyModule.ApiKey": "secret"}
        env_vars = {"MyModule.ApiKey": "MYMODULE_APIKEY"}
        result = _build_constants(defaults, env_vars)
        assert result[0].secret_name == "MX_CONST_MYMODULE_APIKEY"

    def test_invalid_name_semicolon_raises(self):
        defaults = {"bad;name": "1"}
        env_vars = {"bad;name": "X"}
        with pytest.raises(ValueError):
            _build_constants(defaults, env_vars)

    def test_invalid_name_leading_digit_raises(self):
        defaults = {"1bad": "1"}
        env_vars = {"1bad": "X"}
        with pytest.raises(ValueError):
            _build_constants(defaults, env_vars)


class TestParseFromZip:
    def test_flat_layout(self, make_pad_zip):
        zpath = make_pad_zip()
        result = parse_from_zip(zpath)
        assert len(result) == 1
        assert result[0].name == "MyModule.MyConst"

    def test_nested_single_directory_layout(self, make_pad_zip):
        zpath = make_pad_zip(nested="MyApp")
        result = parse_from_zip(zpath)
        assert len(result) == 1
        assert result[0].name == "MyModule.MyConst"

    def test_missing_defaults_returns_empty(self, make_pad_zip):
        zpath = make_pad_zip(omit="defaults")
        assert parse_from_zip(zpath) == []

    def test_missing_variables_returns_empty(self, make_pad_zip):
        zpath = make_pad_zip(omit="variables")
        assert parse_from_zip(zpath) == []

    def test_shortest_path_preferred(self, make_pad_zip, tmp_path):
        import zipfile
        zpath = tmp_path / "pad.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("etc/constants/defaults.conf", '"A.B" = "root"')
            zf.writestr("Nested/etc/constants/defaults.conf", '"A.B" = "nested"')
            zf.writestr("etc/constants/variables.conf", '"A.B" = ${?A_B}')
        result = parse_from_zip(zpath)
        assert result[0].default == "root"


class TestParseFromDirectory:
    def test_happy_path(self, tmp_path):
        const_dir = tmp_path / "etc" / "constants"
        const_dir.mkdir(parents=True)
        (const_dir / "defaults.conf").write_text('"A.B" = "hello"\n', encoding="utf-8")
        (const_dir / "variables.conf").write_text('"A.B" = ${?A_B}\n', encoding="utf-8")
        result = parse_from_directory(tmp_path)
        assert len(result) == 1
        assert result[0].name == "A.B"

    def test_missing_files_returns_empty(self, tmp_path):
        assert parse_from_directory(tmp_path) == []


class TestParseUserRolesFromZip:
    def _zip_with_metadata(self, tmp_path, metadata_text, path="model/metadata.json", filename="pad.zip"):
        zpath = tmp_path / filename
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr(path, metadata_text)
        return zpath

    def test_flat_layout(self, tmp_path):
        metadata = '{"Roles": {"uuid-1": {"Name": "User"}, "uuid-2": {"Name": "Administrator"}}}'
        zpath = self._zip_with_metadata(tmp_path, metadata)
        result = parse_user_roles_from_zip(zpath)
        assert sorted(result) == ["Administrator", "User"]

    def test_nested_single_directory_layout(self, tmp_path):
        metadata = '{"Roles": {"uuid-1": {"Name": "User"}}}'
        zpath = self._zip_with_metadata(tmp_path, metadata, path="MyApp/model/metadata.json")
        result = parse_user_roles_from_zip(zpath)
        assert result == ["User"]

    def test_missing_roles_key_returns_empty(self, tmp_path):
        zpath = self._zip_with_metadata(tmp_path, "{}")
        assert parse_user_roles_from_zip(zpath) == []

    def test_missing_file_returns_empty(self, tmp_path):
        zpath = tmp_path / "pad.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("etc/constants/defaults.conf", "")
        assert parse_user_roles_from_zip(zpath) == []

    def test_malformed_json_returns_empty_without_raising(self, tmp_path):
        zpath = self._zip_with_metadata(tmp_path, "not json{")
        assert parse_user_roles_from_zip(zpath) == []

    def test_datamodel_metadata_json_not_matched(self, tmp_path):
        # Bare endswith("metadata.json") would incorrectly match this path.
        zpath = self._zip_with_metadata(
            tmp_path, '{"Roles": {"uuid-1": {"Name": "ShouldNotBeFound"}}}',
            path="datamodel/metadata.json",
        )
        assert parse_user_roles_from_zip(zpath) == []

    def test_quote_in_name_skipped(self, tmp_path):
        metadata = '{"Roles": {"uuid-1": {"Name": "Bad\\"Role"}, "uuid-2": {"Name": "Good"}}}'
        zpath = self._zip_with_metadata(tmp_path, metadata)
        assert parse_user_roles_from_zip(zpath) == ["Good"]

    def test_control_char_in_name_skipped(self, tmp_path):
        metadata = '{"Roles": {"uuid-1": {"Name": "Bad\\nRole"}, "uuid-2": {"Name": "Good"}}}'
        zpath = self._zip_with_metadata(tmp_path, metadata)
        assert parse_user_roles_from_zip(zpath) == ["Good"]

    def test_dedupe_preserves_first_occurrence(self, tmp_path):
        metadata = '{"Roles": {"uuid-1": {"Name": "User"}, "uuid-2": {"Name": "User"}}}'
        zpath = self._zip_with_metadata(tmp_path, metadata)
        assert parse_user_roles_from_zip(zpath) == ["User"]
