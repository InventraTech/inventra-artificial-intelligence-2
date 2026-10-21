import os

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("GROQ_API_KEY", "test-key")
os.environ["IAI_AUTH_MODE"] = "mock"
os.environ["IAI_BUSINESS_MODE"] = "mock"
os.environ["IAI_STORAGE_MODE"] = "mock"
os.environ["IAI_LLM_MODE"] = "demo"
os.environ["LANGSMITH_TRACING"] = "false"

import pytest


@pytest.fixture(autouse=True)
def isolated_database():
    from iai.app.core.infra.storage import get_db, initialize_storage

    db = get_db()
    for collection in db.list_collection_names():
        db.drop_collection(collection)
    initialize_storage()
    yield
