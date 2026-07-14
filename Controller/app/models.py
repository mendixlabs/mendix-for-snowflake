from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from .pad_parser import CONSTANT_NAME_PATTERN, USER_ROLE_NAME_MAX

_CONSTANT_NAME_RE = re.compile(CONSTANT_NAME_PATTERN)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_ROLE_MAPPING_MAX_ENTRIES = 50

# The four per-app EAI reference slots declared in manifest.yml (app_eai_1..4).
# Fixed and small, so a plain tuple/frozenset - not derived from env - is the
# structural "is this a legal slot key" check; whether a given slot is actually
# bound is separate, runtime state (main.BOUND_EAI_SLOTS).
EAI_SLOT_KEYS: tuple[str, ...] = ("app_eai_1", "app_eai_2", "app_eai_3", "app_eai_4")
_EAI_SLOT_SET = frozenset(EAI_SLOT_KEYS)

# Sentinel returned in place of constant values everywhere they leave the
# controller (registry rows, API responses). Submitting it back means "keep the
# existing secret"; the literal string is therefore reserved and can never be
# stored as a real constant value.
HIDDEN_VALUE = "<HIDDEN>"


def _validate_eai_slots(slots: list[str]) -> list[str]:
    # Structural check only (is this one of the four declared slot keys?) - a
    # requested slot that IS a legal key but isn't currently bound is rejected
    # separately at the endpoint level (main.py), which is the only place that
    # knows the runtime bind state.
    unknown = sorted(set(slots) - _EAI_SLOT_SET)
    if unknown:
        raise ValueError(
            f"unknown external_access slot(s) {unknown}; must be a subset of {list(EAI_SLOT_KEYS)}"
        )
    return slots


def _validate_constant_names(constants: dict[str, str]) -> dict[str, str]:
    # Constant names become Snowflake secret identifiers (MX_CONST_<name>), so a
    # name with quotes/spaces/semicolons could break out of the identifier
    # position in CREATE SECRET. Reject anything that is not a dotted identifier.
    for key in constants:
        if not _CONSTANT_NAME_RE.match(key):
            raise ValueError(
                f"invalid constant name {key!r}: must match {CONSTANT_NAME_PATTERN}"
            )
    return constants


class ResourceTier(str, Enum):
    small = "small"
    medium = "medium"
    large = "large"


RESOURCE_TIERS = {
    ResourceTier.small:  {"cpu_request": "0.25", "cpu_limit": "0.5",  "mem_request": "512M", "mem_limit": "1G"},
    ResourceTier.medium: {"cpu_request": "0.5",  "cpu_limit": "1",    "mem_request": "1G",   "mem_limit": "2G"},
    ResourceTier.large:  {"cpu_request": "1",    "cpu_limit": "2",    "mem_request": "2G",   "mem_limit": "4G"},
}


class CreateAppRequest(BaseModel):
    # The name is embedded (uppercased) in derived Snowflake identifiers
    # (MXAPP_<NAME> schema, <NAME>_SERVICE, app_<name>_user role), so it is
    # restricted to identifier characters and capped well under Snowflake's
    # 255-char identifier limit. Uniqueness is checked case-insensitively at
    # registration because the derived identifiers are case-insensitive.
    name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]*$", max_length=50,
                      description="App identifier (letters, digits, underscores; max 50)")
    # Flows into the runtime's CREATE DATABASE (shell psql) and the service spec;
    # constrain to an identifier so it cannot inject SQL/shell metacharacters.
    pg_database: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    admin_password: str
    resource_tier: ResourceTier = ResourceTier.medium
    use_caller_rights: bool = False
    constants: dict[str, str] = Field(default_factory=dict)
    # Interpolated into GRANT … TO ROLE; constrain to an identifier so a privileged
    # caller can't inject SQL via this field (the UI restricts it, the API didn't).
    owner_role: str = Field(default="MENDIX_ADMIN_OPERATOR_ROLE", pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    # Optional: born-licensed app. license_id is an identifier (plain env var later);
    # license_key is a credential and is never persisted - it only ever reaches
    # sf.create_or_replace_secret. Both or neither: a key with no id, or an id with
    # no key, is a half-configured license.
    license_id: Optional[str] = None
    license_key: Optional[str] = None
    # Which of the four declared app_eai_N slots this app's service should attach
    # to. Structurally validated here (must be a known slot key); whether each
    # requested slot is actually bound is checked at request time in main.py
    # (422 if not) - a new app is never silently created with fewer integrations
    # than requested.
    external_access: list[str] = Field(default_factory=list)

    _check_constants = field_validator("constants")(_validate_constant_names)
    _check_external_access = field_validator("external_access")(_validate_eai_slots)

    @model_validator(mode="after")
    def _check_license_pair(self) -> "CreateAppRequest":
        if bool(self.license_id) != bool(self.license_key):
            raise ValueError("license_id and license_key must be provided together")
        return self


class UpdateConstantsRequest(BaseModel):
    constants: dict[str, str]

    _check_constants = field_validator("constants")(_validate_constant_names)


class UpdateSpecRequest(BaseModel):
    resource_tier: Optional[ResourceTier] = None
    use_caller_rights: Optional[bool] = None


class UpdateExternalAccessRequest(BaseModel):
    """Body for PUT /apps/{name}/external-access. The full desired slot set,
    not a delta (same replace-the-whole-value semantics as UpdateRoleMappingRequest) -
    an empty list detaches every currently-attached slot."""
    slots: list[str]

    _check_slots = field_validator("slots")(_validate_eai_slots)


class RollbackRequest(BaseModel):
    """Optional body for POST /apps/{name}/rollback. Omitted (or entry_id=None)
    means the default path: roll back to the last successful deploy."""
    entry_id: Optional[int] = None


class UpdateLicenseRequest(BaseModel):
    # Write-only: unlike constants there is no HIDDEN_VALUE sentinel here, so every
    # PUT must carry a real key - there is nothing stored to "keep unchanged".
    license_id: str = Field(..., min_length=1)
    license_key: str = Field(..., min_length=1)


class UpdateRoleMappingRequest(BaseModel):
    """Snowflake account role name -> Mendix userrole name. Keys are normalized
    to uppercase (Snowflake role names are case-insensitive identifiers)."""
    role_mapping: dict[str, str]

    @field_validator("role_mapping")
    @classmethod
    def _check_role_mapping(cls, mapping: dict[str, str]) -> dict[str, str]:
        if not mapping:
            raise ValueError("role_mapping must not be empty; use DELETE to clear it")
        if len(mapping) > _ROLE_MAPPING_MAX_ENTRIES:
            raise ValueError(f"role_mapping cannot have more than {_ROLE_MAPPING_MAX_ENTRIES} entries")

        result: dict[str, str] = {}
        for raw_key, raw_val in mapping.items():
            key = raw_key.strip()
            val = raw_val.strip()
            if not key:
                raise ValueError("role_mapping keys must not be empty")
            if not val:
                raise ValueError("role_mapping values must not be empty")
            if len(key) > 255:
                raise ValueError(f"role_mapping key {key!r} exceeds 255 characters")
            if len(val) > USER_ROLE_NAME_MAX:
                raise ValueError(f"role_mapping value {val!r} exceeds {USER_ROLE_NAME_MAX} characters")
            if _CONTROL_CHAR_RE.search(key) or _CONTROL_CHAR_RE.search(val):
                raise ValueError("role_mapping keys/values must not contain control characters")
            if "'" in val or '"' in val:
                # Values feed the Java XPath lookup //System.UserRole[Name='...'].
                raise ValueError(f"role_mapping value {val!r} must not contain quotes")
            upper_key = key.upper()
            if upper_key in result:
                raise ValueError(f"duplicate role_mapping key after uppercasing: {upper_key!r}")
            result[upper_key] = val
        return result


class AppRecord(BaseModel):
    name: str
    service_name: str
    # Per-app schema (MXAPP_<NAME>, unqualified) holding the app's secrets and
    # filestorage stage. Stored rather than derived so ownership never depends
    # on reconstructing a naming convention.
    app_schema: str
    pg_database: str
    resource_tier: str
    use_caller_rights: bool
    constants: dict[str, str]
    owner_role: str = "MENDIX_ADMIN_OPERATOR_ROLE"
    # Identifier, not a credential: safe to store and return as-is. The license key
    # never appears on this model or anywhere else that leaves the controller; it
    # lives only in the per-app MX_LICENSE_KEY secret.
    license_id: Optional[str] = None
    user_roles: list[str] = Field(default_factory=list)       # detected from PAD at deploy
    role_mapping: dict[str, str] = Field(default_factory=dict)  # operator-set; not a secret, never masked
    # WS0 schema groundwork (consumed by later workstreams): failure detail, bound
    # per-app EAI slot keys, and platform (base image) staleness tracking.
    status_detail: Optional[str] = None
    failed_operation: Optional[str] = None
    external_access: list[str] = Field(default_factory=list)  # bound app_eai_N slot keys attached to this app
    platform_image: Optional[str] = None       # MENDIX_BASE_IMAGE at last respec
    platform_update_available: bool = False
    pad_stage_path: Optional[str]
    endpoint_url: Optional[str]
    last_deploy_status: Optional[str]
    created_at: Optional[str]
    last_deployed_at: Optional[str]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def licensed(self) -> bool:
        return bool(self.license_id)


class AppStatusResponse(BaseModel):
    app: AppRecord
    service_status: Optional[str]


class UpdateComputePoolRequest(BaseModel):
    # Upper bounds cap runaway compute scaling (cost / compute-abuse guard). 10 nodes
    # of the pool's small instance family is ample headroom for Mendix workloads; raise
    # deliberately if a consumer genuinely needs more.
    min_nodes: Optional[int] = Field(None, ge=1, le=10)
    max_nodes: Optional[int] = Field(None, ge=1, le=10)
    auto_suspend_secs: Optional[int] = Field(None, ge=0)


class EgressAckRequest(BaseModel):
    """Body for POST /system/egress-ack. Pydantic's `date` type already
    enforces ISO 8601 (YYYY-MM-DD), rejecting anything else with a 422."""
    through_date: date


class EgressAlertConfigRequest(BaseModel):
    """Body for POST /system/egress-alert-config. Both fields may be empty to
    clear the alert configuration; an integration with no recipients (or vice
    versa) is stored as-is but never sends (egress_watch skips silently when
    either half is unconfigured)."""
    integration_name: str = ""
    recipients: list[str] = Field(default_factory=list)

    @field_validator("recipients")
    @classmethod
    def _check_recipients(cls, recipients: list[str]) -> list[str]:
        cleaned = []
        for raw in recipients:
            r = raw.strip()
            if not r:
                continue
            if _CONTROL_CHAR_RE.search(r):
                raise ValueError(f"recipient {r!r} must not contain control characters")
            if "@" not in r:
                raise ValueError(f"recipient {r!r} does not look like an email address")
            cleaned.append(r)
        return cleaned
