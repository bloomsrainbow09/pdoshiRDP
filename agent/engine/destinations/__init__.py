"""Publishing targets.

Every destination is the same three operations, so the scheduler never needs to
know whether it is talking to YouTube or Instagram:

    authorize(destination_row)          -> refresh/validate credentials
    publish(destination_row, rendition) -> PublishResult
    quota(destination_row)              -> how many posts are still allowed today

Credentials differ wildly per platform (YouTube = OAuth refresh token; Instagram =
a long-lived IG Graph token plus a business account id), so they live in the
destinations.credentials jsonb column instead of being forced into shared columns.

Nothing here is implemented yet -- this file exists so the seam is real and the
scheduler can be written against it before either platform is wired up.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PublishResult:
    ok: bool
    external_id: str | None = None
    external_url: str | None = None
    error: str | None = None


class Destination(Protocol):
    platform: str

    async def authorize(self, dest: dict) -> bool:
        """Refresh or validate stored credentials. False if a human must re-auth."""
        ...

    async def publish(self, dest: dict, rendition: dict) -> PublishResult:
        """Publish one rendition. MUST be safe to retry: the caller relies on
        posts.UNIQUE(destination_label, rendition_id) to prevent double-posting,
        so return the existing external_id if the upload already landed."""
        ...

    async def quota(self, dest: dict) -> int:
        """Posts still permitted today (destinations.daily_cap minus today's count)."""
        ...


def get(platform: str) -> Destination:
    if platform == "youtube":
        from engine.destinations import youtube
        return youtube.YouTube()
    if platform == "instagram":
        from engine.destinations import instagram
        return instagram.Instagram()
    raise SystemExit(f"unknown destination platform '{platform}'")
