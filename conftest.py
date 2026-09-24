import os

os.environ["INVENTRA_TESTING"] = "1"
os.environ["MONGO_CONNECTION"] = "mongodb://localhost:27017/?serverSelectionTimeoutMS=50"
if os.getenv("RUN_LLM_TESTS") != "1":
    os.environ["GEMINI_API_KEY"] = "test-key"
    os.environ["GROQ_API_KEY"] = "test-key"
