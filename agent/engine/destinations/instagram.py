"""Instagram destination — not implemented yet.

Shape of the work, recorded so the design is not re-derived later:

  Account   Only a Business or Creator account can be posted to via API, and it must
            be linked to a Facebook Page. A personal IG account cannot publish
            through the Graph API at all — this is the first blocker to clear.

  Auth      Facebook Login -> long-lived token (60 days) -> must be refreshed before
            expiry or it dies silently. Unlike YouTube's refresh token this one has
            a hard clock, so a renewal job is mandatory, not optional.

  Publish   Two steps, always: POST /{ig-user-id}/media to create a container, then
            POST /{ig-user-id}/media_publish with its creation_id. Reels need the
            video reachable at a PUBLIC URL — the Graph API fetches it itself, it
            does not accept an upload. So engine/media must be able to hand out a
            public (or signed) link; a private Drive file will not work.

  Limits    25 published posts per rolling 24h per account. Carousels count as one.

  Retry     media_publish with the same creation_id is safe; creating a second
            container is what duplicates. Persist creation_id on the post row before
            publishing so a retry resumes rather than restarts.
"""

from engine.destinations import PublishResult


class Instagram:
    platform = "instagram"

    async def authorize(self, dest: dict) -> bool:
        raise NotImplementedError("Instagram auth not wired up yet")

    async def publish(self, dest: dict, rendition: dict) -> PublishResult:
        raise NotImplementedError("Instagram publish not wired up yet")

    async def quota(self, dest: dict) -> int:
        raise NotImplementedError("Instagram quota not wired up yet")
