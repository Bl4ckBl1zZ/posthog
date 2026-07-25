from functools import lru_cache
from typing import TYPE_CHECKING

from django.conf import settings

import posthoganalytics
from rest_framework.exceptions import NotFound
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from posthog.utils import _build_flag_provider

if TYPE_CHECKING:
    from posthog.models.team.team import Team
    from posthog.models.user import User

REPLAY_VISION_FEATURE_FLAG = "replay-vision"
# Gates the "and then…" VisionAction sub-feature, separate from product access above.
REPLAY_VISION_ACTIONS_FEATURE_FLAG = "replay-vision-actions"


@lru_cache(maxsize=1)
def _self_hosted_flag_client() -> posthoganalytics.Client:
    client = posthoganalytics.Client(
        "self-hosted-local-feature-evaluation",
        host=settings.SITE_URL,
        personal_api_key="local-cache-only",
        send=False,
        enable_local_evaluation=True,
        flag_definition_cache_provider=_build_flag_provider(),
    )
    client.load_feature_flags()
    return client


def _vision_flag_enabled(flag_key: str, user: "User", team: "Team") -> bool:
    distinct_id = user.distinct_id or str(user.uuid)
    organization_id = str(team.organization_id)
    project_id = str(team.id)
    client = _self_hosted_flag_client() if settings.SELF_CAPTURE else posthoganalytics
    return bool(
        client.feature_enabled(
            flag_key,
            distinct_id,
            groups={"organization": organization_id, "project": project_id},
            group_properties={"organization": {"id": organization_id}, "project": {"id": project_id}},
            only_evaluate_locally=settings.SELF_CAPTURE,
            send_feature_flag_events=False,
        )
    )


def is_replay_vision_enabled(user: "User", team: "Team") -> bool:
    return _vision_flag_enabled(REPLAY_VISION_FEATURE_FLAG, user, team)


def is_replay_vision_actions_enabled(user: "User", team: "Team") -> bool:
    return _vision_flag_enabled(REPLAY_VISION_ACTIONS_FEATURE_FLAG, user, team)


class ReplayVisionEnabledPermission(BasePermission):
    """Hide Vision endpoints behind the `replay-vision` flag: 404 (not 403) when off."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        if not is_replay_vision_enabled(request.user, view.team):  # type: ignore[arg-type, attr-defined]
            raise NotFound()
        return True


class ReplayVisionActionsEnabledPermission(BasePermission):
    """Hide Vision *action* endpoints behind the `replay-vision-actions` flag: 404 (not 403) when off."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        if not is_replay_vision_actions_enabled(request.user, view.team):  # type: ignore[arg-type, attr-defined]
            raise NotFound()
        return True
