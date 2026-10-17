import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import os

import pytest


@pytest.fixture(autouse=True)
def _offline_mongomock(request, monkeypatch):
    """Run offline and give each test a fresh in-memory MongoDB (unless marked real_mongo)."""
    os.environ["WORKPLACEOPS_OFFLINE"] = "1"
    if request.node.get_closest_marker("real_mongo"):
        yield
        return
    try:
        import mongomock
        from app.db.mongo import close_client, set_client
    except ImportError:
        yield
        return
    monkeypatch.setenv("WORKPLACEOPS_PERSISTENCE", "required")
    set_client(mongomock.MongoClient(tz_aware=True))
    try:
        yield
    finally:
        close_client()
