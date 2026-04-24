# Code Review — HubSpot Ticket Automation (Sheets Export)

Date: 2026-04-23
Reviewer: gsd-code-reviewer
Scope: Sheets export pipeline + edits to existing skill infrastructure

## Summary

Overall a solid, thoughtful design. The two-zone (skill-owned vs operator-owned) invariant is enforced well at the dedupe layer, the local CSV mirror is a genuinely good recovery story, and the header-name-by-lookup approach for column resolution is the right call. The pipeline works.

However, there are a handful of real bugs that will bite: (a) a schema mismatch between what the skill emits at step 2h and what the operator-owned columns in `mispack_log` / `carrier_issue_log` expect for boolean coercion, (b) a subtle but important bug in `combine_with_pending` that will drop current-run rows if an earlier pending payload already contains the same `ticket_id` (the skill run after a failed run will silently lose the current-run row for a ticket that was already in the retry queue — no, wait, actually the opposite — see CR-02 for the correct analysis), (c) a race/corruption risk on the pending queue when two runs overlap, and (d) a TOCTOU+concurrency gap on `sheets_state.json` and `sheets_token.json`.

Counts: 2 Critical · 6 High · 8 Medium · 6 Low/Nit · 5 Enhancements.

---

## Critical

### [CRITICAL] Pending-queue retry loses current-run rows for tickets already queued from prior failure
**File:** `scripts/sheets_sync.py:268-289, 332-351`
**Finding:** `combine_with_pending` concatenates pending batches + current batch, then `append_rows_for_tab` dedupes by `ticket_id`. Because pending batches are iterated *first* and the `existing` set is populated from the sheet (not from the merged list), duplicates *within* the merged list are removed by `existing.add(key)` inside the loop — the **first** occurrence wins. Scenario: run N queues ticket `T` (Sheets was down); run N+1 re-processes ticket `T` (operator added a new note so dedupe in `state.json` said re-draft), so the current payload also has a row for `T`. `combine_with_pending` puts pending first, current second. The pending row wins, the *newer* current-run row is silently dropped.
**Impact:** The sheet reflects stale data from a prior run (older `classifier_confidence`, older `issue_description`, older `priority`). Operator sees an outdated snapshot; newer draft_note_id is lost. This is subtle and will never produce a visible error.
**Suggested fix:** Either (a) put current batch *first* in the merge order so it wins over stale pending, (b) merge-by-ticket_id inside `combine_with_pending` with "current overrides pending" semantics, or (c) document this as intended and have step 2h/state dedupe prevent re-processing a ticket whose row is still queued. Option (a) is a one-line fix:
```python
for batch in [current] + pending:   # was: pending + [current]
```

### [CRITICAL] `sheets_state.json` committed to repo (contains live spreadsheet_id + ouid)
**File:** `config/sheets_state.json:1-10` and `.gitignore:1-3`
**Finding:** `.gitignore` only excludes `config/.secrets/` and `*.token.json`. `config/sheets_state.json` is tracked in the repo and contains the live workbook `spreadsheet_id`, a URL with `ouid=112982160640775058880` (Google user ID of the owner), and tab IDs. While the `drive.file` scope restricts external access to files created by the app, the `ouid` leaks the owner's Google user ID, and the spreadsheet_id leaks the workbook location to anyone who gets read access to the repo (they cannot open it without being shared, but it's still metadata leakage). More importantly, `config/pending_actions.json` and `config/state.json` are *also* not in `.gitignore` and contain `ticket_fingerprints` with HubSpot ticket IDs and `processed_ticket_ids` — customer-adjacent operational data.
**Impact:** Accidental repo publication (fork, push to wrong remote, share with contractor) leaks (a) owner's Google user id, (b) workbook/tab identifiers, (c) ticket IDs and processing history. None are catastrophic in isolation; together they make this a clear data-minimization violation for a help-desk system.
**Suggested fix:** Add to `.gitignore`:
```
config/sheets_state.json
config/sheets_pending_sync.json
config/state.json
config/pending_actions.json
outputs/
```
Ship a `config/state.json.example` and `config/sheets_state.json.example` with safe placeholder values. If these files are already committed, `git rm --cached` them (operator must decide whether to also purge history).

---

## High

### [HIGH] Boolean coercion produces `"TRUE"`/`"FALSE"` strings but source form fields are already strings of unknown shape
**File:** `scripts/sheets_sync.py:129-134` and `SKILL.md:281, 301`
**Finding:** `_coerce` only converts `isinstance(v, bool)` — native Python `True`/`False`. But SKILL.md step 2h instructs Claude to copy `ticket_context.form_fields.check_box_to_reship_order_immediately` and `insurance_on_package` straight through, and those come from HubSpot custom properties as **strings** (`"true"`, `"false"`, or sometimes `""` / `"yes"` / `"no"` depending on the form field type). Neither the skill nor the sync script normalizes these. Result: Sheet cells will contain literal lowercase `"true"` / `"false"` / `"yes"` / `""` — inconsistent with the `"TRUE"`/`"FALSE"` convention implied by `_coerce` and not friendly to sheet filters/pivots.
**Impact:** Data integrity degradation. Filter formulas (`=COUNTIF(F:F, "TRUE")`) will under-count. Operator-facing inconsistency as the skill evolves and some rows come in as bools, others as strings.
**Suggested fix:** Extend `_coerce` (or add an explicit boolean-field whitelist) to normalize common string-boolean inputs to `TRUE`/`FALSE`. E.g.:
```python
_TRUE_STRINGS = {"true", "yes", "1", "y", "on"}
_FALSE_STRINGS = {"false", "no", "0", "n", "off", ""}

def _coerce_bool_like(v: Any) -> Any:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, str) and v.strip().lower() in _TRUE_STRINGS:
        return "TRUE"
    if isinstance(v, str) and v.strip().lower() in _FALSE_STRINGS:
        return "FALSE" if v.strip() else ""   # preserve truly-empty as empty
    return v
```
Apply per-column based on a schema hint (safer than coercing all strings globally). Alternatively: document in SKILL.md step 2h that Claude must normalize `reshipment_needed` and `insurance_on_package` to Python `True`/`False` before appending to the buffer.

### [HIGH] Concurrent runs corrupt `sheets_pending_sync.json` (read-modify-write race)
**File:** `scripts/sheets_sync.py:167-181, 318-319, 350, 354-356`
**Finding:** `load_pending` → `save_pending` is a classic non-atomic read-modify-write with no locking. Two `sheets_sync.py` processes running concurrently (e.g., cron fires while previous run is slow, or operator manually invokes during cron) will both: read queue at state-N, each append/clear, each write. Last writer wins — prior run's queue additions are lost. This is the exact failure mode that pending queues exist to prevent.
**Impact:** Failed sync payloads silently disappear, defeating the retry guarantee advertised in the docstring.
**Suggested fix:** Use a file lock (on Windows: `msvcrt.locking` or the `portalocker` pip package, which works cross-platform) around the read-modify-write. Scope the lock to the entire `main()` path from pending-load to pending-save. For a single-operator tool, even a simple "acquire lockfile or exit 0" at the top of `main()` is acceptable — just make concurrent runs skip rather than corrupt. Also applies to `sheets_token.json` rewrites in `load_creds` (lines 248-252).

### [HIGH] Token file refresh is a lost-update race
**File:** `scripts/sheets_sync.py:243-252`, `scripts/sheets_bootstrap.py:69-78`, `scripts/send_draft.py:35-38`
**Finding:** Multiple scripts each independently refresh and rewrite `sheets_token.json` / `token.json`. With concurrent runs (or just overlapping invocations — e.g., gmail send_draft.py called by the skill while sheets_sync.py is also running), one process reads token at state-A, refreshes, writes state-B; another reads at state-A, refreshes, writes state-B'. Google's refresh endpoint may (depending on mode) rotate the refresh token, in which case one of the writes overwrites a newer refresh token with a stale one — leading to "invalid_grant" on the *next* run.
**Impact:** Sporadic token failures that require manual re-auth. Hard to diagnose because it looks like the token just expired.
**Suggested fix:** Acquire a short-lived file lock on the token file before refresh-and-write. Alternatively, use a single shared auth helper module that all three scripts import, with internal locking. Or, simplest: since Pack'N uses an Internal Workspace OAuth client (refresh tokens don't rotate on Internal apps), add a comment documenting this assumption so it doesn't regress if the client type ever changes.

### [HIGH] Unicode/CR/LF in cell values will break CSV mirror and `USER_ENTERED` sheet interpretation
**File:** `scripts/sheets_sync.py:116-134, 138-163, 223-229`
**Finding:** The local CSV writer uses `csv.DictWriter` with defaults — it will handle embedded newlines in quoted fields correctly. Good. However, `valueInputOption="USER_ENTERED"` on line 226 means the Sheets API will **interpret strings as formulas** if they start with `=`, `+`, `-`, or `@`. A customer message that starts with `"=DELETE_ALL..."` copied into `issue_description` becomes a live formula; a merchant name like `"-Prime Logistics"` becomes a negative number. This is the Sheets equivalent of CSV injection.
**Impact:** Formula injection — a hostile customer can inject a cell that, when an operator clicks it, runs a formula. More realistically, benign data that begins with `-` or `+` (phone numbers, SKUs like `-001`) is silently coerced to a number. Display breakage at minimum; in the malicious case, cross-tab formula injection (e.g., IMPORTRANGE to exfiltrate other operator-only columns).
**Suggested fix:** Either use `valueInputOption="RAW"` (preserves strings verbatim, but loses auto-date and auto-number conversion), or defensively prefix any string that starts with `= + - @` with a leading apostrophe in `_coerce`:
```python
_FORMULA_CHARS = ("=", "+", "-", "@")
def _coerce(v: Any) -> Any:
    if v is None: return ""
    if isinstance(v, bool): return "TRUE" if v else "FALSE"
    if isinstance(v, str) and v.startswith(_FORMULA_CHARS):
        return "'" + v
    return v
```
Apply selectively to free-text columns (`issue_description`, `operator_notes`, `customer_name`) — not to columns you *want* parsed (`requested_credit_usd` as number). A schema-driven coercion is the robust version; a blanket defensive-apostrophe is the fast fix.

### [HIGH] `TAB_SCHEMAS` duplicated — `sheets_sync.py` has no canonical source for skill-owned vs operator-owned split
**File:** `scripts/sheets_bootstrap.py:38-66` and (absent from) `scripts/sheets_sync.py`
**Finding:** `sheets_bootstrap.py` defines `TAB_SCHEMAS` as the source of truth for header names and implicit zones via comments (`# skill-owned` / `# operator-owned`). `sheets_sync.py` does not import or reference it; it discovers columns at runtime via `fetch_header_map`. This works *until*: (a) operator deletes a skill-owned header — sync silently skips that column with a warning but the row is still appended, producing a partial row; (b) bootstrap is re-run after a schema change but sync is old — drift. There's no single-source-of-truth for which columns are skill-owned (must not be deleted) vs operator-owned (empty on first insert, never touched after).
**Impact:** Invariant #10 in CLAUDE.md ("Never rename or delete a skill-owned header — the skill will log an error") is currently soft-enforced. The skill logs a warning but still appends a broken row. Also: when new skill-owned columns are added to bootstrap but don't exist on existing sheets, sync writes rows missing those columns with no signal to the operator.
**Suggested fix:** Extract `TAB_SCHEMAS` into a shared module (`scripts/sheets_schema.py`) with explicit `skill_owned: list[str]` and `operator_owned: list[str]` per tab. Have both bootstrap and sync import it. In sync, after `fetch_header_map`, verify all `skill_owned` columns are present; if any are missing, **fail hard** for that tab (queue the payload, warn loudly) rather than silently writing a partial row. Bootstrap should also have a "schema drift" command that reports new skill-owned columns missing from existing sheets.

### [HIGH] `col_index_to_letter` correct but unused for append; append via `!A1` works for 26 columns — breaks at column AA+ only if a specific path is taken
**File:** `scripts/sheets_sync.py:85-92, 223-229`
**Finding:** Not a bug yet, but a latent one. `append_rows_for_tab` appends to `{tab}!A1` which the Sheets API interprets as "append after the last row of the table starting at A1" — this works regardless of column count because `values.append` autoscans. However, `fetch_existing_ids` uses `col_index_to_letter` correctly to build `{tab}!{letter}2:{letter}`, which relies on the column-letter conversion. I traced `col_index_to_letter`: at `idx=25` it returns `"Z"`, at `idx=26` it returns `"AA"`, at `idx=27` returns `"AB"`. Correct. But note: if an operator adds enough of their own columns to push the dedupe column past Z (26 columns), this is the only code path that correctly handles it. Worth calling out because the schemas currently fit in < 26 skill columns so this is untested.
**Impact:** None today. If operator adds 10+ of their own columns to `mispack_log` and puts them before `ticket_id`, dedupe will still work — the conversion is right. Flagging because I verified it and want the reviewer to know it was checked.
**Suggested fix:** Add a unit-test-style inline self-check in module load (once tests exist):
```python
assert col_index_to_letter(0) == "A"
assert col_index_to_letter(25) == "Z"
assert col_index_to_letter(26) == "AA"
assert col_index_to_letter(701) == "ZZ"
assert col_index_to_letter(702) == "AAA"
```

---

## Medium

### [MEDIUM] `customer_name` construction fragile when either firstname or lastname is null
**File:** `SKILL.md:273` (`"customer_name": "<firstname + ' ' + lastname from ticket_context.contact; empty if missing>"`)
**Finding:** The skill instruction says "firstname + ' ' + lastname; empty if missing". Ambiguous: does "missing" mean *both* missing, or *either*? If firstname is `"Jacob"` and lastname is `None`, naive concat produces `"Jacob None"` or `"Jacob "`. HubSpot contacts very frequently have only one or the other populated.
**Impact:** Rows like `"Jacob "` or `"None Wilson"` land in the sheet.
**Suggested fix:** Clarify in SKILL.md: `customer_name = " ".join(x for x in [firstname, lastname] if x)`. Or push this into Python in a preprocessing step in `sheets_sync.py` (less ideal because the skill is instructed to assemble the dict — keep the logic co-located).

### [MEDIUM] `requested_credit_usd` passed as raw form-field string; sheet can't sum it
**File:** `SKILL.md:280` and `scripts/sheets_sync.py:116-134`
**Finding:** `requested_credit_amount` from the HubSpot form comes in as a string (e.g., `"250.00"`, `"$250"`, `"approx $250"`). The skill copies it through untouched. Sheet cell becomes text, not number — `SUM` won't work. Also: `valueInputOption="USER_ENTERED"` will coerce clean numerics like `"250.00"` to a number, but `"$250"` to text — inconsistent typing per row.
**Impact:** KPI rollups on "total requested credit" are unreliable. Operators end up doing manual cleanup.
**Suggested fix:** In `sheets_sync.py`, coerce known-numeric columns via a schema hint: strip `$`, commas, whitespace; attempt float parse; emit a float or empty string. Do this per-column via a schema entry rather than globally (`_coerce` would otherwise misread SKUs that happen to be digits).

### [MEDIUM] `fetch_existing_ids` reads whole column every append — quadratic cost over time
**File:** `scripts/sheets_sync.py:104-113, 201-203`
**Finding:** Every call to `append_rows_for_tab` with `dedupe_on` fetches the entire `id_col` column from row 2 down. After 10,000 tickets, every append pulls 10,000 cells just to dedupe. Currently fast enough at 25 tickets/run, but grows linearly per-run × linearly per-sheet-size → quadratic ops over the lifetime of the workbook.
**Impact:** Not a correctness issue and explicitly out-of-scope per v1 guidelines. Flagging because it will silently become a 30-second-per-run API quota hog within a year of operation at Pack'N's ticket volume.
**Suggested fix:** Cache the seen ticket_id set in `sheets_state.json` (or a sibling `sheets_dedupe_cache.json`), updated after each successful append. Fall back to sheet read only if the cache is absent or corrupt. Alternatively: use the Sheets API's `values.get` with a range that grows as the sheet grows is fine for now, but consider a "batch size" cap (read only the last N=500 rows' ticket_ids) — adequate because tickets don't get re-submitted months later.

### [MEDIUM] `existing` set in `append_rows_for_tab` treats empty tracking-id cells as empty string, not as "row without id"
**File:** `scripts/sheets_sync.py:104-113`
**Finding:** `{v[0] for v in resp.get("values", []) if v and v[0]}` drops empty cells — good. But the upstream set comprehension is `if v and v[0]` which treats `v[0] = 0` or `v[0] = "0"` as falsy (well, `"0"` is truthy, so only numeric `0` is the risk). `ticket_id` is always a string in practice, but if an operator pasted a raw integer into the id column, it'd be silently dropped. Minor.
**Impact:** One in a thousand edge case.
**Suggested fix:** Change to `if v and str(v[0]).strip()`. Also applies to the dedupe-key lookup on line 208: `str(r.get(dedupe_on, "")).strip()` — which you already do. Parity.

### [MEDIUM] Carrier inference: missing DHL, Amazon Logistics, OnTrac — all routed to `unknown`
**File:** `scripts/sheets_sync.py:64-81`
**Finding:** Regex covers UPS and a USPS letter-prefix family + purely numeric heuristics (FedEx 12/15 digits, USPS 20/22 starting with 9). Missing: DHL (10-digit numeric, but collides with FedEx; also DHL Express uses `JD...` alphanumeric), Amazon Logistics (`TBA` prefix, 12 chars, all digits after), OnTrac (starts with `C`, 15 chars), LaserShip (`1LS...` or bare numeric). Pack'N as a 3PL likely ships via all of these.
**Impact:** Many rows will flag `unknown`, forcing operator to manually relabel. Not a bug — heuristics are "conservative by design" per the module docstring — but the carrier list is thin.
**Suggested fix:** Add patterns:
```python
_DHL_EXPRESS = re.compile(r"^JD[A-Z0-9]{16,20}$", re.IGNORECASE)
_AMZN_LOGISTICS = re.compile(r"^TBA[0-9]{9,12}$", re.IGNORECASE)
_ONTRAC = re.compile(r"^(C|D)[0-9]{14}$", re.IGNORECASE)   # 15 chars
# FedEx prefers 12/15 digits; DHL also has 10-digit numeric
if len(t) == 10 and t.isdigit():
    return "DHL"   # or "unknown" if you prefer strict
```
Also: move the carrier heuristics into a tested module and add a test fixture file with real-world samples. For now, add a comment listing "known unsupported carriers → unknown" so the operator doesn't expect them to resolve.

### [MEDIUM] `write_local_mirror` re-derives `fieldnames` per append — header drift across runs
**File:** `scripts/sheets_sync.py:138-163`
**Finding:** `fieldnames` is computed as "union of keys across rows in this payload". If run N emits a row with 14 keys and run N+1 emits a row with the same 14 keys + one new one (e.g., skill was upgraded to emit a new field), the CSV was created with 14-col header; run N+1 opens in append mode and writes a row with `extrasaction="ignore"` — the new field is dropped from the CSV even though it'd be fine to add. Conversely, if run N+1 has one fewer field than run N's header, that field is simply empty in the new row — ok. But this means the CSV mirror silently drifts from the sheet.
**Impact:** CSV mirror is advertised as the "recovery source of truth". It is subtly not: new fields added post-mirror-creation never land in the CSV unless the file is deleted and recreated. In a real recovery scenario, an operator restoring from CSV would be missing recently-added columns.
**Suggested fix:** Pin the CSV header to the full schema (from `TAB_SCHEMAS` once extracted per the HIGH above) rather than the payload's key set. If the file exists, read its header, detect drift, and either (a) error loudly, (b) rotate the old CSV and create a new one, or (c) never change the header once created. Option (b) is simplest.

### [MEDIUM] `first_seen_utc` set by the skill per row, not Python — timestamp jitter across ticket batches
**File:** `SKILL.md:272, 297`
**Finding:** Step 2h instructs the skill to populate `first_seen_utc` as "current UTC ISO, set once per row here". Claude following that instruction will emit a slightly different timestamp per row (or, worse, a reused timestamp from earlier in the run). This column should semantically be "when the row was inserted" (single value per run) or "when the ticket was first seen in the HubSpot pull" (from `ticket.createdate` or `hs_lastmodifieddate`).
**Impact:** Ambiguous semantics — operator can't trust whether `first_seen_utc` is the ticket creation time, the first-processing time, or the sheet-write time. For claim SLA tracking ("ship a claim within 14 days of first seeing the carrier issue") this matters.
**Suggested fix:** Pick one semantic. Recommended: use `ticket_context.createdate` as `first_seen_utc` (this is when the *customer submitted* the issue, which is what SLA clocks key off). Alternative: have `sheets_sync.py` stamp `first_seen_utc` at write time if empty — moves the responsibility out of the skill.

### [MEDIUM] `sendNotificationEmail=True` emails everyone on share even if they've seen this before
**File:** `scripts/sheets_bootstrap.py:199-219`
**Finding:** `ensure_sharing` is idempotent — it skips already-shared emails, good. But `sendNotificationEmail=True` is correct behavior *only* on first share. If an operator is added to `share_with` after initial bootstrap, the re-run will correctly notify only the new email. Fine. However, if the workbook is ever *recreated* (state file deleted + bootstrap re-run), everyone gets re-notified — probably fine, but worth noting.
**Impact:** Very minor — operational surprise at most.
**Suggested fix:** None required. Document that bootstrap re-creation notifies everyone.

---

## Low / Nits

### [LOW] `sheets_bootstrap.py:251` — `KeyError` if `TAB_SCHEMAS` has a tab that was never created and not in `missing`
**File:** `scripts/sheets_bootstrap.py:246-251`
**Finding:** `state["tab_ids"] = {k: tab_ids[k] for k in TAB_SCHEMAS}` — this depends on `tab_ids` (from `verify_tabs` + `add_missing_tabs`) having every key in `TAB_SCHEMAS`. Current code guarantees this (if missing, it's added), but if `add_missing_tabs` silently fails for one tab (API returns success but no `addSheet` reply — extremely unlikely but possible), the dict comp raises `KeyError`.
**Impact:** Defensive-coding nit.
**Suggested fix:** Add an assertion that all expected tabs are present before the dict comp, or fall back to `tab_ids.get(k)` with a post-check.

### [LOW] Missing `timeout` on all `googleapiclient` calls
**File:** All three Sheets scripts
**Finding:** `googleapiclient` defaults to 60-second socket timeout, but no retry-with-backoff for 429/5xx. A rate-limit blip will immediately fall into the pending queue.
**Impact:** More queue flushes than necessary; fine for a single-operator tool.
**Suggested fix:** Wrap key API calls with `tenacity`-style retry on 429/5xx (respecting `Retry-After` header). Low priority given the pending queue exists as a safety net.

### [LOW] `sheets_auth.py` and `gmail_auth.py` are ~95% duplicated
**File:** `scripts/sheets_auth.py:1-54`, `scripts/gmail_auth.py:1-47`
**Finding:** Two scripts, same body, different scopes and file paths. Tolerable for a small project but drift-prone (a security fix in one won't be in the other).
**Impact:** Maintainability.
**Suggested fix:** Extract a shared `_oauth_bootstrap(creds_path, token_path, scopes)` helper in a `scripts/_google_auth.py` module; have both scripts call it with their respective constants.

### [LOW] `send_draft.py:26` strips then checks `draft_id` truthy — but does not validate format
**File:** `scripts/send_draft.py:22-29`
**Finding:** Accepts any non-empty string as `draft_id`. Gmail draft IDs match `r"^[a-zA-Z0-9_-]+$"`. Garbage input (`"; rm -rf /"`) is passed to the API and rejected server-side — fine, but a client-side sanity check is cheap.
**Impact:** None security-wise (no shell exec here).
**Suggested fix:** Skip unless a pattern of garbage inputs emerges.

### [LOW] `print()` statements in `sheets_bootstrap.py` emit share URLs and emails to stdout — fine for CLI, noisy if ever wrapped
**File:** `scripts/sheets_bootstrap.py:217, 268-277`
**Finding:** No secret leakage (just spreadsheet_id and emails, which are already in settings.yaml), but if `sheets_bootstrap.py` is ever invoked from within the skill or a CI script, the output is a lot.
**Impact:** Cosmetic.
**Suggested fix:** Route to a `logging` module with a verbosity flag, or at minimum gate chatty prints behind `-v`.

### [NIT] No test suite
**File:** Project-wide
**Finding:** Per user instruction — noting once, not re-flagging per file.
**Impact:** Regressions won't be caught; especially risky for `col_index_to_letter`, `_coerce`, `infer_carrier`, `combine_with_pending`, and the dedupe flow in `append_rows_for_tab`.
**Suggested fix:** Add `pytest` + a `tests/` dir. Priority targets in order: (1) `combine_with_pending` — the merge-order bug above would be caught by one test; (2) `infer_carrier`; (3) `col_index_to_letter`; (4) `_coerce` including string-boolean normalization once fixed.

---

## Suggested enhancements (not bugs)

1. **`sheets_sync.py` argparse + backfill mode.** Current invocation is `py sheets_sync.py <payload_path>`. A `--backfill <csv_path>` or `--backfill-from-runs <glob>` flag would be invaluable for a one-time historical load if Pack'N wants to seed the log with pre-automation tickets. Also add `--dry-run` that exercises the full flow except the final `values.append`.

2. **Token rotation / proactive refresh.** OAuth refresh tokens from Internal Workspace clients don't expire, but they *can* be revoked (user removes app from their Google account settings, admin revokes, etc.). Add a cron-friendly `scripts/sheets_auth_check.py` that tries a no-op `values.get` and exits non-zero if auth fails — so monitoring can catch revocation before a skill run blows up.

3. **Schema-version column in `kpi_system`.** Add a `skill_schema_version` column to `kpi_system` that encodes the schema version used for this run. When you later add columns or change semantics, you'll be able to correlate rows to skill versions for analytics.

4. **`sheets_state.json` integrity check.** Read-check it at sync startup: if `spreadsheet_id` is empty or `tab_ids` is missing, exit with a clear "run bootstrap.py first" message. Currently line 326 (`tab_map = cfg["tabs"]`) and line 325 (`spreadsheet_id = state["spreadsheet_id"]`) will `KeyError` on malformed state.

5. **Sheets-side "operator-owned columns" protection.** The current design relies on the skill never writing those columns. A stronger guarantee: add a **data validation** rule via the Sheets API on the operator-owned column range that blocks any write (or at least warns). This is a belt-and-suspenders enforcement of invariant #8 — if the skill is ever buggy, Sheets refuses the write rather than silently clobbering.

---

_One-line summary written per user request below._
