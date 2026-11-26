import os

# No test may ever reach the developer's real key: every test uses a keychain that does not exist, or its own.
os.environ["CODING_AGENT_KEYCHAIN"] = "/nonexistent/coding-agent-v2-tests.keychain"
