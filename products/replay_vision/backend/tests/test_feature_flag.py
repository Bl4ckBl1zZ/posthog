from types import SimpleNamespace
from uuid import UUID

from unittest.mock import MagicMock, patch

from django.test import override_settings

from products.replay_vision.backend.feature_flag import is_replay_vision_enabled


@override_settings(SELF_CAPTURE=True)
def test_self_hosted_replay_vision_flag_uses_local_evaluation() -> None:
    client = MagicMock()
    client.feature_enabled.return_value = True
    user = SimpleNamespace(distinct_id="user-1", uuid=UUID("00000000-0000-0000-0000-000000000001"))
    team = SimpleNamespace(id=3, organization_id=UUID("00000000-0000-0000-0000-000000000002"))

    with patch(
        "products.replay_vision.backend.feature_flag._self_hosted_flag_client",
        return_value=client,
    ):
        assert is_replay_vision_enabled(user, team) is True

    client.feature_enabled.assert_called_once_with(
        "replay-vision",
        "user-1",
        groups={"organization": str(team.organization_id), "project": "3"},
        group_properties={
            "organization": {"id": str(team.organization_id)},
            "project": {"id": "3"},
        },
        only_evaluate_locally=True,
        send_feature_flag_events=False,
    )
