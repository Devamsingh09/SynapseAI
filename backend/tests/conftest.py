import os
import sys

# Allow `import adaptive_loop` / `import chatbot_backend` when pytest is run
# from the repo root or from backend/ — no package __init__.py needed.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
