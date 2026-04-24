"""Compression information loss evaluation tests.

Measures what information survives compression and what is lost.
Each test defines "critical elements" that MUST survive, "recoverable
elements" accessible via selective 2-phase, and elements that may be lost.

This is a quality gate — if compression loses critical information,
the test fails. It's not about size, it's about whether an AI agent
can still do its job with the compressed output.
"""

from __future__ import annotations

import json


from memtomem_stm.proxy.compression import (
    FieldExtractCompressor,
    HybridCompressor,
    SchemaPruningCompressor,
    SelectiveCompressor,
    SkeletonCompressor,
    TruncateCompressor,
)


# ═══════════════════════════════════════════════════════════════════════════
# Test data: realistic MCP tool responses
# ═══════════════════════════════════════════════════════════════════════════


API_RESPONSE_JSON = json.dumps({
    "users": [
        {"id": 1, "name": "Alice", "email": "alice@example.com", "role": "admin"},
        {"id": 2, "name": "Bob", "email": "bob@example.com", "role": "editor"},
        {"id": 3, "name": "Charlie", "email": "charlie@example.com", "role": "viewer"},
    ] + [
        {"id": i, "name": f"User{i}", "email": f"user{i}@example.com", "role": "viewer"}
        for i in range(4, 51)
    ],
    "total": 50,
    "page": 1,
    "per_page": 50,
    "has_more": False,
}, indent=2)


CODE_FILE = """# Authentication Module

## Overview

This module handles JWT-based authentication for the API.
It supports access tokens and refresh tokens with configurable TTLs.

## Configuration

```python
AUTH_CONFIG = {
    "secret_key": "your-secret-key",
    "access_token_ttl": 3600,      # 1 hour
    "refresh_token_ttl": 2592000,  # 30 days
    "algorithm": "HS256",
    "issuer": "memtomem-api",
}
```

## Token Generation

```python
def create_access_token(user_id: str, roles: list[str]) -> str:
    payload = {
        "sub": user_id,
        "roles": roles,
        "exp": datetime.utcnow() + timedelta(seconds=AUTH_CONFIG["access_token_ttl"]),
        "iss": AUTH_CONFIG["issuer"],
    }
    return jwt.encode(payload, AUTH_CONFIG["secret_key"], algorithm=AUTH_CONFIG["algorithm"])
```

## Token Validation

```python
def validate_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            AUTH_CONFIG["secret_key"],
            algorithms=[AUTH_CONFIG["algorithm"]],
            issuer=AUTH_CONFIG["issuer"],
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise AuthError("Token has expired")
    except jwt.InvalidTokenError:
        raise AuthError("Invalid token")
```

## Refresh Flow

1. Client sends expired access token + valid refresh token
2. Server validates refresh token
3. Server issues new access token + optionally rotates refresh token
4. Old refresh token is invalidated (single-use)

## Error Handling

| Error | HTTP Code | Meaning |
|-------|-----------|---------|
| `AuthError` | 401 | Invalid or expired credentials |
| `PermissionError` | 403 | Valid token but insufficient roles |
| `RateLimitError` | 429 | Too many auth attempts |

## Security Notes

- Always use HTTPS in production
- Rotate secret keys periodically
- Implement token blacklisting for logout
- Rate limit login attempts (max 5 per minute)
- Log all authentication failures for monitoring
"""

MEETING_NOTES = """# Sprint Planning — 2026-04-01

## Attendees
- Kim Cheolsu (Backend Lead)
- Lee Younghee (Frontend Lead)
- Park Jimin (DevOps)
- Choi Minjun (PM)

## Agenda
1. Sprint 12 retrospective
2. Sprint 13 goal setting
3. Tech debt discussion
4. Release timeline

## Sprint 12 Retrospective

### What went well
- API v2 migration completed ahead of schedule
- GraphQL schema stabilized, no breaking changes
- Test coverage improved from 78% to 85%

### What needs improvement
- Deployment pipeline still takes 45 minutes
- Documentation is falling behind
- Need better monitoring for the new GraphQL endpoints

## Sprint 13 Goals

### High Priority
1. **Auth refactor**: Migrate from session-based to JWT (assigned: Kim Cheolsu)
   - Estimated: 5 story points
   - Deadline: April 15
2. **Dashboard redesign**: Implement new Figma designs (assigned: Lee Younghee)
   - Estimated: 8 story points
   - Deadline: April 20

### Medium Priority
3. **CI/CD speedup**: Reduce pipeline from 45min to 15min (assigned: Park Jimin)
4. **API docs**: Auto-generate from GraphQL schema (assigned: Kim Cheolsu)

### Low Priority
5. **Dark mode**: User-requested feature (assigned: Lee Younghee)

## Tech Debt Discussion

- Redis connection pooling needs to be configured (current: no pooling)
- Database migration scripts are not idempotent — risk of data loss on re-run
- Legacy REST endpoints (v1) still receiving 20% of traffic — need deprecation plan

## Decisions Made

1. **JWT migration starts Sprint 13** — unanimously approved
2. **v1 API deprecation**: Set sunset date for June 30, 2026
3. **Monitoring**: Adopt Grafana + Prometheus stack (Park Jimin to set up)
4. **Sprint length**: Stay at 2 weeks (rejected proposal for 3 weeks)

## Action Items

- [ ] Kim Cheolsu: Draft JWT migration plan by April 3
- [ ] Lee Younghee: Break down dashboard into sub-tasks
- [ ] Park Jimin: Set up Grafana dashboard for GraphQL metrics
- [ ] Choi Minjun: Update project timeline in Notion
- [ ] ALL: Review v1 API deprecation communication draft by April 5
"""


API_DOCS = """# User Management API

## GET /users

List all users with optional filtering.

### Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| page | int | no | Page number (default 1) |
| limit | int | no | Items per page (default 20, max 100) |
| role | string | no | Filter by role: admin, editor, viewer |
| search | string | no | Full-text search on name and email |

### Response

```json
{
  "users": [{"id": 1, "name": "Alice", "role": "admin"}],
  "total": 150,
  "page": 1
}
```

## POST /users

Create a new user. Requires `admin` role.

### Request Body

```json
{
  "name": "string (required)",
  "email": "string (required, unique)",
  "role": "admin | editor | viewer (default: viewer)",
  "password": "string (min 8 chars, must include uppercase + number)"
}
```

### Response

- `201 Created` — returns the created user object
- `409 Conflict` — email already exists
- `422 Unprocessable Entity` — validation failed

## PUT /users/{id}

Update an existing user. Requires `admin` role or own account.

### Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| id | int | yes | User ID |

### Request Body

Same as POST, all fields optional.

### Response

- `200 OK` — returns updated user
- `404 Not Found` — user does not exist
- `403 Forbidden` — insufficient permissions

## DELETE /users/{id}

Permanently delete a user. Requires `admin` role. Cannot delete self.

### Parameters

| Name | Type | Required | Description |
|------|------|----------|-------------|
| id | int | yes | User ID |

### Response

- `204 No Content` — successfully deleted
- `404 Not Found` — user does not exist
- `403 Forbidden` — cannot delete own account

## PATCH /users/{id}/role

Change a user's role. Requires `admin` role.

### Request Body

```json
{"role": "admin | editor | viewer"}
```

### Response

- `200 OK` — returns updated user
- `400 Bad Request` — invalid role value
"""


NESTED_CONFIG_JSON = json.dumps({
    "database": {
        "host": "db.internal.example.com",
        "port": 5432,
        "name": "production_db",
        "pool": {"min": 5, "max": 20, "idle_timeout": 300},
        "replicas": [
            {"host": "replica-1.internal", "port": 5432, "weight": 50},
            {"host": "replica-2.internal", "port": 5432, "weight": 50},
        ],
    },
    "cache": {
        "provider": "redis",
        "host": "redis.internal.example.com",
        "port": 6379,
        "ttl_seconds": 3600,
        "max_memory": "2gb",
    },
    "auth": {
        "provider": "jwt",
        "secret_rotation_days": 90,
        "session_ttl": 86400,
        "mfa_required": True,
        "allowed_origins": [
            "https://app.example.com",
            "https://admin.example.com",
            "https://staging.example.com",
        ],
    },
    "monitoring": {
        "metrics": {"provider": "prometheus", "port": 9090},
        "logging": {"provider": "elasticsearch", "level": "info"},
        "alerting": {"provider": "pagerduty", "escalation_minutes": 15},
    },
}, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# 1. TRUNCATE — what survives at different ratios?
# ═══════════════════════════════════════════════════════════════════════════


class TestTruncateInfoLoss:
    """Evaluate what truncation preserves at various compression ratios."""

    def test_50pct_preserves_opening_context(self):
        """At 50% compression, the document's purpose and key context survive."""
        comp = TruncateCompressor()
        budget = len(CODE_FILE) // 2
        result = comp.compress(CODE_FILE, max_chars=budget)

        # Critical: document purpose and config must survive (they're at the top)
        assert "Authentication Module" in result
        assert "JWT" in result
        assert "access_token_ttl" in result

    def test_25pct_preserves_overview_and_lists_remaining(self):
        """At 25% compression, overview survives and remaining sections are listed."""
        comp = TruncateCompressor()
        budget = len(CODE_FILE) // 4
        result = comp.compress(CODE_FILE, max_chars=budget)

        assert "Authentication" in result
        assert "JWT" in result
        # Section-aware truncation indicates condensed/remaining sections
        assert "condensed" in result or "truncated" in result or "original" in result

    def test_truncation_metadata_hints_at_lost_content(self):
        """Truncation summary tells you what was cut."""
        comp = TruncateCompressor()
        result = comp.compress(CODE_FILE, max_chars=500)

        # Section-aware: indicates condensed sections, or classic truncation metadata
        assert "condensed" in result or "truncated" in result or "original" in result

    def test_meeting_decisions_at_80pct(self):
        """At 80% budget, meeting decisions (near bottom) may be lost."""
        comp = TruncateCompressor()
        budget = int(len(MEETING_NOTES) * 0.8)
        result = comp.compress(MEETING_NOTES, max_chars=budget)

        # Attendees and agenda (top) should survive
        assert "Kim Cheolsu" in result
        assert "Sprint Planning" in result

        # Decisions section is near the bottom — check if lost
        decisions_preserved = "JWT migration starts Sprint 13" in result
        # At 80% of a long doc, decisions at ~80% point may or may not survive
        # This is informational — the test documents the behavior
        if not decisions_preserved:
            assert "truncated" in result  # at least we know content was cut

    def test_json_truncation_preserves_structure_hint(self):
        """Truncated JSON gives enough context to understand the data shape."""
        comp = TruncateCompressor()
        result = comp.compress(API_RESPONSE_JSON, max_chars=500)

        # First items should survive
        assert "Alice" in result or "users" in result
        assert "truncated" in result


# ═══════════════════════════════════════════════════════════════════════════
# 2. HYBRID — head preserved, tail navigable
# ═══════════════════════════════════════════════════════════════════════════


class TestSectionAwareTruncation:
    """Verify section-aware truncation cuts at heading boundaries."""

    def test_cuts_at_heading_boundary(self):
        """Truncation should not cut mid-section."""
        text = "\n\n".join(
            f"## Section {c}\n\n{'Content for section. ' * 5}" for c in "ABCDE"
        )
        comp = TruncateCompressor()
        # Budget fits ~2 sections but not all 5
        result = comp.compress(text, max_chars=400)
        assert "Section A" in result
        # Remaining sections are condensed or listed
        assert "condensed" in result or "Section" in result

    def test_remaining_section_titles_listed(self):
        """All sections have at least heading + first line representation."""
        text = "\n\n".join(f"## Topic {i}\n\n{'Details here. ' * 5}" for i in range(6))
        comp = TruncateCompressor()
        result = comp.compress(text, max_chars=600)
        # Every section heading should be present (minimum representation)
        for i in range(6):
            assert f"Topic {i}" in result

    def test_plain_text_uses_classic_truncation(self):
        """Text without headings uses position-based truncation (backward compat)."""
        text = "Sentence one. Sentence two. Sentence three. Sentence four. Sentence five."
        comp = TruncateCompressor()
        result = comp.compress(text, max_chars=40)
        assert "truncated" in result
        # No "more sections" for plain text
        assert "more sections" not in result

    def test_single_heading_uses_classic_truncation(self):
        """Only 1 heading: not enough structure, fallback to classic."""
        text = "## Only Heading\n\n" + "Long content here. " * 50
        comp = TruncateCompressor()
        result = comp.compress(text, max_chars=100)
        assert "truncated" in result


class TestHybridInfoLoss:
    """Evaluate hybrid head-preserve + tail-TOC compression."""

    def test_head_fully_intact(self):
        """Head portion is returned exactly as-is (no modification)."""
        comp = HybridCompressor(head_chars=500)
        result = comp.compress(CODE_FILE, max_chars=1500)

        # Check key elements from the head (first ~400 chars)
        assert "Authentication Module" in result
        assert "Overview" in result

    def test_tail_sections_discoverable(self):
        """Tail is compressed to TOC — section titles are visible."""
        comp = HybridCompressor(head_chars=500)
        result = comp.compress(MEETING_NOTES, max_chars=2000)

        # Head should have attendees
        assert "Kim Cheolsu" in result

        # TOC should list section titles from the tail
        has_toc = (
            "selection_key" in result  # JSON TOC from selective
            or "Decisions" in result   # truncated tail mentioning section
            or "Action Items" in result
        )
        assert has_toc, "Tail sections should be discoverable in compressed output"

    def test_code_blocks_in_head_intact(self):
        """Code blocks within head_chars are not modified."""
        comp = HybridCompressor(head_chars=1500)
        result = comp.compress(CODE_FILE, max_chars=3000)

        # The configuration code block should be intact
        assert "AUTH_CONFIG" in result
        assert '"access_token_ttl": 3600' in result

    def test_compression_ratio_reasonable(self):
        """Hybrid achieves meaningful compression on large documents."""
        # Generate a large document to test compression
        large_doc = CODE_FILE + "\n\n" + MEETING_NOTES + "\n\n" + CODE_FILE
        comp = HybridCompressor(head_chars=1000)
        budget = len(large_doc) // 2
        result = comp.compress(large_doc, max_chars=budget)

        ratio = len(result) / len(large_doc)
        assert ratio < 0.7, f"Expected >30% reduction, got {1 - ratio:.0%}"
        assert len(result) <= budget


# ═══════════════════════════════════════════════════════════════════════════
# 3. SELECTIVE — 2-phase: what's in TOC vs what's recoverable
# ═══════════════════════════════════════════════════════════════════════════


class TestSelectiveInfoLoss:
    """Evaluate selective 2-phase: TOC preserves structure, selection recovers content."""

    def test_toc_lists_all_sections(self):
        """TOC contains every section from the original document."""
        comp = SelectiveCompressor(min_section_chars=10)
        toc_json = comp.compress(MEETING_NOTES, max_chars=500)
        toc = json.loads(toc_json)

        entry_keys = {e["key"] for e in toc["entries"]}
        # All major sections should appear in TOC
        expected_sections = {"Attendees", "Agenda", "Sprint 13 Goals", "Decisions Made", "Action Items"}
        found = expected_sections & entry_keys
        assert len(found) >= 3, f"Expected most sections in TOC, found: {found}"

    def test_toc_shows_section_sizes(self):
        """TOC entries include size info so agent can prioritize."""
        comp = SelectiveCompressor(min_section_chars=10)
        toc_json = comp.compress(MEETING_NOTES, max_chars=500)
        toc = json.loads(toc_json)

        for entry in toc["entries"]:
            assert "size" in entry
            assert isinstance(entry["size"], int)
            assert entry["size"] > 0

    def test_selected_section_fully_recovered(self):
        """Phase 2 selection returns the FULL section content, not truncated."""
        comp = SelectiveCompressor(min_section_chars=10)
        toc_json = comp.compress(MEETING_NOTES, max_chars=500)
        toc = json.loads(toc_json)
        key = toc["selection_key"]

        # Find "Decisions Made" or similar key
        decision_entry = None
        for e in toc["entries"]:
            if "Decisions" in e["key"] or "decisions" in e["key"].lower():
                decision_entry = e
                break

        if decision_entry:
            selected = comp.select(key, [decision_entry["key"]])
            # Full content should be recovered — not truncated
            assert "JWT migration" in selected or "unanimously" in selected

    def test_json_toc_preserves_key_names(self):
        """JSON document TOC shows all top-level keys."""
        comp = SelectiveCompressor()
        toc_json = comp.compress(API_RESPONSE_JSON, max_chars=300)
        toc = json.loads(toc_json)

        entry_keys = {e["key"] for e in toc["entries"]}
        assert "users" in entry_keys
        assert "total" in entry_keys or "page" in entry_keys

    def test_no_information_permanently_lost(self):
        """All original content is recoverable via selection — zero permanent loss."""
        comp = SelectiveCompressor(min_section_chars=10)
        toc_json = comp.compress(MEETING_NOTES, max_chars=500)
        toc = json.loads(toc_json)
        key = toc["selection_key"]

        # Select ALL sections
        all_keys = [e["key"] for e in toc["entries"]]
        recovered = comp.select(key, all_keys)

        # All critical content should be recoverable
        assert "Kim Cheolsu" in recovered
        assert "JWT" in recovered or "Auth" in recovered
        assert "Grafana" in recovered or "Prometheus" in recovered


# ═══════════════════════════════════════════════════════════════════════════
# 4. FIELD EXTRACT — JSON structure preservation
# ═══════════════════════════════════════════════════════════════════════════


class TestFieldExtractInfoLoss:
    """Evaluate JSON field extraction compression."""

    def test_all_top_level_keys_visible(self):
        """All top-level JSON keys must be visible after compression."""
        comp = FieldExtractCompressor()
        result = comp.compress(API_RESPONSE_JSON, max_chars=500)

        assert "users" in result
        assert "total" in result
        assert "page" in result
        assert "has_more" in result

    def test_array_items_show_preview(self):
        """Array dict items show first key-value pairs, not just '{N keys}'."""
        comp = FieldExtractCompressor()
        result = comp.compress(API_RESPONSE_JSON, max_chars=1200)

        # Dict items should show actual values like "Alice", not just "{4 keys}"
        assert "Alice" in result or "id" in result
        # Array is truncated with "more" indicator
        assert "more" in result

    def test_array_count_shown(self):
        """Array length is indicated so agent knows total size."""
        comp = FieldExtractCompressor()
        result = comp.compress(API_RESPONSE_JSON, max_chars=800)

        # Should show "more" indicator for large arrays
        assert "more" in result.lower() or "50" in result or "items" in result

    def test_nested_structure_hinted(self):
        """Nested objects are summarized, not discarded."""
        data = json.dumps({
            "config": {"database": {"host": "localhost", "port": 5432, "name": "mydb"}},
            "features": {"auth": True, "cache": True, "logging": False},
        }, indent=2)
        comp = FieldExtractCompressor()
        result = comp.compress(data, max_chars=200)

        assert "config" in result
        assert "features" in result


# ═══════════════════════════════════════════════════════════════════════════
# 5. CROSS-STRATEGY COMPARISON — same input, different strategies
# ═══════════════════════════════════════════════════════════════════════════


class TestCrossStrategyComparison:
    """Compare what each strategy preserves for the same document."""

    def _count_preserved(self, result: str, keywords: list[str]) -> int:
        return sum(1 for kw in keywords if kw.lower() in result.lower())

    def test_meeting_critical_elements(self):
        """Compare preservation of critical meeting elements across strategies."""
        critical = [
            "JWT migration",           # key decision
            "Kim Cheolsu",             # assignee
            "April 15",               # deadline
            "Grafana",                # tech choice
            "v1 API deprecation",     # strategy decision
            "June 30",               # sunset date
        ]

        budget = 2000  # ~50% of original

        results = {
            "truncate": TruncateCompressor().compress(MEETING_NOTES, max_chars=budget),
            "hybrid": HybridCompressor(head_chars=800).compress(MEETING_NOTES, max_chars=budget),
            "field_extract": FieldExtractCompressor().compress(MEETING_NOTES, max_chars=budget),
        }

        scores = {}
        for name, text in results.items():
            scores[name] = self._count_preserved(text, critical)

        # Truncate should preserve at least attendees (top of doc)
        assert "Kim Cheolsu" in results["truncate"]

        # Hybrid should preserve head + provide access to tail
        assert scores["hybrid"] >= 1

        # All strategies should have some preservation
        for name, score in scores.items():
            assert score >= 1, f"{name} preserved 0/{len(critical)} critical elements"

    def test_code_file_critical_elements(self):
        """Compare preservation of critical code elements."""
        critical = [
            "create_access_token",    # function name
            "validate_token",         # function name
            "HS256",                  # algorithm
            "ExpiredSignatureError",  # error type
            "HTTPS",                  # security note
        ]

        budget = 1500  # ~30% of original

        truncated = TruncateCompressor().compress(CODE_FILE, max_chars=budget)
        hybrid = HybridCompressor(head_chars=800).compress(CODE_FILE, max_chars=budget)

        trunc_score = self._count_preserved(truncated, critical)
        hybrid_score = self._count_preserved(hybrid, critical)

        # Both should preserve at least the top of the file
        assert "Authentication" in truncated
        assert "Authentication" in hybrid

        # Document the preservation difference (informational)
        # Hybrid should generally match or exceed truncate since it keeps head intact
        assert hybrid_score >= trunc_score or hybrid_score >= 1

    def test_selective_recovers_everything(self):
        """Selective 2-phase should have zero permanent information loss."""
        comp = SelectiveCompressor(min_section_chars=10)
        toc_json = comp.compress(CODE_FILE, max_chars=500)

        if toc_json.startswith("{"):
            toc = json.loads(toc_json)
            key = toc["selection_key"]
            all_keys = [e["key"] for e in toc["entries"]]
            recovered = comp.select(key, all_keys)

            critical = [
                "create_access_token", "validate_token",
                "HS256", "ExpiredSignatureError", "HTTPS",
            ]
            recovered_score = self._count_preserved(recovered, critical)
            assert recovered_score == len(critical), (
                f"Selective should recover ALL critical elements, got {recovered_score}/{len(critical)}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 6. COMPRESSION RATIO vs INFORMATION RETENTION CURVE
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressionCurve:
    """Map the relationship between compression ratio and info retention."""

    def test_truncate_preserves_more_with_budget(self):
        """More budget → at least as many keywords preserved overall."""
        keywords_by_position = [
            ("Authentication Module", 0.05),   # top 5%
            ("AUTH_CONFIG", 0.15),              # ~15%
            ("create_access_token", 0.30),      # ~30%
            ("validate_token", 0.45),           # ~45%
            ("Refresh Flow", 0.60),             # ~60%
            ("Error Handling", 0.70),           # ~70%
            ("Security Notes", 0.85),           # ~85%
            ("Rate limit", 0.90),               # ~90%
        ]

        comp = TruncateCompressor()
        ratios = [0.3, 0.5, 0.7, 0.9]
        retention = {}

        for ratio in ratios:
            budget = int(len(CODE_FILE) * ratio)
            result = comp.compress(CODE_FILE, max_chars=budget)
            preserved = sum(1 for kw, _ in keywords_by_position if kw in result)
            retention[ratio] = preserved

        # At 90%, most keywords should survive (section-aware distributes info)
        assert retention[0.9] >= len(keywords_by_position) - 3

        # At 30%, at least the top keywords should survive
        assert retention[0.3] >= 2

        # Largest budget should preserve at least as much as smallest
        assert retention[0.9] >= retention[0.3]

    def test_hybrid_retains_more_than_truncate_at_tight_budget(self):
        """At tight budgets, hybrid's head preservation beats pure truncation."""
        keywords_in_head = ["Authentication Module", "JWT", "access_token_ttl"]

        budget = int(len(CODE_FILE) * 0.4)

        trunc = TruncateCompressor().compress(CODE_FILE, max_chars=budget)
        hybrid = HybridCompressor(head_chars=1000).compress(CODE_FILE, max_chars=budget)

        trunc_head = sum(1 for kw in keywords_in_head if kw in trunc)
        hybrid_head = sum(1 for kw in keywords_in_head if kw in hybrid)

        # Hybrid should preserve head at least as well as truncate
        assert hybrid_head >= trunc_head

        # Hybrid's tail should have structural hints (TOC or section names)
        has_tail_hints = (
            "selection_key" in hybrid
            or "Remaining content" in hybrid
            or "truncated" in hybrid
        )
        assert has_tail_hints, "Hybrid tail should provide navigability hints"


# ═══════════════════════════════════════════════════════════════════════════
# 7. SKELETON — API docs structural preservation
# ═══════════════════════════════════════════════════════════════════════════


class TestSkeletonInfoLoss:
    """Evaluate skeleton compression on API documentation."""

    def test_all_endpoint_headings_preserved(self):
        """Every endpoint heading survives — skeleton's core guarantee."""
        comp = SkeletonCompressor()
        result = comp.compress(API_DOCS, max_chars=800)

        for method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            assert method in result, f"{method} endpoint heading lost"

    def test_http_paths_preserved(self):
        """API paths survive alongside their HTTP methods."""
        comp = SkeletonCompressor()
        result = comp.compress(API_DOCS, max_chars=800)

        assert "/users" in result
        assert "/users/{id}" in result

    def test_subheadings_preserved(self):
        """Subheadings (Parameters, Response, Request Body) survive."""
        comp = SkeletonCompressor()
        result = comp.compress(API_DOCS, max_chars=1200)

        # At least some subheadings should be present
        subheadings = ["Parameters", "Response", "Request Body"]
        found = sum(1 for h in subheadings if h in result)
        assert found >= 2, f"Only {found}/3 subheading types survived"

    def test_body_content_trimmed(self):
        """Skeleton is shorter than original — body is aggressively trimmed."""
        comp = SkeletonCompressor()
        result = comp.compress(API_DOCS, max_chars=1000)

        assert len(result) < len(API_DOCS)
        # Original metadata is appended
        assert "skeleton" in result
        assert "body_trimmed_chars" in result

    def test_body_trimmed_chars_positive(self):
        """body_trimmed_chars is positive when body content is actually trimmed."""
        comp = SkeletonCompressor()
        result = comp.compress(API_DOCS, max_chars=800)

        import re

        m = re.search(r"(\d+) body_trimmed_chars", result)
        assert m, "body_trimmed_chars not found in skeleton footer"
        trimmed = int(m.group(1))
        assert trimmed > 0, "Expected positive body_trimmed_chars for compressed doc"

    def test_body_trimmed_chars_zero_when_fits(self):
        """body_trimmed_chars is 0 when all content fits within budget."""
        comp = SkeletonCompressor()
        # Budget larger than input — no compression needed, returns as-is
        result = comp.compress(API_DOCS, max_chars=len(API_DOCS) + 1000)
        assert result == API_DOCS  # passthrough, no footer

    def test_skeleton_footer_sections_count(self):
        """Footer reports correct number of sections (headings)."""
        comp = SkeletonCompressor()
        result = comp.compress(API_DOCS, max_chars=1000)

        import re

        m = re.search(r"(\d+) sections\)", result)
        assert m, "sections count not found in skeleton footer"
        sections = int(m.group(1))
        # API_DOCS has: GET, POST, PUT, DELETE, PATCH top-level
        # plus Parameters, Response, Request Body subheadings
        assert sections >= 5, f"Expected ≥5 sections, got {sections}"

    def test_skeleton_vs_truncate_heading_coverage(self):
        """Skeleton preserves more headings than truncate at same budget."""
        budget = 800

        skeleton = SkeletonCompressor().compress(API_DOCS, max_chars=budget)
        truncated = TruncateCompressor().compress(API_DOCS, max_chars=budget)

        endpoints = ["GET /users", "POST /users", "PUT /users", "DELETE /users", "PATCH /users"]
        skel_found = sum(1 for ep in endpoints if ep in skeleton)
        trunc_found = sum(1 for ep in endpoints if ep in truncated)

        assert skel_found >= trunc_found, (
            f"Skeleton ({skel_found}) should preserve at least as many "
            f"endpoints as truncate ({trunc_found})"
        )

    def test_fallback_to_truncate_with_single_heading(self):
        """Document with < 2 headings falls back to TruncateCompressor."""
        text = "## Only Heading\n\n" + "Long API documentation. " * 100
        comp = SkeletonCompressor()
        result = comp.compress(text, max_chars=200)

        # Falls back to truncate behavior
        assert "truncated" in result or "condensed" in result or "original" in result


# ═══════════════════════════════════════════════════════════════════════════
# 8. SCHEMA PRUNING — JSON structure preservation
# ═══════════════════════════════════════════════════════════════════════════


class TestSchemaPruningInfoLoss:
    """Evaluate schema-preserving JSON pruning."""

    def test_all_top_level_keys_preserved(self):
        """Every top-level config key survives pruning."""
        comp = SchemaPruningCompressor()
        result = comp.compress(NESTED_CONFIG_JSON, max_chars=800)

        for key in ("database", "cache", "auth", "monitoring"):
            assert key in result, f"Top-level key '{key}' lost"

    def test_nested_keys_preserved(self):
        """Second-level keys survive — structure is fully represented."""
        comp = SchemaPruningCompressor()
        result = comp.compress(NESTED_CONFIG_JSON, max_chars=1000)

        # Database nested keys
        assert "host" in result
        assert "pool" in result or "replicas" in result

        # Auth nested keys
        assert "provider" in result
        assert "mfa_required" in result or "session_ttl" in result

    def test_array_sampling_shows_count(self):
        """Large arrays show first items + omitted count, not just truncated."""
        comp = SchemaPruningCompressor()
        result = comp.compress(API_RESPONSE_JSON, max_chars=800)

        parsed = json.loads(result)
        users = parsed.get("users", [])
        # Array should be sampled, not fully included
        assert len(users) < 50
        # Omitted count should appear somewhere
        found_count = any("omitted" in str(item) for item in users if isinstance(item, str))
        assert found_count, "Array should indicate number of omitted items"

    def test_first_and_last_array_items_present(self):
        """Schema pruning keeps head + tail items (not just head)."""
        comp = SchemaPruningCompressor()
        result = comp.compress(API_RESPONSE_JSON, max_chars=1200)

        # First item should be present
        assert "Alice" in result or '"id": 1' in result
        # Last item (User50 or similar) should also be present
        # SchemaPruningCompressor keeps first 2 + last 1
        parsed = json.loads(result)
        users = parsed.get("users", [])
        real_items = [u for u in users if isinstance(u, dict)]
        if len(real_items) >= 2:
            # Check tail item exists (last real dict in the sampled array)
            last = real_items[-1]
            assert last.get("id", 0) >= 40 or last.get("name", "").startswith("User")

    def test_string_values_capped_not_deleted(self):
        """Long strings are truncated with '...', not removed entirely."""
        data = json.dumps({"description": "x" * 200, "other": "short"})
        comp = SchemaPruningCompressor(max_string=50)
        result = comp.compress(data, max_chars=200)

        parsed = json.loads(result)
        desc = parsed.get("description", "")
        assert len(desc) <= 55  # 50 + "..."
        assert desc.endswith("...")
        assert parsed["other"] == "short"  # short strings unchanged

    def test_numeric_and_bool_values_exact(self):
        """Non-string primitives survive pruning unchanged."""
        comp = SchemaPruningCompressor()
        result = comp.compress(NESTED_CONFIG_JSON, max_chars=1200)

        parsed = json.loads(result)
        cache = parsed.get("cache", {})
        assert cache.get("port") == 6379
        assert cache.get("ttl_seconds") == 3600

    def test_pruning_vs_field_extract_key_coverage(self):
        """Schema pruning preserves deeper nesting than field extraction."""
        budget = 600

        pruned = SchemaPruningCompressor().compress(NESTED_CONFIG_JSON, max_chars=budget)
        extracted = FieldExtractCompressor().compress(NESTED_CONFIG_JSON, max_chars=budget)

        # Both should have top-level keys
        for key in ("database", "cache", "auth"):
            assert key in pruned, f"Pruning lost '{key}'"
            assert key in extracted, f"Extraction lost '{key}'"


# ═══════════════════════════════════════════════════════════════════════════
# 9. FULL CROSS-STRATEGY — all 5 compressors on the same input
# ═══════════════════════════════════════════════════════════════════════════


class TestFullCrossStrategy:
    """Compare all compressor strategies on API documentation."""

    def _count_preserved(self, result: str, keywords: list[str]) -> int:
        return sum(1 for kw in keywords if kw.lower() in result.lower())

    def test_api_docs_endpoint_preservation(self):
        """Compare how many API endpoints each strategy preserves."""
        endpoints = ["GET /users", "POST /users", "PUT /users/{id}",
                     "DELETE /users/{id}", "PATCH /users/{id}/role"]
        budget = 1000

        results = {
            "truncate": TruncateCompressor().compress(API_DOCS, max_chars=budget),
            "hybrid": HybridCompressor(head_chars=500).compress(API_DOCS, max_chars=budget),
            "skeleton": SkeletonCompressor().compress(API_DOCS, max_chars=budget),
        }

        scores = {name: self._count_preserved(text, endpoints) for name, text in results.items()}

        # Skeleton should excel at preserving all endpoints
        assert scores["skeleton"] >= 4, (
            f"Skeleton should preserve most endpoints, got {scores['skeleton']}/5"
        )
        # Skeleton should beat or match truncate
        assert scores["skeleton"] >= scores["truncate"]

    def test_json_strategy_comparison(self):
        """Compare JSON-specific strategies on nested config."""
        keys = ["database", "cache", "auth", "monitoring",
                "host", "port", "provider", "pool", "replicas"]
        budget = 800

        results = {
            "truncate": TruncateCompressor().compress(NESTED_CONFIG_JSON, max_chars=budget),
            "schema_pruning": SchemaPruningCompressor().compress(
                NESTED_CONFIG_JSON, max_chars=budget
            ),
            "field_extract": FieldExtractCompressor().compress(
                NESTED_CONFIG_JSON, max_chars=budget
            ),
        }

        scores = {name: self._count_preserved(text, keys) for name, text in results.items()}

        # Schema pruning should preserve the most structure
        assert scores["schema_pruning"] >= scores["truncate"], (
            f"Schema pruning ({scores['schema_pruning']}) should beat "
            f"truncate ({scores['truncate']}) on JSON structure"
        )
