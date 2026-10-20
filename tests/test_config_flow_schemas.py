"""Regression tests for native Probatio config-flow validation."""

import probatio
import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from probatio.humanize import humanize_error

from custom_components.unifi_presence.config_flow import _build_credentials_schema
from custom_components.unifi_presence.const import CONF_SSL_VERIFY, DEFAULT_PORT, DEFAULT_SSL_VERIFY, DOMAIN

from .conftest import MOCK_CONFIG_DATA, add_mock_config_entry

pytestmark = pytest.mark.usefixtures("_bypass_setup")


@pytest.fixture(params=["user", "reconfigure", "reauth"])
async def credential_form(
    hass: HomeAssistant, request: pytest.FixtureRequest
) -> tuple[probatio.Schema, dict[str, object]]:
    """Get each real credential form without submitting credentials to a controller."""
    if request.param == "user":
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    else:
        entry = add_mock_config_entry(hass)
        if request.param == "reconfigure":
            result = await entry.start_reconfigure_flow(hass)
        else:
            result = await entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    assert isinstance(schema, probatio.Schema)
    user_input: dict[str, object] = {CONF_USERNAME: "new-admin", CONF_PASSWORD: "new-password"}
    if CONF_HOST in schema.schema:
        user_input[CONF_HOST] = "192.0.2.1"
    return schema, user_input


def test_credential_form_preserves_submitted_password(
    credential_form: tuple[probatio.Schema, dict[str, object]],
) -> None:
    """Secret markers redact errors, not valid credentials saved by the flow."""
    schema, user_input = credential_form
    validated = schema(user_input)
    assert validated[CONF_USERNAME] == user_input[CONF_USERNAME]
    assert validated[CONF_PASSWORD] == user_input[CONF_PASSWORD]


def test_credential_form_never_defaults_stored_password(
    credential_form: tuple[probatio.Schema, dict[str, object]],
) -> None:
    """Every credential form requires a fresh password rather than reusing storage."""
    schema, user_input = credential_form
    password_field = next(key for key in schema.schema if str(key) == CONF_PASSWORD)
    assert isinstance(password_field, probatio.Required)
    assert isinstance(password_field.schema, probatio.Secret)
    assert password_field.default is probatio.UNDEFINED
    del user_input[CONF_PASSWORD]
    with pytest.raises(probatio.MultipleInvalid):
        schema(user_input)


def test_credential_form_redacts_invalid_password(
    credential_form: tuple[probatio.Schema, dict[str, object]],
) -> None:
    """Human-readable validation errors must not include malformed credential values."""
    schema, user_input = credential_form
    secret = "invalid-password-do-not-log"
    user_input[CONF_PASSWORD] = {"secret": secret}
    with pytest.raises(probatio.MultipleInvalid) as raised:
        schema(user_input)
    assert raised.value.path == [CONF_PASSWORD]
    assert secret not in str(raised.value)
    assert secret not in humanize_error(user_input, raised.value)


def test_credential_form_serializes_secret_password_for_frontend(
    credential_form: tuple[probatio.Schema, dict[str, object]],
) -> None:
    """Home Assistant can render secret password fields without exposing stored values."""
    schema, _ = credential_form
    fields = probatio.to_field_list(schema, custom_serializer=cv.custom_serializer)
    password_field = next(field for field in fields if field["name"] == CONF_PASSWORD)
    assert password_field == {"type": "string", "name": CONF_PASSWORD, "required": True, "secret": True}


@pytest.mark.parametrize("defaults", [None, MOCK_CONFIG_DATA], ids=["setup", "reconfigure"])
def test_credential_schema_preserves_defaults(defaults: dict[str, object] | None) -> None:
    """The native schema retains credential defaults without inserting a password."""
    schema = _build_credentials_schema(defaults)
    user_input = {CONF_HOST: "192.0.2.1", CONF_USERNAME: "admin", CONF_PASSWORD: "password"}
    validated = schema(user_input)
    assert validated == {
        **user_input,
        CONF_PORT: DEFAULT_PORT if defaults is None else defaults[CONF_PORT],
        CONF_SSL_VERIFY: DEFAULT_SSL_VERIFY if defaults is None else defaults[CONF_SSL_VERIFY],
    }
    if defaults is not None:
        assert schema({CONF_PASSWORD: "replacement-password"}) == {
            **{key: defaults[key] for key in (CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_SSL_VERIFY)},
            CONF_PASSWORD: "replacement-password",
        }


@pytest.mark.parametrize("port", [1, "8443", 65535])
def test_credential_schema_accepts_valid_ports(port: int | str) -> None:
    """Port validation preserves valid bounds and converts numeric form input."""
    validated = _build_credentials_schema()(
        {CONF_HOST: "192.0.2.1", CONF_USERNAME: "admin", CONF_PASSWORD: "password", CONF_PORT: port}
    )
    assert validated[CONF_PORT] == int(port)


@pytest.mark.parametrize("port", [0, 65536, -1, True, 8443.5, "invalid"])
def test_credential_schema_rejects_invalid_ports(port: object) -> None:
    """Invalid and lossy port values never reach the controller."""
    with pytest.raises(probatio.MultipleInvalid):
        _build_credentials_schema()(
            {CONF_HOST: "192.0.2.1", CONF_USERNAME: "admin", CONF_PASSWORD: "password", CONF_PORT: port}
        )
