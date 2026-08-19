"""Shared test fixtures."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import Base
from app.main import create_app

# Use a separate test database.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://rs:rs@localhost:5432/remote_support_test",
)

# Populated by ``prepare_test_database`` so every reference is bound to the
# same event loop used by the tests.
_engine = None
_TestSessionLocal = None


event_loop_policy = asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def event_loop():
    loop = event_loop_policy.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def prepare_test_database(event_loop) -> AsyncGenerator[None, None]:
    """Create and drop the test database schema once per session."""
    global _engine, _TestSessionLocal
    _engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        future=True,
        poolclass=NullPool,
    )
    _TestSessionLocal = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _engine.dispose()


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Fresh session per test, rolled back at the end."""
    assert _TestSessionLocal is not None
    async with _TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture(autouse=True)
async def _clear_rate_limits() -> None:
    """Rate-limit counters commit in their own sessions; clear them between tests."""
    assert _TestSessionLocal is not None
    async with _TestSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE rate_limits"))
        await session.commit()


class _FakeLiveKitAPI:
    """Minimal stand-in for LiveKitAPI in endpoint tests."""

    def __init__(self, *args, **kwargs) -> None:
        self.room = self._Room()
        self.agent_dispatch = self._AgentDispatch()

    async def aclose(self) -> None:
        return None

    class _Room:
        async def create_room(self, request):
            return type("Room", (), {"name": request.name})()

        async def delete_room(self, request):
            return None

        async def update_room_metadata(self, request):
            return None

        async def list_rooms(self, request):
            return type("ListRoomsResponse", (), {"rooms": []})()

    class _AgentDispatch:
        async def create_dispatch(self, request):
            return type("Dispatch", (), {"id": "dispatch_123"})()


class _FakeGCSClient:
    """No-op GCS client for lifespan startup."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def bucket(self, name: str):
        return type("Bucket", (), {"exists": lambda self: True})()


@pytest.fixture
def app():
    # Patch cloud clients before the app is constructed so lifespan startup
    # succeeds without real credentials.
    import app.main as main_module

    main_module.api.LiveKitAPI = _FakeLiveKitAPI
    main_module.gcs.Client = _FakeGCSClient

    app = create_app()
    app.state.lkapi = _FakeLiveKitAPI()
    app.state.gcs_client = _FakeGCSClient()

    # Point all DB-using modules at the test session factory.
    import app.db as db_module
    import app.routers.agent as agent_module
    import app.routers.recordings as recordings_module
    import app.routers.sessions as sessions_module
    import app.routers.transcripts as transcripts_module
    import app.routers.webhooks as webhooks_module
    import app.services.egress as egress_module
    import app.services.rate_limit as rate_limit_module

    for module in (
        db_module,
        sessions_module,
        agent_module,
        transcripts_module,
        recordings_module,
        webhooks_module,
        egress_module,
        rate_limit_module,
    ):
        module.AsyncSessionLocal = _TestSessionLocal
    return app


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client that exercises the app on the test event loop."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
