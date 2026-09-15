"""YouTube destination — not implemented yet.

Shape of the work, recorded so the design is not re-derived later:

  Auth      OAuth 2.0, scope youtube.upload (+ youtube.readonly for quota checks).
            One consent per channel; store the refresh token in
            destinations.credentials. Same pattern as the Drive OAuth already in
            keys/gdrive_oauth_token.json — and the same gotcha: PUBLISH the OAuth
            app in GCP or refresh tokens expire after 7 days (see docs/03).

  Upload    videos.insert, resumable, multipart. A Short is simply a video that is
            <= 60s and 9:16 — there is no separate Shorts endpoint.

  Quota     The real constraint is not posts/day but the API quota: 10,000 units/day
            per project, and videos.insert costs ~1600 — so ~6 uploads/day per
            PROJECT, shared across every channel using that project. Several
            channels therefore need either several GCP projects or a quota increase.
            This is the single biggest design constraint on the YouTube side.

  Retry     videos.insert is NOT idempotent. Rely on
            posts.UNIQUE(destination_label, rendition_id) and re-check the channel's
            recent uploads before retrying, or a timeout becomes a duplicate video.
"""

from engine.destinations import PublishResult


class YouTube:
    platform = "youtube"

    async def authorize(self, dest: dict) -> bool:
        raise NotImplementedError("YouTube auth not wired up yet")

    async def publish(self, dest: dict, rendition: dict) -> PublishResult:
        raise NotImplementedError("YouTube publish not wired up yet")

    async def quota(self, dest: dict) -> int:
        raise NotImplementedError("YouTube quota not wired up yet")
