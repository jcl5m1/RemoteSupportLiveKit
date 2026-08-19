"""Stub clients used when ALLOW_DEGRADED_START is true.

These let the backend boot without real LiveKit / GCS credentials. Every
actual API call raises, so code paths that touch the cloud fail cleanly and
/readyz reports the dependency unhealthy.
"""

from __future__ import annotations

from livekit import api


class DegradedLiveKitAPI:
    """Stand-in for livekit.api.LiveKitAPI when credentials are absent."""

    _error = RuntimeError("LiveKit API is degraded: missing/invalid credentials")

    class _Room:
        @staticmethod
        async def list_rooms(_request: api.ListRoomsRequest) -> None:
            raise DegradedLiveKitAPI._error

        @staticmethod
        async def delete_room(_request: api.DeleteRoomRequest) -> None:
            raise DegradedLiveKitAPI._error

    room = _Room()

    async def aclose(self) -> None:
        pass


class DegradedGCSClient:
    """Stand-in for google.cloud.storage.Client when credentials are absent."""

    _error = RuntimeError("GCS client is degraded: missing/invalid credentials")

    class _Bucket:
        def __init__(self, name: str) -> None:
            self.name = name

        def exists(self) -> bool:
            raise DegradedGCSClient._error

        def blob(self, _name: str) -> DegradedGCSClient._Blob:
            return DegradedGCSClient._Blob()

    class _Blob:
        def generate_signed_url(self, **_kwargs: object) -> str:
            raise DegradedGCSClient._error

        def upload_from_string(self, _data: object, **_kwargs: object) -> None:
            raise DegradedGCSClient._error

    def __init__(self, bucket_name: str) -> None:
        self._bucket_name = bucket_name

    def bucket(self, name: str) -> _Bucket:
        return self._Bucket(name)
