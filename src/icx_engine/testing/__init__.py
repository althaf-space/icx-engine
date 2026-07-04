import warnings

# langgraph pulls in langchain-core which ships a Pydantic V1 shim.
# The shim emits a UserWarning on Python 3.14+ at import time.
# Suppress it here so it never reaches the user's console.
warnings.filterwarnings(
    "ignore",
    message="Core Pydantic V1 functionality",
    category=UserWarning,
    module="langchain_core",
)
