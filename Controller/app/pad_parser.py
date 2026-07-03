"""
Port of Get-PadConstants from Deploy Script/deploy.ps1.

Parses etc/constants/defaults.conf and etc/constants/variables.conf from an
extracted PAD directory, returning only constants that appear in both files.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

# A Mendix constant's qualified name (Module.Constant) is always a dotted
# identifier. We turn it into a Snowflake secret identifier (MX_CONST_...), so it
# must contain nothing that could break out of an identifier position in DDL.
# Enforced here (PAD is untrusted input) and re-used by the API models.
CONSTANT_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_.]*$"
_CONSTANT_NAME_RE = re.compile(CONSTANT_NAME_PATTERN)

# Mendix userrole names may contain spaces, so this is a length/safety cap, not
# an identifier pattern. Also re-used by UpdateRoleMappingRequest in models.py.
USER_ROLE_NAME_MAX = 200
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class PadConstant:
    name: str        # "Module.ConstantName"
    env_var: str     # env var name from variables.conf
    default: str     # default value from defaults.conf
    secret_name: str # "MX_CONST_MODULE_CONSTANTNAME"


def _parse_defaults(text: str) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^\s*"([^"]+)"\s*=\s*(.*)$', line)
        if m:
            name = m.group(1)
            val = m.group(2).strip()
            # Strip surrounding quotes
            if re.match(r'^"(.*)"$', val):
                val = val[1:-1]
            defaults[name] = val
    return defaults


def _parse_variables(text: str) -> dict[str, str]:
    env_vars: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r'^\s*"([^"]+)"\s*=\s*\$\{\?([^}]+)\}', line)
        if m:
            env_vars[m.group(1)] = m.group(2)
    return env_vars


def _build_constants(defaults: dict[str, str], env_vars: dict[str, str]) -> list[PadConstant]:
    result = []
    for name, default in defaults.items():
        if name in env_vars:
            if not _CONSTANT_NAME_RE.match(name):
                raise ValueError(
                    f"PAD constant name {name!r} is not a valid identifier "
                    f"(must match {CONSTANT_NAME_PATTERN}); refusing to derive a secret name from it"
                )
            secret_name = "MX_CONST_" + name.replace(".", "_").upper()
            result.append(PadConstant(
                name=name,
                env_var=env_vars[name],
                default=default,
                secret_name=secret_name,
            ))
    return result


def parse_from_directory(pad_dir: str | Path) -> list[PadConstant]:
    pad_dir = Path(pad_dir)
    defaults_file = pad_dir / "etc" / "constants" / "defaults.conf"
    variables_file = pad_dir / "etc" / "constants" / "variables.conf"

    if not defaults_file.exists() or not variables_file.exists():
        return []

    defaults = _parse_defaults(defaults_file.read_text(encoding="utf-8"))
    env_vars = _parse_variables(variables_file.read_text(encoding="utf-8"))
    return _build_constants(defaults, env_vars)


def parse_from_zip(zip_path: str | Path) -> list[PadConstant]:
    """Parse constants from a PAD zip without fully extracting it."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

        def _read(suffix: str) -> str | None:
            # Handle both flat layout and single-directory layout inside zip
            candidates = [n for n in names if n.endswith(suffix)]
            if not candidates:
                return None
            # Prefer the shortest path (closest to root)
            candidates.sort(key=len)
            with zf.open(candidates[0]) as f:
                return f.read().decode("utf-8")

        defaults_text = _read("etc/constants/defaults.conf")
        variables_text = _read("etc/constants/variables.conf")

        if defaults_text is None or variables_text is None:
            return []

        defaults = _parse_defaults(defaults_text)
        env_vars = _parse_variables(variables_text)
        return _build_constants(defaults, env_vars)


def _valid_user_role_name(name: str) -> bool:
    if not name or len(name) > USER_ROLE_NAME_MAX:
        return False
    if _CONTROL_CHAR_RE.search(name):
        return False
    # Quotes would break out of the Java XPath lookup
    # //System.UserRole[Name='...'] in HeaderSSOHandler.java; reject them here
    # rather than trusting every downstream consumer to escape correctly.
    if "'" in name or '"' in name:
        return False
    return True


def parse_user_roles_from_zip(zip_path: str | Path) -> list[str]:
    """Userrole names from model/metadata.json inside the PAD, [] on any problem.

    Role detection is auxiliary (it only pre-populates the role-mapping UI and
    validates mapping targets), so any failure here must never fail a deploy.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            candidates = [
                n for n in names
                if n == "model/metadata.json" or n.endswith("/model/metadata.json")
            ]
            if not candidates:
                return []
            candidates.sort(key=len)
            with zf.open(candidates[0]) as f:
                data = json.loads(f.read().decode("utf-8"))

            roles = data.get("Roles") or {}
            result: list[str] = []
            seen: set[str] = set()
            for value in roles.values():
                if not isinstance(value, dict):
                    continue
                raw_name = value.get("Name")
                if not isinstance(raw_name, str):
                    continue
                name = raw_name.strip()
                if not _valid_user_role_name(name):
                    logger.warning("Skipping invalid userrole name from PAD metadata.json: %r", raw_name)
                    continue
                if name in seen:
                    continue
                seen.add(name)
                result.append(name)
            return result
    except Exception:
        logger.warning("Failed to parse userroles from %s", zip_path, exc_info=True)
        return []
