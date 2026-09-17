#!/usr/bin/env python3
"""
GitHub Ticket Creator
======================
Interactive CLI to create GitHub issues (Bug / Task / Feature / Change Request)
with:
  - Type-specific description templates (Bug / Task / Feature / CR — each with
    its own fields, e.g. Bug gets Actual/Expected result, Severity, Priority)
  - Manual vs AI-assisted content entry, chosen per ticket before anything is filled in:
      * Manual  -> you're prompted field-by-field
      * AI      -> give a short description (optionally with a screenshot) or
                   the whole story in your own words, and the AI fills every
                   field of the template for you (you can still reject it and
                   fall back to manual)
  - Label / Assignee / Milestone(Sprint) / Project status — all still manual for now
  - Relationships to other issues (Relates to / Blocks / Blocked by / Duplicate of)
  - Dry-run preview, local ticket log, duplicate-title warning

Requirements:
    pip install requests

You manage the GitHub URL/token and (optionally) an AI API key. Put them in a
local .env file next to this script (see .env.example) — or export them as
real environment variables, which always take priority over .env.
"""

import os
import re
import sys
import json
import base64
import difflib
import mimetypes
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package. Install it with: pip install requests")


def _load_dotenv(path=".env"):
    """Minimal .env loader (no extra dependency): sets os.environ from KEY=VALUE
    lines, skipping blanks/comments. Never overrides a real env var that's
    already set, so actual environment variables still win over .env."""
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# ============================================================================
# CONFIG — set these in a local .env file (see .env.example), or as real
# environment variables, which always take priority over .env
# ============================================================================

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")   # a GitHub PAT with 'repo' + 'project' scopes
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")   # the org/user that owns the repo — no default, set per-install
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")     # the repo name — no default, set per-install
GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com")       # change for GHE
GITHUB_GRAPHQL_URL = os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")

# A repo can have more than one Projects V2 board linked to it (e.g. an unrelated
# template board someone created). Optional — only needed when a repo has more
# than one; leave unset and the app picks the only one / warns which it picked.
GITHUB_PROJECT_NAME = os.environ.get("GITHUB_PROJECT_NAME", "")

# AI provider used by "AI-assisted" ticket entry — Anthropic (Claude) and Google
# (Gemini) are both supported; AI_PROVIDER picks which is active by default and
# can be switched at runtime (see set_ai_provider) without touching .env. Both
# providers' keys can be configured at once — only the active one is actually
# called. Leave a key empty to leave that provider unavailable to switch to.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()

AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_API_URL = os.environ.get("AI_API_URL", "https://api.anthropic.com/v1/messages")
AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5-20251001")   # set to whatever model string your API key has access to

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_URL = os.environ.get("GEMINI_API_URL", "https://generativelanguage.googleapis.com/v1beta/models")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

LOCAL_LOG_FILE = os.environ.get("TICKET_LOG_FILE", "ticket_log.json")

# ============================================================================
# TEMPLATES — fields + description format applied per ticket type
# ============================================================================
#
# Each field dict supports:
#   key      -> placeholder name used in "format"
#   label    -> prompt shown to the user / description given to the AI
#   type     -> "text" | "multiline" | "select" | "bullets"
#   options  -> required for "select"; the AI is told to pick exactly one of these
#   default  -> used for "text"/"select" if left blank

TEMPLATES = {
    "bug": {
        "label_prefix": "[BUG]",
        # "bug_title" is filled in from the ticket title (Step 2), not prompted separately.
        "fields": [
            {"key": "description", "label": "Description", "type": "multiline"},
            {"key": "steps_to_reproduce", "label": "Steps to reproduce", "type": "multiline"},
            {"key": "expected_result", "label": "Expected result", "type": "multiline"},
            {"key": "actual_result", "label": "Actual result", "type": "multiline"},
            {"key": "platform", "label": "Platform", "type": "select", "options": ["QA", "Stage"]},
            {"key": "os", "label": "Operating System", "type": "text", "default": "Windows"},
            {"key": "browser", "label": "Browser", "type": "text", "default": "Chrome"},
            {"key": "severity", "label": "Severity", "type": "select", "options": ["Critical", "High", "Medium", "Low"]},
            {"key": "priority", "label": "Priority", "type": "select", "options": ["P0 - Urgent", "P1 - High", "P2 - Medium", "P3 - Low"]},
            {"key": "screenshot", "label": "Screenshot", "type": "text", "default": "Attached"},
            {"key": "url_video_image", "label": "URL / Video / Image", "type": "text"},
        ],
        "format": """**Bug Report**

-----------------------------------------------------------------------------------------------------------------

**Bug Title:**
{bug_title}

**Description:**
{description}

**Steps to reproduce:**
{steps_to_reproduce}

**Expected result:**
{expected_result}

**Actual result:**
{actual_result}

**Platform:** {platform}

**Operating System:** {os}

**Browser:** {browser}

**Severity:** {severity}

**Priority:** {priority}

**Screenshot:** {screenshot}

**URL / Video / Image:** {url_video_image}
""",
    },
    "task": {
        "label_prefix": "[TASK]",
        "fields": [
            {"key": "description", "label": "Description", "type": "multiline"},
            {"key": "acceptance_criteria", "label": "Acceptance Criteria (one item per line)", "type": "bullets"},
        ],
        "format": """**Task**

-----------------------------------------------------------------------------------------------------------------

**Description:**
{description}

**Acceptance Criteria:**
{acceptance_criteria}
""",
    },
    "feature": {
        "label_prefix": "[FEATURE]",
        "fields": [
            {"key": "user_story", "label": "User Story (As a ___, I want ___, so that ___)", "type": "multiline"},
            {"key": "description", "label": "Description", "type": "multiline"},
            {"key": "acceptance_criteria", "label": "Acceptance Criteria (one item per line)", "type": "bullets"},
        ],
        "format": """**Feature**

-----------------------------------------------------------------------------------------------------------------

**User Story:**
{user_story}

**Description:**
{description}

**Acceptance Criteria:**
{acceptance_criteria}
""",
    },
    "cr": {
        "label_prefix": "[CR]",
        "fields": [
            {"key": "description", "label": "Description", "type": "multiline"},
            {"key": "reason_for_change", "label": "Reason for Change", "type": "multiline"},
            {"key": "impact_risk", "label": "Impact / Risk", "type": "multiline"},
            {"key": "rollback_plan", "label": "Rollback Plan", "type": "multiline"},
            {
                "key": "acceptance_criteria",
                "label": "Acceptance Criteria / Validation Steps (one item per line)",
                "type": "bullets",
            },
        ],
        "format": """**Change Request**

-----------------------------------------------------------------------------------------------------------------

**Description:**
{description}

**Reason for Change:**
{reason_for_change}

**Impact / Risk:**
{impact_risk}

**Rollback Plan:**
{rollback_plan}

**Acceptance Criteria / Validation Steps:**
{acceptance_criteria}
""",
    },
}

# GitHub's native Issue Type field (the "Type" dropdown in the sidebar) — set on
# create instead of prefixing the title with [BUG]/[TASK]/etc. Verified against
# this org's configured types; "cr" has no matching type, so it's left unset.
ISSUE_TYPE_MAP = {
    "bug": "Bug",
    "task": "Task",
    "feature": "Feature",
    "cr": None,
}

# Human-facing capitalized labels for each internal (lowercase) TEMPLATES key —
# used anywhere a ticket type is shown to a user; the lowercase key itself is
# what's stored in ticket_log.json and used for history-based suggestions, so
# it stays unchanged everywhere else.
TYPE_DISPLAY_NAMES = {
    "bug": "Bug",
    "task": "Task",
    "feature": "Feature",
    "cr": "CR",
}

RELATIONSHIP_KEYWORDS = {
    "relates_to": "Relates to",
    "blocks": "Blocks",
    "blocked_by": "Blocked by",
    "duplicate_of": "Duplicate of",
}

# ============================================================================
# GITHUB API CLIENT
# ============================================================================


class GitHubClient:
    def __init__(self, token, owner, repo, api_url, graphql_url):
        if not token or not owner or not repo:
            sys.exit(
                "Missing GitHub config. Set GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO "
                "(as env vars or in the CONFIG section of this script)."
            )
        self.owner = owner
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self.graphql_url = graphql_url
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    # ---- REST helpers ----
    def _get(self, path, params=None):
        r = self.session.get(f"{self.api_url}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path, payload):
        r = self.session.post(f"{self.api_url}{path}", json=payload)
        if not r.ok:
            raise RuntimeError(f"GitHub API error {r.status_code}: {r.text}")
        return r.json()

    def _patch(self, path, payload):
        r = self.session.patch(f"{self.api_url}{path}", json=payload)
        if not r.ok:
            raise RuntimeError(f"GitHub API error {r.status_code}: {r.text}")
        return r.json()

    def _graphql(self, query, variables=None):
        r = self.session.post(self.graphql_url, json={"query": query, "variables": variables or {}})
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    # ---- Reference data pulled from your repo dashboard ----
    def get_labels(self):
        return [l["name"] for l in self._get(f"/repos/{self.owner}/{self.repo}/labels", {"per_page": 100})]

    def get_collaborators(self):
        try:
            return [c["login"] for c in self._get(f"/repos/{self.owner}/{self.repo}/collaborators", {"per_page": 100})]
        except requests.HTTPError:
            # collaborators endpoint needs push access; fall back to assignable users
            return [c["login"] for c in self._get(f"/repos/{self.owner}/{self.repo}/assignees", {"per_page": 100})]

    def get_milestones(self):
        return self._get(f"/repos/{self.owner}/{self.repo}/milestones", {"state": "open", "per_page": 100})

    def get_open_issues_titles(self):
        issues = self._get(f"/repos/{self.owner}/{self.repo}/issues", {"state": "open", "per_page": 100})
        return [(i["number"], i["title"]) for i in issues if "pull_request" not in i]

    # ---- Issue creation ----
    def create_issue(self, title, body, labels, assignees, milestone_number, issue_type=None):
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        if assignees:
            payload["assignees"] = assignees
        if milestone_number:
            payload["milestone"] = milestone_number
        if issue_type:
            payload["type"] = issue_type
        return self._post(f"/repos/{self.owner}/{self.repo}/issues", payload)

    # ---- Projects (v2) ----
    def get_projects_v2(self):
        query = """
        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            projectsV2(first: 20) { nodes { id title number } }
          }
        }"""
        data = self._graphql(query, {"owner": self.owner, "repo": self.repo})
        return data["repository"]["projectsV2"]["nodes"]

    def get_project_fields(self, project_id):
        query = """
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              fields(first: 30) {
                nodes {
                  ... on ProjectV2FieldCommon { id name }
                  ... on ProjectV2SingleSelectField {
                    id name options { id name }
                  }
                }
              }
            }
          }
        }"""
        data = self._graphql(query, {"projectId": project_id})
        return data["node"]["fields"]["nodes"]

    def add_issue_to_project(self, project_id, issue_node_id):
        mutation = """
        mutation($projectId: ID!, $contentId: ID!) {
          addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
            item { id }
          }
        }"""
        data = self._graphql(mutation, {"projectId": project_id, "contentId": issue_node_id})
        return data["addProjectV2ItemById"]["item"]["id"]

    def get_project_items(self, project_id, status_field_name="Status", max_items=20000):
        """Fetch every item on a Projects V2 board with its current swimlane
        (the named single-select field, usually "Status") and its underlying
        issue's labels/milestone — the data the bulk-management view filters
        and edits. Paginates internally up to max_items (a safety bound against
        a runaway loop, not a normal limit — a board with more items than that
        needs this raised, and prints a warning below if it's ever hit, since
        silently truncating here means bulk filters/edits miss real tickets)."""
        query = """
        query($projectId: ID!, $cursor: String, $statusField: String!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              items(first: 100, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  status: fieldValueByName(name: $statusField) {
                    ... on ProjectV2ItemFieldSingleSelectValue { name optionId }
                  }
                  content {
                    ... on Issue {
                      number
                      title
                      url
                      state
                      labels(first: 30) { nodes { name } }
                      milestone { title number }
                    }
                  }
                }
              }
            }
          }
        }"""
        items, cursor = [], None
        while len(items) < max_items:
            data = self._graphql(query, {"projectId": project_id, "cursor": cursor, "statusField": status_field_name})
            page = data["node"]["items"]
            for node in page["nodes"]:
                content = node.get("content")
                if not content:  # e.g. a draft item with no linked issue
                    continue
                items.append(
                    {
                        "item_id": node["id"],
                        "issue_number": content["number"],
                        "title": content["title"],
                        "url": content["url"],
                        "state": content["state"],
                        "status": (node.get("status") or {}).get("name"),
                        "status_option_id": (node.get("status") or {}).get("optionId"),
                        "labels": [l["name"] for l in content["labels"]["nodes"]],
                        "milestone_title": (content.get("milestone") or {}).get("title"),
                        "milestone_number": (content.get("milestone") or {}).get("number"),
                    }
                )
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        if len(items) >= max_items:
            print(f"  WARNING: get_project_items hit max_items={max_items} — results are truncated and bulk filters/edits will miss tickets. Raise max_items.")
        return items[:max_items]

    def update_issue_labels(self, issue_number, labels):
        return self._patch(f"/repos/{self.owner}/{self.repo}/issues/{issue_number}", {"labels": labels})

    def update_issue_milestone(self, issue_number, milestone_number):
        # GitHub's REST API clears the milestone when the field is JSON null,
        # so this also doubles as "remove from sprint" when milestone_number is None.
        return self._patch(f"/repos/{self.owner}/{self.repo}/issues/{issue_number}", {"milestone": milestone_number})

    def set_project_status(self, project_id, item_id, field_id, option_id):
        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
            value: { singleSelectOptionId: $optionId }
          }) { projectV2Item { id } }
        }"""
        self._graphql(
            mutation,
            {"projectId": project_id, "itemId": item_id, "fieldId": field_id, "optionId": option_id},
        )

    # ---- Best-effort formal sub-issue relationship (newer GitHub feature; may not be enabled) ----
    def try_link_sub_issue(self, parent_node_id, child_node_id):
        mutation = """
        mutation($parentId: ID!, $childId: ID!) {
          addSubIssue(input: {issueId: $parentId, subIssueId: $childId}) { subIssue { id } }
        }"""
        try:
            self._graphql(mutation, {"parentId": parent_node_id, "childId": child_node_id})
            return True
        except Exception:
            return False


# ============================================================================
# AI-ASSISTED TICKET GENERATION (optional)
# ============================================================================


_LEADING_ENUM_RE = re.compile(r"^\s*(?:\d+[.\)]|[-*•])\s*")


def _stringify_field_value(value):
    """The model is asked for string field values but sometimes returns a JSON
    array instead (e.g. steps_to_reproduce as a list). A naive str(value) would
    dump Python's list-repr ("['a', 'b']") straight into the ticket body, so
    lists are joined into readable numbered lines instead — stripping any
    numbering/bullets the model already put on individual items first, so they
    don't get double-numbered ("1. 1. Open the app...")."""
    if isinstance(value, list):
        items = [_LEADING_ENUM_RE.sub("", str(v).strip()) for v in value if str(v).strip()]
        return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1) if item)
    if isinstance(value, dict):
        return json.dumps(value)
    return "" if value is None else str(value)


def _encode_image(image_path):
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/png"
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return mime_type, data


# Shared between ai_generate_ticket and ai_generate_tickets_batch — the default
# instruction to just "write clean, professional English" is exactly what makes
# AI-drafted tickets read like AI wrote them: padded, hedge-y, repeats the same
# fact across three fields. This asks for the opposite register on purpose.
_HUMAN_TONE_INSTRUCTIONS = (
    "Write like a QA engineer or developer jotting a real ticket, not like an AI assistant "
    "summarizing one. Short and concrete beats formal and padded. Fix typos and clean up broken "
    "grammar, but keep the plainspoken register the notes were written in — don't inflate a "
    "two-line bug report into a paragraph of corporate prose.\n"
    "Avoid AI-sounding filler and hedge phrases: \"it appears that\", \"this issue occurs when\", "
    "\"additionally\", \"it is worth noting\", \"ensure that\", \"leverage\", \"utilize\", "
    "\"functionality\", \"seamless\", \"in order to\". Just say the thing.\n"
    "Don't restate the same fact across multiple fields (e.g. Description and Actual Result "
    "saying the same sentence twice) — each field should add something the others don't.\n"
    "Steps to reproduce are short imperative actions a person would actually type "
    "(\"Open the signup form\", \"Enter an invalid email\", \"Submit\"), not full explanatory "
    "sentences with a subject and a reason clause.\n"
    "Keep every field point-to-point: one short sentence or fragment per idea, then stop. No "
    "justification clauses (\"which allows...\", \"this could lead to...\", \"this ensures...\"), "
    "no explaining why something matters, no softening lead-ins. If a field can be said in five "
    "words, don't use fifteen. This applies to every ticket type — Bug, Task, Feature, and CR "
    "alike — not just bug reports.\n"
    "For example, given notes like \"signup doesnt check email format, any text goes through\":\n"
    "  AI-sounding (don't write this): \"The application does not properly validate the email "
    "format field, which allows users to submit invalid data. This could lead to downstream "
    "data quality issues.\"\n"
    "  What we want instead: \"Signup doesn't check the email format — you can type anything and "
    "it submits fine.\""
)


def _field_specs(template, single_issue=True):
    subject = "the issue" if single_issue else "that one issue"
    specs = [f'- "title": a short, clear ticket title summarizing {subject} (no type prefix like "[BUG]")']
    for f in template["fields"]:
        spec = f'- "{f["key"]}": {f["label"]}'
        if f.get("type") == "select":
            spec += f" (choose exactly one of: {', '.join(f['options'])})"
        specs.append(spec)
    return specs


AI_PROVIDERS = ("anthropic", "gemini")


def ai_provider_configured(provider=None):
    """Whether the given (or currently active) provider has what it needs to
    actually be called."""
    provider = (provider or AI_PROVIDER).strip().lower()
    if provider == "gemini":
        return bool(GEMINI_API_KEY and GEMINI_MODEL)
    return bool(AI_API_KEY and AI_MODEL)


def set_ai_provider(provider):
    """Switch which AI provider is active. Raises ValueError for an unknown
    name or one with no key configured — callers (the UI's provider switch)
    should surface that rather than silently failing on the next AI call."""
    global AI_PROVIDER
    provider = (provider or "").strip().lower()
    if provider not in AI_PROVIDERS:
        raise ValueError(f"Unknown AI provider '{provider}' — must be one of {AI_PROVIDERS}")
    if not ai_provider_configured(provider):
        raise ValueError(f"{provider} has no API key configured")
    AI_PROVIDER = provider


def get_ai_provider():
    """AI_PROVIDER can change at runtime via set_ai_provider — callers outside
    this module (the Flask app) should read it through here rather than a
    `from main import AI_PROVIDER`, which would freeze a stale copy at import
    time instead of tracking switches."""
    return AI_PROVIDER


def _request_anthropic_text(prompt, image_path, max_tokens):
    content = [{"type": "text", "text": prompt}]
    if image_path:
        try:
            mime_type, b64data = _encode_image(image_path)
            content.insert(0, {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": b64data},
            })
        except Exception as e:
            print(f"  (Could not attach screenshot: {e})")

    headers = {
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    if AI_API_KEY.startswith("sk-ant-oat"):
        # OAuth access token (e.g. from `ant auth print-credentials --access-token`)
        # goes on Authorization: Bearer, not x-api-key, and needs the oauth beta header.
        headers["Authorization"] = f"Bearer {AI_API_KEY}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        headers["x-api-key"] = AI_API_KEY

    resp = requests.post(
        AI_API_URL,
        headers=headers,
        json={
            "model": AI_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    parts = [c["text"] for c in resp.json().get("content", []) if c.get("type") == "text"]
    return "\n".join(parts).strip()


def _request_gemini_text(prompt, image_path, max_tokens):
    parts = [{"text": prompt}]
    if image_path:
        try:
            mime_type, b64data = _encode_image(image_path)
            parts.append({"inline_data": {"mime_type": mime_type, "data": b64data}})
        except Exception as e:
            print(f"  (Could not attach screenshot: {e})")

    resp = requests.post(
        f"{GEMINI_API_URL.rstrip('/')}/{GEMINI_MODEL}:generateContent",
        params={"key": GEMINI_API_KEY},
        json={
            "contents": [{"parts": parts}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise RuntimeError(f"Gemini returned no candidates (blockReason={block_reason})")
    text_parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in text_parts).strip()


def _call_ai_json(prompt, image_path=None, max_tokens=1200):
    """Dispatches to whichever provider is currently active (see AI_PROVIDER /
    set_ai_provider) and parses the reply as JSON, tolerating stray ```json
    fences even though prompts ask the model not to use them. Returns the
    parsed JSON (dict or list) — raises on any failure, so callers decide how
    to report/fall back."""
    if AI_PROVIDER == "gemini":
        raw = _request_gemini_text(prompt, image_path, max_tokens)
    else:
        raw = _request_anthropic_text(prompt, image_path, max_tokens)

    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            first_line, rest = raw.split("\n", 1)
            raw = rest if first_line.strip().lower() in ("json", "") else raw
    return json.loads(raw)


def ai_generate_ticket(ticket_type, template, story_text, image_path=None):
    """Calls the AI model to derive a title AND fill every template field from free text
    (+ optional screenshot) — this is the whole point of AI-assisted mode: you give only the
    ticket type and a description, the AI writes the rest (title, expected/actual result,
    severity/priority, steps to reproduce, etc. — whatever the type's template calls for).
    Returns a dict {"title": ..., <field_key>: value, ...}, or None if AI isn't configured /
    the call fails (caller should fall back to manual entry)."""
    if not ai_provider_configured():
        print(f"\n(AI not configured — set the {AI_PROVIDER} API key to use AI-assisted mode.)")
        return None

    prompt = (
        f"You are helping fill out a {ticket_type.upper()} ticket for a software project. "
        f"The user has given only a short description (and maybe a screenshot) — you must write "
        f"the entire ticket from that. Produce a single JSON object with EXACTLY these keys and "
        f"nothing else:\n\n"
        + "\n".join(_field_specs(template))
        + "\n\nDescription:\n" + story_text
        + "\n\n" + _HUMAN_TONE_INSTRUCTIONS
        + "\nReturn ONLY the JSON object — no markdown fences, no commentary."
    )

    try:
        data = _call_ai_json(prompt, image_path=image_path)
        result = {"title": str(data.get("title", "")).strip()}
        result.update({f["key"]: _stringify_field_value(data.get(f["key"], "")) for f in template["fields"]})
        return result
    except Exception as e:
        print(f"  (AI generation failed: {e})")
        return None


def ai_generate_tickets_batch(ticket_type, template, raw_notes):
    """Same idea as ai_generate_ticket, but for one block of raw notes that may
    describe MULTIPLE distinct issues jotted down during a single test session —
    one AI call splits the notes into separate tickets and fills every field for
    each, instead of round-tripping one call per bug. Returns a list of dicts
    (same shape as ai_generate_ticket's return value), or None if AI isn't
    configured / the call fails."""
    if not ai_provider_configured():
        print(f"\n(AI not configured — set the {AI_PROVIDER} API key to use AI-assisted mode.)")
        return None

    prompt = (
        f"You are helping fill out {ticket_type.upper()} tickets for a software project from one block of raw "
        f"notes. The notes may describe MULTIPLE separate, unrelated issues jotted down during one test session — "
        f"one per line, per paragraph, or separated by dashes/blank lines/numbering. Split the notes into distinct "
        f"issues: do not merge unrelated issues into one ticket, and do not split a single issue into multiple "
        f"tickets. If the whole block only describes one issue, return a single-element array.\n\n"
        f"Produce a JSON ARRAY where each element is an object with EXACTLY these keys and nothing else:\n\n"
        + "\n".join(_field_specs(template, single_issue=False))
        + "\n\nRaw notes:\n" + raw_notes
        + "\n\n" + _HUMAN_TONE_INSTRUCTIONS
        + "\nReturn ONLY the JSON array — no markdown fences, no commentary, no extra keys, no trailing text."
    )

    try:
        data = _call_ai_json(prompt, max_tokens=4000)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError(f"expected a JSON array, got {type(data).__name__}")
        results = []
        for item in data:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            fields = {f["key"]: _stringify_field_value(item.get(f["key"], "")) for f in template["fields"]}
            results.append({"title": title, **fields})
        return results or None
    except Exception as e:
        print(f"  (AI batch generation failed: {e})")
        return None


# ============================================================================
# SMALL CLI HELPERS
# ============================================================================


def choose_one(prompt, options, suggested=None):
    """suggested: an option (or None) pre-filled as the Enter-key default, based on
    ticket history. Type 'n' to explicitly pick nothing even when a suggestion exists."""
    print(prompt)
    for i, opt in enumerate(options, 1):
        tag = "  <- suggested" if opt == suggested else ""
        print(f"  {i}. {opt}{tag}")
    hint = f"or press Enter to use '{suggested}', 'n' for none" if suggested else "or press Enter to skip"
    while True:
        choice = input(f"Select a number ({hint}): ").strip()
        if choice == "":
            return suggested
        if choice.lower() == "n":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return options[int(choice) - 1]
        print("Invalid choice, try again.")


def choose_many(prompt, options, suggested=None):
    """suggested: list of options pre-filled as the Enter-key default, based on
    ticket history. Type 'n' to explicitly pick none even when suggestions exist."""
    suggested = suggested or []
    print(prompt)
    for i, opt in enumerate(options, 1):
        tag = "  <- suggested" if opt in suggested else ""
        print(f"  {i}. {opt}{tag}")
    hint = f"or press Enter to use {suggested}, 'n' for none" if suggested else "or press Enter to skip"
    choice = input(f"Select numbers separated by commas ({hint}): ").strip()
    if not choice:
        return suggested
    if choice.lower() == "n":
        return []
    picks = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(options):
            picks.append(options[int(part) - 1])
    return picks


def multiline_input(prompt):
    print(prompt + " (type your text, then an empty line to finish):")
    lines = []
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def to_bullets(text):
    # Strip any numbering/bullet a line already has (AI-generated "bullets" fields
    # sometimes come back as an already-numbered multi-line string, and a manually
    # pasted list might be too) before adding our own "- " — otherwise they stack
    # into "- 1. Item".
    lines = [_LEADING_ENUM_RE.sub("", l.strip()) for l in text.splitlines() if l.strip()]
    if not lines:
        return "- "
    return "\n".join(f"- {l}" for l in lines if l)


def prompt_field(field):
    """Prompt for a single template field based on its declared type."""
    label = field["label"]
    ftype = field.get("type", "text")
    default = field.get("default", "")

    if ftype == "select":
        choice = choose_one(f"\n{label}:", field["options"])
        return choice or default
    if ftype == "multiline":
        return multiline_input(f"\n{label}")
    if ftype == "bullets":
        return to_bullets(multiline_input(f"\n{label}"))

    # plain single-line text
    suffix = f" [{default}]" if default else ""
    value = input(f"\n{label}{suffix}: ").strip()
    return value or default


def collect_fields(template):
    """Prompt for every field a template declares and return {key: value}."""
    return {f["key"]: prompt_field(f) for f in template.get("fields", [])}


def load_ticket_log(repo_key=None):
    """repo_key (e.g. "owner/repo"), when given, restricts history to tickets
    logged for that repo — without it, suggestions built from one project's
    history would leak into an unrelated one. Records logged before this
    scoping existed (or by the CLI, which doesn't pass repo_key) have no
    "repo_key" field and are excluded once a repo_key filter is applied."""
    if not os.path.exists(LOCAL_LOG_FILE):
        return []
    try:
        with open(LOCAL_LOG_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return []
    if repo_key:
        data = [r for r in data if r.get("repo_key") == repo_key]
    return data


def log_ticket_locally(record):
    data = load_ticket_log()
    data.append(record)
    with open(LOCAL_LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ============================================================================
# HISTORY-BASED SUGGESTIONS — reduce repeat prompting by learning from past
# tickets of the same type logged in LOCAL_LOG_FILE
# ============================================================================


def _top_values(records, field, available_options, max_items, min_share=0.3):
    """Count how often each value of `field` appears across `records`, keep only
    values that still exist in `available_options`, and return the most frequent
    ones that show up in at least `min_share` of records (capped at max_items)."""
    if not records:
        return []
    counts = {}
    for r in records:
        values = r.get(field) or []
        if isinstance(values, str):
            values = [values]
        for v in values:
            if v in available_options:
                counts[v] = counts.get(v, 0) + 1
    threshold = max(1, round(len(records) * min_share))
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [v for v, c in ranked if c >= threshold][:max_items]


def suggest_labels(ticket_type, available_labels, repo_key=None):
    records = [r for r in load_ticket_log(repo_key) if r.get("type") == ticket_type]
    return _top_values(records, "labels", available_labels, max_items=3)


# ============================================================================
# ROSTER-BASED ASSIGNEE SUGGESTIONS — fallback for when ticket_log.json has no
# (or too little) history to learn from yet: read who covers which domain
# straight from the "Team Roaster" section of skills/skills.md, and match
# those names to live GitHub logins.
# ============================================================================

TEAM_ROSTER_FILE = os.environ.get("TEAM_ROSTER_FILE", os.path.join("skills", "skills.md"))
ROSTER_DOMAINS_FILE = os.environ.get("ROSTER_DOMAINS_FILE", os.path.join("config", "roster_domains.json"))


def _load_roster_domain_config(path=None):
    """Which words in a role mean which domain (role text -> domain tag), and
    which GitHub label belongs to which domain — both entirely org-specific
    (different repos use different label names and role vocabulary), so they
    live in an editable JSON file instead of being hardcoded in this script.
    Returns ({}, {}) if the file is missing, which just means the roster
    fallback has nothing to map to and stays silent rather than guessing."""
    path = path or ROSTER_DOMAINS_FILE
    if not os.path.exists(path):
        return {}, {}
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config.get("domain_keywords", {}), config.get("label_to_domain", {})


ROSTER_DOMAIN_KEYWORDS, LABEL_TO_ROSTER_DOMAIN = _load_roster_domain_config()


def load_team_roster(path=None):
    """Parse the 'Team Roaster' section of skills.md into
    [{"name", "role", "domains": [...], "is_lead": bool}, ...]. Returns [] if
    the file or section is missing — callers treat that as 'no roster
    available' and skip straight to their next fallback."""
    path = path or TEAM_ROSTER_FILE
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    entries = []
    in_roster = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_roster:
            if stripped.lower().startswith("team roaster") or stripped.lower().startswith("team roster"):
                in_roster = True
            continue
        if not stripped:
            continue
        if stripped.lower().startswith("we manage"):
            break
        m = re.match(r"^([A-Za-z][A-Za-z .]*?)\s*[:\-]\s*(.+)$", stripped)
        if not m:
            continue
        name, role = m.group(1).strip(), m.group(2).strip()
        role_lower = role.lower()
        domains = [d for d, kws in ROSTER_DOMAIN_KEYWORDS.items() if any(kw in role_lower for kw in kws)]
        entries.append({"name": name, "role": role, "domains": domains, "is_lead": "lead" in role_lower})
    return entries


def _match_roster_name_to_login(name, logins):
    """Fuzzy-match a roster first name (e.g. 'Samayak') to a live GitHub login
    (e.g. 'samyak-47billion') — logins here are usually the first name plus
    digits/org suffix, but spelling sometimes drifts slightly, so exact prefix
    matching alone isn't enough."""
    name_l = name.lower().split()[0]  # first token only, e.g. "KP" stays "kp"
    best, best_score = None, 0.0
    for login in logins:
        prefix = re.match(r"^[A-Za-z]+", login)
        candidate = prefix.group(0).lower() if prefix else login.lower()
        score = difflib.SequenceMatcher(None, name_l, candidate).ratio()
        if candidate.startswith(name_l) or name_l.startswith(candidate):
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = login, score
    return best if best_score >= 0.55 else None


def suggest_assignees_from_roster(labels, available_assignees, max_items=2):
    """Map a ticket's labels to a roster domain (frontend/backend/...) via
    LABEL_TO_ROSTER_DOMAIN, then to whoever in skills.md's Team Roaster covers
    that domain, resolved to a live GitHub login. A dedicated single-domain
    person (e.g. "Backend Developer") outranks someone wearing multiple hats
    (e.g. "Product Manager, Backend Lead & Solution Architect") since the
    latter is unlikely to want routine bug assignments; leads break ties.
    Returns [] if there's no roster, no domain-mapped label, or no confident
    name-to-login match — callers should fall back to something else."""
    roster = load_team_roster()
    if not roster:
        return []
    domains = {LABEL_TO_ROSTER_DOMAIN[l.lower()] for l in labels if l.lower() in LABEL_TO_ROSTER_DOMAIN}
    if not domains:
        return []

    def score(person):
        return (1 if person["is_lead"] else 0) + (2 if len(person["domains"]) == 1 else 0)

    candidates = sorted((p for p in roster if domains & set(p["domains"])), key=score, reverse=True)
    logins = []
    for person in candidates:
        login = _match_roster_name_to_login(person["name"], available_assignees)
        if login and login not in logins:
            logins.append(login)
        if len(logins) >= max_items:
            break
    return logins


def suggest_assignees(ticket_type, available_assignees, labels=None, repo_key=None):
    """Most-specific signal wins:
    1. History from past tickets of this type that share at least one of the
       given labels (e.g. past *Backend* bugs) — the most directly relevant.
    2. The roster's domain ownership from skills.md for those labels (e.g.
       Backend label -> whoever in Team Roaster covers backend) — used when
       there's no label-specific history yet, so a brand-new label combo
       still gets a sensible suggestion instead of none.
    3. Overall history for this ticket type, ignoring labels — the weakest
       signal, used only when neither of the above found anything (including
       when no labels were passed in at all)."""
    records = [r for r in load_ticket_log(repo_key) if r.get("type") == ticket_type]

    if labels:
        label_set = set(labels)
        label_matched = [r for r in records if label_set & set(r.get("labels") or [])]
        label_history = _top_values(label_matched, "assignees", available_assignees, max_items=2, min_share=0.5)
        if label_history:
            return label_history

        roster_suggestions = suggest_assignees_from_roster(labels, available_assignees)
        if roster_suggestions:
            return roster_suggestions

    return _top_values(records, "assignees", available_assignees, max_items=2, min_share=0.5)


def suggest_milestone(milestones, repo_key=None):
    """Prefer the open milestone with the nearest upcoming due date (the current
    sprint); fall back to whichever milestone was used most recently in history."""
    dated = [m for m in milestones if m.get("due_on")]
    if dated:
        return min(dated, key=lambda m: m["due_on"])["title"]
    log = load_ticket_log(repo_key)
    for record in reversed(log):
        ms_number = record.get("milestone")
        if ms_number:
            match = next((m["title"] for m in milestones if m["number"] == ms_number), None)
            if match:
                return match
    return None


# ============================================================================
# BATCH MODE — file several bugs from one QA test pass in a single session.
# AI-assisted only (typing every field manually for N tickets defeats the point);
# all drafts share one ticket type + one milestone/sprint, but labels/assignees
# are still suggested-and-confirmed per ticket since they vary by module.
# ============================================================================


def _review_and_label_draft(
    bug_label, ai_result, type_key, template, available_labels, available_assignees, allow_retry, auto_apply=False
):
    """Show one AI draft, let the user keep/skip (and optionally retry) it, and
    if kept, resolve labels/assignees for it — either auto-applied from
    suggestions (auto_apply=True) or picked manually. Returns a draft dict
    ready to add to the batch, "retry" if the caller should re-generate this
    one, or None if it was skipped."""
    ai_title = ai_result.get("title", "").strip()
    ai_fields = {k: v for k, v in ai_result.items() if k != "title"}
    if type_key == "bug":
        ai_fields["bug_title"] = ai_title
    preview_body = template["format"].format(**ai_fields)
    print(f"\n--- Draft for {bug_label} ---\nTitle: {ai_title}\n\n{preview_body}--------------------------")

    options = ["Yes", "Skip this one"]
    if allow_retry:
        options.insert(1, "Edit description and retry")
    keep = choose_one("Keep this draft?", options, suggested="Yes")
    if keep == "Edit description and retry":
        return "retry"
    if keep != "Yes":
        return None

    suggested_labels = suggest_labels(type_key, available_labels)
    if auto_apply:
        labels = suggested_labels
        print(f"  Labels (auto-applied): {labels or '(none suggested)'}")
    else:
        labels = choose_many(f"Labels for {bug_label}:", available_labels, suggested=suggested_labels)

    suggested_assignees = suggest_assignees(type_key, available_assignees, labels=labels)
    if auto_apply:
        assignees = suggested_assignees
        print(f"  Assignees (auto-applied): {assignees or '(none suggested)'}")
    else:
        assignees = choose_many(f"Assignees for {bug_label}:", available_assignees, suggested=suggested_assignees)

    return {"title": ai_title, "body": preview_body, "labels": labels, "assignees": assignees}


def run_batch_mode(gh):
    if not ai_provider_configured():
        print(f"\nBatch mode needs AI-assisted generation. Set the {AI_PROVIDER} API key, then re-run.")
        return

    chosen_display = choose_one(
        "\nTicket type for this batch:", list(TYPE_DISPLAY_NAMES.values()), suggested=TYPE_DISPLAY_NAMES["bug"]
    )
    type_key = {v: k for k, v in TYPE_DISPLAY_NAMES.items()}.get(chosen_display, "bug")
    template = TEMPLATES[type_key]

    auto_apply = (
        choose_one(
            "\nAuto-apply suggested labels/assignees/milestone for this whole batch, without asking per ticket?",
            ["Yes - hands-off", "No - let me pick each one"],
            suggested="Yes - hands-off",
        )
        == "Yes - hands-off"
    )

    milestone_number = None
    milestone_title = None
    try:
        milestones = gh.get_milestones()
        suggested_milestone = suggest_milestone(milestones)
        if auto_apply:
            milestone_title = suggested_milestone
            if milestone_title:
                print(f"Milestone (auto-applied): {milestone_title}")
        else:
            milestone_title = choose_one(
                "\nMilestone (sprint) for this whole batch:", [m["title"] for m in milestones], suggested=suggested_milestone
            )
        if milestone_title:
            milestone_number = next(m["number"] for m in milestones if m["title"] == milestone_title)
    except Exception as e:
        print(f"Could not fetch milestones: {e}")

    try:
        available_labels = gh.get_labels()
    except Exception as e:
        print(f"Could not fetch labels: {e}")
        available_labels = []
    try:
        available_assignees = gh.get_collaborators()
    except Exception as e:
        print(f"Could not fetch assignees: {e}")
        available_assignees = []

    drafts = []
    entry_mode = choose_one(
        "\nHow do you want to enter this batch?",
        ["Paste all notes at once (AI splits them)", "One bug at a time"],
        suggested="Paste all notes at once (AI splits them)",
    )

    if entry_mode == "Paste all notes at once (AI splits them)":
        raw_notes = multiline_input("\nPaste all your notes for this batch (one issue per line/paragraph is fine)")
        if not raw_notes.strip():
            print("\nNo notes given — batch cancelled.")
            return
        print("\nSplitting notes into tickets and drafting each one...")
        ai_results = ai_generate_tickets_batch(type_key, template, raw_notes)
        if not ai_results:
            print("AI batch generation failed or found nothing — falling back to one-by-one entry.")
            entry_mode = "One bug at a time"
        else:
            print(f"AI identified {len(ai_results)} issue(s) — review each below.")
            for i, ai_result in enumerate(ai_results, 1):
                draft = _review_and_label_draft(
                    f"issue #{i}",
                    ai_result,
                    type_key,
                    template,
                    available_labels,
                    available_assignees,
                    allow_retry=False,
                    auto_apply=auto_apply,
                )
                if draft:
                    drafts.append(draft)
                    print(f"  Added to batch ({len(drafts)} so far).")

    if entry_mode == "One bug at a time":
        bug_num = 0
        print("\nEnter one bug per round. Leave the description blank to finish the batch.")
        while True:
            bug_num += 1
            story = ""
            while True:
                story = multiline_input(f"\nBug #{bug_num} — describe it (blank to finish batch)")
                if not story.strip():
                    break

                image_path = input("Screenshot path for this bug (or Enter to skip): ").strip()
                ai_result = ai_generate_ticket(type_key, template, story, image_path or None)
                if not ai_result:
                    print("  AI generation failed for this bug — try describing it again, or leave it blank to skip.")
                    continue

                outcome = _review_and_label_draft(
                    f"bug #{bug_num}",
                    ai_result,
                    type_key,
                    template,
                    available_labels,
                    available_assignees,
                    allow_retry=True,
                    auto_apply=auto_apply,
                )
                if outcome == "retry":
                    continue
                if outcome:
                    drafts.append(outcome)
                    print(f"  Added to batch ({len(drafts)} so far).")
                break  # kept or skipped, either way move on to the next bug

            if not story.strip():
                break

    if not drafts:
        print("\nNo tickets to create — batch cancelled.")
        return

    print("\n" + "=" * 60)
    print(f"BATCH PREVIEW — {len(drafts)} ticket(s), type={type_key}, milestone={milestone_title}")
    print("=" * 60)
    for i, d in enumerate(drafts, 1):
        print(f"{i}. {d['title']}\n   labels={d['labels']} assignees={d['assignees']}")

    confirm = input(f"\nCreate all {len(drafts)} tickets on GitHub? (y/N): ").strip().lower()
    if confirm != "y":
        print("Cancelled — nothing was created.")
        return

    issue_type = ISSUE_TYPE_MAP.get(type_key)
    created = 0
    for d in drafts:
        try:
            issue = gh.create_issue(d["title"], d["body"], d["labels"], d["assignees"], milestone_number, issue_type)
            print(f"✅ Created: {issue['html_url']}")
            log_ticket_locally(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "issue_number": issue["number"],
                    "url": issue["html_url"],
                    "type": type_key,
                    "title": d["title"],
                    "mode": "AI-assisted (batch)",
                    "labels": d["labels"],
                    "assignees": d["assignees"],
                    "milestone": milestone_number,
                    "project": None,
                    "status": None,
                    "relationships": [],
                }
            )
            created += 1
        except Exception as e:
            print(f"⚠ Failed to create ticket '{d['title']}': {e}")

    print(f"\n📝 Batch complete: {created}/{len(drafts)} tickets created.")


# ============================================================================
# MAIN FLOW
# ============================================================================


def main():
    print("=" * 60)
    print(" GitHub Ticket Creator")
    print("=" * 60)

    gh = GitHubClient(GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO, GITHUB_API_URL, GITHUB_GRAPHQL_URL)

    # ---- Step 0: single ticket vs batch (multiple bugs from one QA pass) ----
    session_mode = choose_one(
        "\nHow many tickets are you filing right now?", ["Single ticket", "Batch (multiple bugs from a test pass)"]
    )
    if session_mode == "Batch (multiple bugs from a test pass)":
        run_batch_mode(gh)
        return

    # ---- Step 1: ticket type ----
    chosen_display = choose_one("\nWhat type of ticket is this?", list(TYPE_DISPLAY_NAMES.values()))
    type_key = {v: k for k, v in TYPE_DISPLAY_NAMES.items()}.get(chosen_display)
    if not type_key:
        sys.exit("A ticket type is required.")
    template = TEMPLATES[type_key]

    # ---- Step 2: Manual vs AI-assisted content entry ----
    mode = choose_one("\nHow do you want to fill this ticket?", ["Manual entry", "AI-assisted"])

    raw_title = None
    field_values = None

    # ---- Step 3: AI-assisted — you give only a description (+ optional screenshot);
    # the AI writes the title AND every field of the type's template ----
    if mode == "AI-assisted":
        story = multiline_input("\nDescribe the issue (a quick note, or the whole story — your call)")
        image_path = input("\nPath to a screenshot to attach (or press Enter to skip): ").strip()
        ai_result = ai_generate_ticket(type_key, template, story, image_path or None)
        if ai_result:
            ai_title = ai_result.get("title", "").strip()
            ai_fields = {k: v for k, v in ai_result.items() if k != "title"}
            if type_key == "bug":
                ai_fields["bug_title"] = ai_title
            preview_body = template["format"].format(**ai_fields)
            print(f"\n--- AI draft ---\nTitle: {ai_title}\n\n{preview_body}----------------")
            keep = input("Use this AI-generated content? (y/N, N switches to manual entry): ").strip().lower()
            if keep == "y":
                raw_title = ai_title
                field_values = ai_fields
            else:
                mode = "Manual entry"
        else:
            mode = "Manual entry"

    # ---- Step 3 (fallback): Manual entry — title asked directly, then every field ----
    if mode == "Manual entry" and field_values is None:
        raw_title = input("\nTicket title: ").strip()
        field_values = collect_fields(template)
        if type_key == "bug":
            field_values["bug_title"] = raw_title

    # No more [BUG]/[TASK]/etc. prefix in the title — the ticket type is conveyed
    # via GitHub's native Issue Type field instead (set at creation, below).
    title = raw_title
    issue_type = ISSUE_TYPE_MAP.get(type_key)
    body = template["format"].format(**field_values)

    # Warn on likely duplicate titles (whole-word match only, so "test" doesn't
    # false-positive against "La[test]" — it must appear as its own word/phrase)
    try:
        existing = gh.get_open_issues_titles()
        title_pattern = re.compile(r"\b" + re.escape(raw_title) + r"\b", re.IGNORECASE)
        matches = [f"#{n} {t}" for n, t in existing if raw_title and title_pattern.search(t)]
        if matches:
            print("\n⚠ Possible duplicate(s) found among open issues:")
            for m in matches[:5]:
                print(f"   {m}")
    except Exception:
        pass

    # ---- Step 3.5: hands-off mode — auto-apply suggested labels/assignees/milestone? ----
    auto_apply = (
        choose_one(
            "\nAuto-apply suggested labels/assignees/milestone without asking?",
            ["Yes - hands-off", "No - let me pick"],
            suggested="Yes - hands-off",
        )
        == "Yes - hands-off"
    )

    # ---- Step 4: labels (suggested from history of this ticket type) ----
    try:
        available_labels = gh.get_labels()
        suggested_labels = suggest_labels(type_key, available_labels)
        if auto_apply:
            labels = suggested_labels
            print(f"\nLabels (auto-applied): {labels or '(none suggested)'}")
        else:
            labels = choose_many("\nAvailable labels:", available_labels, suggested=suggested_labels)
    except Exception as e:
        print(f"Could not fetch labels: {e}")
        labels = []

    # ---- Step 5: assignee(s) (suggested from history, falling back to the roster) ----
    try:
        available_assignees = gh.get_collaborators()
        suggested_assignees = suggest_assignees(type_key, available_assignees, labels=labels)
        if auto_apply:
            assignees = suggested_assignees
            print(f"Assignees (auto-applied): {assignees or '(none suggested)'}")
        else:
            assignees = choose_many("\nAvailable assignees:", available_assignees, suggested=suggested_assignees)
    except Exception as e:
        print(f"Could not fetch assignees: {e}")
        assignees = []

    # ---- Step 6: milestone / sprint (suggested: nearest-due open milestone, else last used) ----
    milestone_number = None
    try:
        milestones = gh.get_milestones()
        suggested_milestone = suggest_milestone(milestones)
        if auto_apply:
            chosen_ms = suggested_milestone
            if chosen_ms:
                print(f"Milestone (auto-applied): {chosen_ms}")
        else:
            chosen_ms = choose_one("\nAvailable milestones (sprints):", [m["title"] for m in milestones], suggested=suggested_milestone)
        if chosen_ms:
            milestone_number = next(m["number"] for m in milestones if m["title"] == chosen_ms)
    except Exception as e:
        print(f"Could not fetch milestones: {e}")

    # ---- Step 7: relationships ----
    body += "\n\n---\n**Relationships**\n"
    add_rel = input("\nAdd a relationship to another issue? (y/N): ").strip().lower()
    relationships = []
    while add_rel == "y":
        rel_kind = choose_one("Relationship type:", list(RELATIONSHIP_KEYWORDS.keys()))
        issue_num = input("Related issue number (e.g. 42): ").strip()
        if rel_kind and issue_num.isdigit():
            relationships.append((rel_kind, int(issue_num)))
            body += f"- {RELATIONSHIP_KEYWORDS[rel_kind]} #{issue_num}\n"
        add_rel = input("Add another relationship? (y/N): ").strip().lower()

    # ---- Step 8: project + status (swimlane) — manual ----
    project_choice = None
    status_option = None
    status_field_id = None
    try:
        projects = gh.get_projects_v2()
        if projects:
            titles = [p["title"] for p in projects]
            chosen_proj_title = choose_one("\nAllocate to project:", titles)
            if chosen_proj_title:
                project_choice = next(p for p in projects if p["title"] == chosen_proj_title)
                fields = gh.get_project_fields(project_choice["id"])
                status_field = next((f for f in fields if f.get("name", "").lower() == "status"), None)
                if status_field and status_field.get("options"):
                    option_names = [o["name"] for o in status_field["options"]]
                    chosen_status = choose_one("Select status (swimlane):", option_names)
                    if chosen_status:
                        status_option = next(o for o in status_field["options"] if o["name"] == chosen_status)
                        status_field_id = status_field["id"]
    except Exception as e:
        print(f"Could not fetch projects: {e}")

    # ---- Dry run preview ----
    print("\n" + "=" * 60)
    print("PREVIEW")
    print("=" * 60)
    print(f"Title      : {title}")
    print(f"Type       : {type_key}")
    print(f"Mode       : {mode}")
    print(f"Labels     : {labels}")
    print(f"Assignees  : {assignees}")
    print(f"Milestone  : {milestone_number}")
    print(f"Project    : {project_choice['title'] if project_choice else None}")
    print(f"Status     : {status_option['name'] if status_option else None}")
    print(f"Relations  : {relationships}")
    print("-" * 60)
    print(body)
    print("=" * 60)

    confirm = input("\nCreate this ticket on GitHub? (y/N): ").strip().lower()
    if confirm != "y":
        print("Cancelled — nothing was created.")
        return

    # ---- Create the issue ----
    issue = gh.create_issue(title, body, labels, assignees, milestone_number, issue_type)
    print(f"\n✅ Issue created: {issue['html_url']}")
    if type_key == "cr":
        print("   (Note: no 'Change Request' Issue Type exists in this org yet — Type left unset.)")

    # ---- Add to project + set status ----
    if project_choice:
        try:
            item_id = gh.add_issue_to_project(project_choice["id"], issue["node_id"])
            if status_option and status_field_id:
                gh.set_project_status(project_choice["id"], item_id, status_field_id, status_option["id"])
                print(f"✅ Added to project '{project_choice['title']}' with status '{status_option['name']}'")
            else:
                print(f"✅ Added to project '{project_choice['title']}'")
        except Exception as e:
            print(f"⚠ Could not update project fields: {e}")

    # ---- Local log ----
    log_ticket_locally(
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "issue_number": issue["number"],
            "url": issue["html_url"],
            "type": type_key,
            "title": title,
            "mode": mode,
            "labels": labels,
            "assignees": assignees,
            "milestone": milestone_number,
            "project": project_choice["title"] if project_choice else None,
            "status": status_option["name"] if status_option else None,
            "relationships": relationships,
        }
    )
    print(f"📝 Logged locally to {LOCAL_LOG_FILE}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
