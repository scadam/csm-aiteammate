"""
Schema context for the NL-to-SQL engine.

The table documentation is shared; a dialect-specific header is prepended at
runtime depending on the active back end (real Snowflake vs the SQLite
simulation). The result is injected into the model prompt by
:mod:`src.nl_to_sql` and returned by the ``get_schema`` capability.
"""

from . import config

_TABLE_DOCS = """
--- TABLE: accounts ---
Customer/account context (simulated Gainsight CS + Salesforce).
  account_id            TEXT  -- e.g. 'ACC-1001'
  account_name          TEXT
  tier                  TEXT  -- 'Strategic' | 'Growth' | 'LongTail'
  industry              TEXT
  region                TEXT
  csm_manager_id        TEXT  -- owning CSM (manager); 'unassigned' for long-tail
  csm_name              TEXT
  products              TEXT  -- JSON array as text, e.g. '["FlowDesk","CheckMate"]'
  arr_gbp               INTEGER
  health_score          INTEGER  -- 0-100
  influence             TEXT  -- 'High' | 'Medium' | 'Low'
  sentiment             TEXT  -- 'Positive' | 'Neutral' | 'Frustrated' | 'Negative'
  renewal_date          TEXT  -- ISO date 'YYYY-MM-DD'
  strategic             TEXT  -- 'Yes' | 'No'
  primary_contact       TEXT
  primary_contact_title TEXT
  onboarding_stage      TEXT  -- 'Onboarding' | 'Adopting' | 'Established'

--- TABLE: signals ---
Computed adoption/risk/release signals (the Signal Detection surface).
  signal_id       TEXT
  account_id      TEXT  -- FK -> accounts.account_id
  user_id         TEXT
  user_name       TEXT
  signal_type     TEXT  -- 'adoption_gap' | 'risk' | 'release_relevant'
  product         TEXT  -- 'FlowDesk' | 'CheckMate'
  feature         TEXT
  severity        TEXT  -- 'Low' | 'Medium' | 'High' | 'Critical'
  severity_score  INTEGER  -- 1..5 (use this for threshold filtering)
  description     TEXT
  detected_date   TEXT  -- ISO date
  metric_value    INTEGER
  threshold       INTEGER
  status          TEXT  -- 'new' | 'processed'

--- TABLE: signal_action_map ---
Deterministic signal-to-action mapping (the Next Best Action rules table).
  map_id          TEXT
  signal_type     TEXT
  severity        TEXT
  message_type    TEXT
  channel         TEXT  -- 'email' | 'in_product' | 'csm_review' | 'csm_brief'
  content_source  TEXT  -- FK -> content_library.content_source
  review_required TEXT  -- 'Yes' | 'No'
  notes           TEXT

--- TABLE: routing_rules ---
CSM review vs automatic-send routing rules.
  rule_id         TEXT
  condition_type  TEXT
  description     TEXT
  applies_when    TEXT
  review_required TEXT  -- 'Yes' | 'No'
  priority_weight INTEGER

--- TABLE: enhancements ---
Tagged enhancement releases (six-field tag: product, feature_area, release_type,
audience, complexity, self_service).
  enhancement_id      TEXT
  product             TEXT
  feature_area        TEXT
  release_type        TEXT  -- 'GA' | 'Update'
  audience            TEXT
  complexity          TEXT  -- 'Low' | 'Medium' | 'High'
  self_service        TEXT  -- 'Yes' | 'No'
  title               TEXT
  description         TEXT
  release_date        TEXT  -- ISO date
  matches_request_tag TEXT  -- 'Yes' | 'No' (matches a prior customer request)

--- TABLE: content_library ---
Approved content blocks / playbooks (knowledge base 2).
  content_id     TEXT
  content_source TEXT
  product        TEXT
  feature        TEXT
  message_type   TEXT
  title          TEXT
  body           TEXT
  approved       TEXT  -- 'Yes' | 'No'
  last_reviewed  TEXT  -- ISO date

--- TABLE: voc ---
Voice-of-customer feedback (knowledge base 1).
  voc_id            TEXT
  account_id        TEXT
  user_id           TEXT
  source            TEXT  -- 'survey' | 'call_summary' | 'health_note'
  date              TEXT  -- ISO date
  sentiment         TEXT
  text              TEXT
  feature_requested TEXT

--- TABLE: csm_voice ---
CSM voice archive — accepted, unedited messages used as style anchors (knowledge base 4).
  voice_id       TEXT
  csm_manager_id TEXT
  csm_name       TEXT
  channel        TEXT
  message_type   TEXT
  text           TEXT
  accepted_date  TEXT

--- TABLE: px_engagement ---
In-product engagement history (knowledge base 3) — what a user was already shown.
  engagement_id TEXT
  account_id    TEXT
  user_id       TEXT
  content_id    TEXT
  content_title TEXT
  shown_date    TEXT
  action        TEXT  -- 'viewed' | 'dismissed' | 'clicked'

--- TABLE: review_queue ---
CSM review inbox items.
  item_id        TEXT
  account_id     TEXT
  csm_manager_id TEXT
  priority       TEXT  -- 'High' | 'Medium' | 'Low'
  status         TEXT  -- 'pending' | 'accepted' | 'edited' | 'discarded'
  message_type   TEXT
  channel        TEXT
  draft_text     TEXT
  created_date   TEXT
  signal_id      TEXT

--- TABLE: managers ---
CSM (manager) directory.
  manager_id   TEXT
  upn          TEXT
  display_name TEXT
  role         TEXT
  accounts     TEXT  -- JSON array as text
""".strip()

_SNOWFLAKE_HEADER = """
Dialect: Snowflake SQL. The connection's active database and schema are set to
CSM_DB.ADOPTION, so use plain UPPERCASE table names (e.g. ACCOUNTS) without a
database/schema prefix. Identifiers are case-insensitive when unquoted.

Dialect rules:
- Use ILIKE for case-insensitive string matching, e.g. sentiment ILIKE 'Frustrated'.
- 'products' (accounts) and 'accounts' (managers) are JSON arrays stored as text;
  match with ILIKE, e.g. products ILIKE '%FlowDesk%'.
- Dates are ISO 'YYYY-MM-DD' text; compare as strings or with TO_DATE(col).
""".strip()

_SQLITE_HEADER = """
Dialect: SQLite (the simulated Snowflake is an in-memory SQLite database).
Use plain, unqualified table names (no database/schema prefix).

Dialect rules:
- SQLite LIKE is case-insensitive for ASCII; use LIKE '%value%' for contains matching.
- 'products' (accounts) and 'accounts' (managers) are JSON arrays stored as text;
  match with LIKE, e.g. products LIKE '%FlowDesk%'.
- Dates are ISO 'YYYY-MM-DD' text; compare as strings or with date(col).
""".strip()

_COMMON_RULES = """
Common rules:
- Only generate a single SELECT (or WITH ... SELECT) statement. Never write/modify data.
- Filter signals by severity_score (integer) for thresholds, e.g. severity_score >= 3.
- Return only the SQL — no explanation, no markdown fences, no comments.
- Add LIMIT 100 only if a query could return a very large result set and the user did not ask for "all".

Recipes:
-- Signals above a severity threshold, with account context
SELECT s.signal_id, s.signal_type, s.product, s.feature, s.severity, s.severity_score,
       a.account_name, a.tier, a.influence, a.sentiment
FROM signals s JOIN accounts a ON s.account_id = a.account_id
WHERE s.severity_score >= 3
ORDER BY s.severity_score DESC;

-- Next best action for a signal (deterministic lookup)
SELECT m.message_type, m.channel, m.content_source, m.review_required
FROM signal_action_map m
WHERE m.signal_type = 'adoption_gap' AND m.severity = 'Critical';

-- Accounts at risk (frustrated or low health) owned by a CSM
SELECT account_id, account_name, health_score, sentiment, renewal_date
FROM accounts
WHERE (sentiment = 'Frustrated' OR health_score < 60) AND csm_manager_id = 'csm-svasireddy'
ORDER BY health_score ASC;
""".strip()


def get_schema_context() -> str:
    """Return the full, dialect-aware schema context for the active back end."""
    header = _SNOWFLAKE_HEADER if config.USE_SNOWFLAKE else _SQLITE_HEADER
    return f"{header}\n\n{_TABLE_DOCS}\n\n{_COMMON_RULES}"


def get_schema_markdown() -> str:
    """Return the schema context (used by a ``get_schema`` capability)."""
    return get_schema_context()
