"""Shared identity fixtures and controller factory for Network 11 tests."""

from unittest.mock import MagicMock

from .conftest import make_mock_controller

OLD_ID = "5f0123456789abcdef012345"
NEW_ID = "01234567-89ab-cdef-0123-456789abcdef"
OTHER_ID = "abcdef01-2345-6789-abcd-ef0123456789"
MAC = "aa:bb:cc:dd:ee:ff"
PATCH_CONTROLLER = "custom_components.unifi_presence.coordinator.create_controller_for_params"


def network_11_controller() -> MagicMock:
    """Use the common controller factory with a response, not cached evidence."""
    controller = make_mock_controller()
    controller.request.return_value = {"data": [{"_id": NEW_ID, "name": "default"}]}
    return controller
