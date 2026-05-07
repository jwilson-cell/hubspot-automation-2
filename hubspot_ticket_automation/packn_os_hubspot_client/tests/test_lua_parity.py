"""HUB-07 Pitfall 1 - Lua script parity (Python side).

Bilingual marker extractor: accepts either `// LUA-START` (TS) or
`# LUA-START` (Python) as the marker prefix. The captured group is the body
between the opening string-quote (` for TS, triple-double-quote for Python)
and the matching closer.

Activated by Plan 04-03 Task 1.
"""

import re
from pathlib import Path

# Test file lives at:
#   <repo>/python/packn_os_hubspot_client/tests/test_lua_parity.py
# parents[0]=tests, parents[1]=packn_os_hubspot_client, parents[2]=python,
# parents[3]=<repo root>.
REPO_ROOT = Path(__file__).resolve().parents[3]
TS_FILE = REPO_ROOT / "src" / "lib" / "rate-limit.ts"
PY_FILE = REPO_ROOT / "python" / "packn_os_hubspot_client" / "rate_limit.py"

# (?://|#) accepts EITHER comment-prefix; the body is between a
# string-opener (` for TS, """ for Python) and the matching closer. The
# `re.escape` for the triple-quote is not needed because " is not a regex
# metachar.
_MARKER_RE = re.compile(
    r'(?://|#)\s*LUA-START[\s\S]*?(?:`|""")\s*([\s\S]*?)\s*(?:`|""")[\s\S]*?(?://|#)\s*LUA-END',
)


def _extract_lua(file_path: Path) -> str:
    content = file_path.read_text(encoding="utf-8")
    match = _MARKER_RE.search(content)
    if not match:
        raise AssertionError(f"No LUA-START/LUA-END markers found in {file_path}")
    # Normalize CRLF -> LF so the comparison is OS-agnostic. Windows checkouts
    # get \r\n line endings on TS files; Python files use \n. Redis evaluates
    # Lua identically regardless of line ending, so the parity invariant is
    # about the script body's TOKENS, not the literal byte stream.
    return match.group(1).replace("\r\n", "\n").strip()


def test_lua_take_token_script_byte_equals_ts_source():
    """TS and Python copies of TAKE_TOKEN_LUA must match byte-for-byte."""
    ts_script = _extract_lua(TS_FILE)
    py_script = _extract_lua(PY_FILE)
    assert ts_script == py_script, (
        "TS and Python Lua scripts diverged. See RESEARCH 04 Pitfall 1.\n"
        f"TS: {ts_script!r}\n\n"
        f"PY: {py_script!r}"
    )
    # Sanity check
    assert 'redis.call("HMGET"' in ts_script
    assert 'redis.call("PEXPIRE", key, 60000)' in ts_script
