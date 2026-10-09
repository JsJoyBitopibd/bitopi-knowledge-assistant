import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Every CSV log (calls, chat, sql, sql_gen, schema_select) goes to a throwaway folder during tests, never to
# the repo's logs/ (P0: generate_and_run logs every attempt, and the test fakes would let that through).
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="ragbot-test-logs-"))
