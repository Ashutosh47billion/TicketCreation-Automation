#!/usr/bin/env python3
"""
Local test UI for the GitHub Ticket Creator (main.py).

Wraps the same core logic used by the CLI (GitHubClient, TEMPLATES,
ai_generate_ticket, history-based suggestions, batch logging) behind a small
Flask app + single-page UI, so the whole flow (manual / AI-assisted / batch)
can be exercised by clicking instead of typing into terminal prompts.

Every create action defaults to "dry run" (nothing is sent to GitHub) so you
can test freely; uncheck it to actually create a real issue.

Run:
    .venv/Scripts/python.exe app.py
Then open http://127.0.0.1:5000
"""

import os
import time
import tempfile

from flask import Flask, jsonify, render_template, request

from main import (
    GITHUB_API_URL,
    GITHUB_GRAPHQL_URL,
    GITHUB_OWNER,
    GITHUB_PROJECT_NAME,
    GITHUB_REPO,
    GITHUB_TOKEN,
    ISSUE_TYPE_MAP,
    RELATIONSHIP_KEYWORDS,
    TEMPLATES,
    TYPE_DISPLAY_NAMES,
    GitHubClient,
    ai_generate_ticket,
    ai_generate_tickets_batch,
    ai_provider_configured,
    get_ai_provider,
    load_ticket_log,
    log_ticket_locally,
    set_ai_provider,
    suggest_assignees,
    suggest_labels,
    suggest_milestone,
    to_bullets,
)
from datetime import datetime, timezone

app = Flask(__name__)

# ============================================================================
# CONNECTION — which repo we're pointed at, and the token to talk to it. This
# is now runtime-configurable from the UI's Connect panel (POST /api/connect)
# instead of being fixed at process start from .env: switching projects no
# longer needs an .env edit + restart. The token lives only in this in-memory
# state for the life of the process — never written to disk, never echoed
# back to the client. .env is still read once at startup as a convenience
# fallback (so an existing setup keeps working unchanged), but the Connect
# panel is the primary path now. AI provider keys (Anthropic/Gemini) stay .env-only.
# ============================================================================

_gh = None
_gh_error = "Not connected — use the Connect panel to enter a repo and token"
_current_owner = ""
_current_repo = ""
_current_project_name = ""
_current_token = ""  # server memory only — never returned in any API response
_board_items_cache = {"items": None, "fetched_at": 0}
_board_project_warning = None


def _repo_key():
    return f"{_current_owner}/{_current_repo}" if _current_owner and _current_repo else None


def connect(owner, repo, token, project_name="", api_url=None, graphql_url=None):
    """(Re)connect to a GitHub repo. Validates with one real API call before
    committing to the new state, so a bad token/owner/repo doesn't silently
    leave the app half-configured on the old (or no) connection. An empty
    token reuses the currently-connected one — the Connect panel locks the
    token field after a successful connect (it can't show the real value back,
    since that's never sent to the client), so editing just owner/repo/project
    without retyping a token that hasn't changed needs this fallback."""
    global _gh, _gh_error, _current_owner, _current_repo, _current_project_name, _current_token, _board_items_cache, _board_project_warning
    owner = (owner or "").strip()
    repo = (repo or "").strip()
    token = (token or "").strip() or _current_token
    project_name = (project_name or "").strip()
    if not owner or not repo or not token:
        return False, "Owner, repo, and token are all required"
    try:
        client = GitHubClient(token, owner, repo, api_url or GITHUB_API_URL, graphql_url or GITHUB_GRAPHQL_URL)
        client.get_labels()  # cheapest real call that proves token+owner+repo actually work together
    except Exception as e:
        return False, f"Could not connect: {e}"
    _gh = client
    _gh_error = None
    _current_owner = owner
    _current_repo = repo
    _current_project_name = project_name
    _current_token = token
    _board_items_cache = {"items": None, "fetched_at": 0}  # new repo — old cached board data is invalid
    _board_project_warning = None
    return True, None


if GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO:
    _ok, _err = connect(GITHUB_OWNER, GITHUB_REPO, GITHUB_TOKEN, GITHUB_PROJECT_NAME, GITHUB_API_URL, GITHUB_GRAPHQL_URL)
    if not _ok:
        _gh_error = _err

# Not a constant — the active provider can change at runtime via /api/ai-provider,
# so this is always re-checked live rather than cached at import time.
def ai_configured():
    return ai_provider_configured()


def render_body(type_key, raw_title, field_values):
    """Fill in defaults + bullets-formatting the same way the CLI does before
    plugging values into the template's format string."""
    template = TEMPLATES[type_key]
    processed = {}
    for f in template.get("fields", []):
        value = (field_values or {}).get(f["key"], "")
        if not value and f.get("default"):
            value = f["default"]
        if f.get("type") == "bullets":
            value = to_bullets(value)
        processed[f["key"]] = value
    if type_key == "bug":
        processed["bug_title"] = raw_title or ""
    return template["format"].format(**processed), processed


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config")
def api_config():
    types = {}
    for key, tpl in TEMPLATES.items():
        types[key] = {
            "label_prefix": tpl["label_prefix"],
            "fields": tpl["fields"],
            "issue_type": ISSUE_TYPE_MAP.get(key),
            "display_name": TYPE_DISPLAY_NAMES.get(key, key),
        }
    return jsonify(
        {
            "types": types,
            "relationship_keywords": RELATIONSHIP_KEYWORDS,
            "ai_configured": ai_configured(),
            "ai_provider": get_ai_provider(),
            "ai_providers_available": {
                "anthropic": ai_provider_configured("anthropic"),
                "gemini": ai_provider_configured("gemini"),
            },
            "github_configured": _gh is not None,
            "github_error": _gh_error,
            # current_owner/current_repo are safe to echo back (not secret) so the
            # Connect panel can show what's active without re-prompting on reload.
            # The token itself is never returned — server memory only.
            "current_owner": _current_owner,
            "current_repo": _current_repo,
            "current_project_name": _current_project_name,
        }
    )


@app.route("/api/ai-provider", methods=["POST"])
def api_ai_provider():
    """Switch the active AI provider at runtime (Anthropic/Gemini) — rejects a
    provider with no key configured rather than switching to something that'll
    just fail on the next AI call."""
    data = request.get_json(force=True)
    try:
        set_ai_provider(data.get("provider"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "provider": get_ai_provider()})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    """Point the app at a different GitHub repo without an .env edit or
    restart. Validates the token/owner/repo combination with a real API call
    before switching — a bad attempt leaves the previous connection intact."""
    data = request.get_json(force=True)
    ok, err = connect(
        data.get("owner"),
        data.get("repo"),
        data.get("token"),
        data.get("project_name", ""),
    )
    if not ok:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "owner": _current_owner, "repo": _current_repo, "project_name": _current_project_name})


@app.route("/api/reference-data")
def api_reference_data():
    type_key = request.args.get("type", "bug")
    if type_key not in TEMPLATES:
        return jsonify({"error": f"Unknown ticket type '{type_key}'"}), 400

    if _gh is None:
        return jsonify(
            {
                "labels": [],
                "assignees": [],
                "milestones": [],
                "suggested_labels": [],
                "suggested_assignees": [],
                "suggested_milestone": None,
                "error": _gh_error,
            }
        )

    try:
        labels = _gh.get_labels()
        assignees = _gh.get_collaborators()
        milestones = _gh.get_milestones()
    except Exception as e:
        return jsonify({"error": f"GitHub API error: {e}"}), 502

    suggested_labels = suggest_labels(type_key, labels, repo_key=_repo_key())
    return jsonify(
        {
            "labels": labels,
            "assignees": assignees,
            "milestones": [{"number": m["number"], "title": m["title"]} for m in milestones],
            "suggested_labels": suggested_labels,
            # seeded from the suggested labels above (nothing's actually been picked yet);
            # /api/suggest-assignees re-suggests once the user changes label checkboxes.
            "suggested_assignees": suggest_assignees(type_key, assignees, labels=suggested_labels, repo_key=_repo_key()),
            "suggested_milestone": suggest_milestone(milestones, repo_key=_repo_key()),
            "error": None,
        }
    )


@app.route("/api/suggest-assignees")
def api_suggest_assignees():
    """Re-suggest assignees for the labels actually checked right now — called
    live as the user toggles label checkboxes, so a roster-based suggestion
    (e.g. Frontend label -> the Frontend Lead from skills.md) shows up even
    when the user's label choice differs from what was pre-suggested."""
    type_key = request.args.get("type", "bug")
    if type_key not in TEMPLATES:
        return jsonify({"error": f"Unknown ticket type '{type_key}'"}), 400
    labels = [l for l in (request.args.get("labels") or "").split(",") if l]

    if _gh is None:
        return jsonify({"suggested_assignees": [], "error": _gh_error})
    try:
        assignees = _gh.get_collaborators()
    except Exception as e:
        return jsonify({"error": f"GitHub API error: {e}"}), 502

    return jsonify({"suggested_assignees": suggest_assignees(type_key, assignees, labels=labels, repo_key=_repo_key()), "error": None})


@app.route("/api/duplicate-check")
def api_duplicate_check():
    title = (request.args.get("title") or "").strip()
    if not title:
        return jsonify({"matches": []})
    if _gh is None:
        return jsonify({"matches": [], "error": _gh_error})
    try:
        existing = _gh.get_open_issues_titles()
    except Exception as e:
        return jsonify({"matches": [], "error": str(e)})
    lowered = title.lower()
    matches = [f"#{n} {t}" for n, t in existing if lowered in t.lower()]
    return jsonify({"matches": matches[:5]})


@app.route("/api/ai-draft", methods=["POST"])
def api_ai_draft():
    if not ai_configured():
        return jsonify({"error": f"AI not configured — set the {get_ai_provider()} API key in .env, or switch provider"}), 400

    type_key = request.form.get("type", "bug")
    if type_key not in TEMPLATES:
        return jsonify({"error": f"Unknown ticket type '{type_key}'"}), 400
    story = request.form.get("story", "").strip()
    if not story:
        return jsonify({"error": "Description is required"}), 400

    template = TEMPLATES[type_key]
    tmp_path = None
    screenshot = request.files.get("screenshot")
    try:
        if screenshot and screenshot.filename:
            suffix = os.path.splitext(screenshot.filename)[1] or ".png"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                screenshot.save(tmp.name)
                tmp_path = tmp.name

        ai_result = ai_generate_ticket(type_key, template, story, tmp_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    if not ai_result:
        return jsonify({"error": "AI generation failed — check server console for details"}), 502

    title = ai_result.get("title", "").strip()
    fields = {k: v for k, v in ai_result.items() if k != "title"}
    body, _ = render_body(type_key, title, fields)
    return jsonify({"title": title, "fields": fields, "body": body})


@app.route("/api/ai-draft-batch", methods=["POST"])
def api_ai_draft_batch():
    """Split one block of raw notes (possibly describing several distinct
    issues from one test session) into multiple drafts in a single AI call,
    instead of round-tripping /api/ai-draft once per issue."""
    if not ai_configured():
        return jsonify({"error": f"AI not configured — set the {get_ai_provider()} API key in .env, or switch provider"}), 400

    data = request.get_json(force=True)
    type_key = data.get("type", "bug")
    if type_key not in TEMPLATES:
        return jsonify({"error": f"Unknown ticket type '{type_key}'"}), 400
    raw_notes = (data.get("notes") or "").strip()
    if not raw_notes:
        return jsonify({"error": "Notes are required"}), 400

    template = TEMPLATES[type_key]
    ai_results = ai_generate_tickets_batch(type_key, template, raw_notes)
    if not ai_results:
        return jsonify({"error": "AI batch generation failed or found no issues — check server console"}), 502

    drafts = []
    for ai_result in ai_results:
        title = ai_result.get("title", "").strip()
        fields = {k: v for k, v in ai_result.items() if k != "title"}
        body, _ = render_body(type_key, title, fields)
        drafts.append({"title": title, "fields": fields, "body": body})
    return jsonify({"drafts": drafts})


@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json(force=True)
    type_key = data.get("type", "bug")
    if type_key not in TEMPLATES:
        return jsonify({"error": f"Unknown ticket type '{type_key}'"}), 400
    body, processed = render_body(type_key, data.get("title", ""), data.get("field_values", {}))
    return jsonify({"body": body, "field_values": processed})


def _create_one(type_key, title, field_values, labels, assignees, milestone_number, dry_run, mode_label):
    body, _ = render_body(type_key, title, field_values)
    issue_type = ISSUE_TYPE_MAP.get(type_key)

    if dry_run:
        return {
            "dry_run": True,
            "title": title,
            "type": type_key,
            "body": body,
            "labels": labels,
            "assignees": assignees,
            "milestone_number": milestone_number,
            "issue_type": issue_type,
        }

    if _gh is None:
        raise RuntimeError(_gh_error or "GitHub not configured")

    issue = _gh.create_issue(title, body, labels, assignees, milestone_number, issue_type)
    log_ticket_locally(
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "repo_key": _repo_key(),
            "issue_number": issue["number"],
            "url": issue["html_url"],
            "type": type_key,
            "title": title,
            "mode": mode_label,
            "labels": labels,
            "assignees": assignees,
            "milestone": milestone_number,
            "project": None,
            "status": None,
            "relationships": [],
        }
    )
    return {
        "dry_run": False,
        "title": title,
        "issue_number": issue["number"],
        "url": issue["html_url"],
        "labels": labels,
        "assignees": assignees,
        "milestone_number": milestone_number,
    }


@app.route("/api/create", methods=["POST"])
def api_create():
    data = request.get_json(force=True)
    type_key = data.get("type", "bug")
    if type_key not in TEMPLATES:
        return jsonify({"error": f"Unknown ticket type '{type_key}'"}), 400
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    try:
        result = _create_one(
            type_key,
            title,
            data.get("field_values", {}),
            data.get("labels", []),
            data.get("assignees", []),
            data.get("milestone_number"),
            bool(data.get("dry_run", True)),
            data.get("mode_label", "UI"),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(result)


@app.route("/api/create-batch", methods=["POST"])
def api_create_batch():
    data = request.get_json(force=True)
    type_key = data.get("type", "bug")
    if type_key not in TEMPLATES:
        return jsonify({"error": f"Unknown ticket type '{type_key}'"}), 400
    milestone_number = data.get("milestone_number")
    dry_run = bool(data.get("dry_run", True))
    tickets = data.get("tickets", [])
    if not tickets:
        return jsonify({"error": "No tickets in batch"}), 400

    results = []
    for t in tickets:
        title = (t.get("title") or "").strip()
        if not title:
            results.append({"error": "Skipped a ticket with no title"})
            continue
        try:
            result = _create_one(
                type_key,
                title,
                t.get("field_values", {}),
                t.get("labels", []),
                t.get("assignees", []),
                milestone_number,
                dry_run,
                "AI-assisted (batch, UI)",
            )
            results.append(result)
        except Exception as e:
            results.append({"error": str(e), "title": title})
    return jsonify({"results": results})


@app.route("/api/ticket-log")
def api_ticket_log():
    log = load_ticket_log(repo_key=_repo_key())
    return jsonify({"tickets": list(reversed(log))[:50]})


# ============================================================================
# BULK BOARD MANAGEMENT — filter existing tickets by swimlane (the Projects V2
# "Status" field) and/or label, then bulk-move them to another sprint and/or
# bulk add/remove labels. Defaults to dry run like everything else here.
# ============================================================================


def _get_board_project():
    """The Projects V2 board this repo's tickets live on, + its Status field.
    A repo can have more than one board linked to it (a stray template, an
    unrelated project someone created) — the project name entered on Connect
    picks one by exact title when that happens; without it, the first one is
    used and a warning is surfaced via /api/board/meta so a wrong pick doesn't
    silently go unnoticed."""
    global _board_project_warning
    if _gh is None:
        raise RuntimeError(_gh_error or "GitHub not configured")
    projects = _gh.get_projects_v2()
    if not projects:
        raise RuntimeError("No GitHub Projects (v2) found for this repo")

    if _current_project_name:
        project = next((p for p in projects if p["title"].lower() == _current_project_name.lower()), None)
        if not project:
            found = ", ".join(p["title"] for p in projects)
            raise RuntimeError(f"Project '{_current_project_name}' not found among this repo's projects: {found}")
    else:
        project = projects[0]
        if len(projects) > 1:
            others = ", ".join(f"'{p['title']}'" for p in projects if p is not project)
            _board_project_warning = (
                f"This repo has {len(projects)} Projects linked to it — using '{project['title']}'. "
                f"If that's wrong, set the project name on the Connect panel to one of: {others}."
            )

    fields = _gh.get_project_fields(project["id"])
    status_field = next((f for f in fields if f.get("name", "").lower() == "status"), None)
    if not status_field:
        raise RuntimeError(f"Project '{project['title']}' has no Status field")
    return project, status_field


# A full board fetch pages through every item (1900+ on this org's board, ~25s)
# to get correct results — see the max_items fix in GitHubClient.get_project_items.
# That's too slow to pay on every filter click, so cache it briefly; any bulk
# write below invalidates it immediately so the next fetch is always fresh.
_BOARD_ITEMS_CACHE_TTL = 45  # seconds


def _get_all_board_items(project_id):
    age = time.time() - _board_items_cache["fetched_at"]
    if _board_items_cache["items"] is not None and age < _BOARD_ITEMS_CACHE_TTL:
        return _board_items_cache["items"]
    items = _gh.get_project_items(project_id)
    _board_items_cache["items"] = items
    _board_items_cache["fetched_at"] = time.time()
    return items


@app.route("/api/board/meta")
def api_board_meta():
    try:
        project, status_field = _get_board_project()
        labels = _gh.get_labels()
        milestones = _gh.get_milestones()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(
        {
            "project_title": project["title"],
            "status_options": status_field.get("options", []),
            "labels": labels,
            "milestones": [{"number": m["number"], "title": m["title"]} for m in milestones],
            "warning": _board_project_warning,
        }
    )


@app.route("/api/board/items")
def api_board_items():
    status_filter = {s for s in (request.args.get("status") or "").split(",") if s}
    label_filter = {l for l in (request.args.get("labels") or "").split(",") if l}
    # current-sprint filter: "none" (a milestone-less ticket) alongside real milestone numbers
    milestone_filter = {m for m in (request.args.get("milestone") or "").split(",") if m}
    try:
        project, _ = _get_board_project()
        items = _get_all_board_items(project["id"])
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    def matches(item):
        if status_filter and item.get("status") not in status_filter:
            return False
        if label_filter and not (label_filter & set(item.get("labels") or [])):
            return False
        if milestone_filter:
            ms = item.get("milestone_number")
            ms_key = str(ms) if ms is not None else "none"
            if ms_key not in milestone_filter:
                return False
        return True

    return jsonify({"items": [i for i in items if matches(i)]})


@app.route("/api/board/bulk-update", methods=["POST"])
def api_board_bulk_update():
    data = request.get_json(force=True)
    items = data.get("items", [])
    if not items:
        return jsonify({"error": "No tickets selected"}), 400
    dry_run = bool(data.get("dry_run", True))

    apply_milestone = bool(data.get("apply_milestone"))
    milestone_number = data.get("milestone_number")  # None is valid here — means "remove from sprint"
    apply_status = bool(data.get("apply_status"))
    status_option_id = data.get("status_option_id")
    add_labels = set(data.get("add_labels") or [])
    remove_labels = set(data.get("remove_labels") or [])
    apply_labels = bool(add_labels or remove_labels)

    project = None
    status_field = None
    if apply_status:
        try:
            project, status_field = _get_board_project()
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    results = []
    for item in items:
        issue_number = item.get("issue_number")
        current_labels = set(item.get("labels") or [])
        planned = {
            "issue_number": issue_number,
            "title": item.get("title"),
        }
        if apply_labels:
            planned["labels"] = sorted((current_labels - remove_labels) | add_labels)
        if apply_milestone:
            planned["milestone_number"] = milestone_number
        if apply_status:
            planned["status_option_id"] = status_option_id

        if dry_run:
            results.append({**planned, "dry_run": True})
            continue

        try:
            if apply_labels:
                _gh.update_issue_labels(issue_number, planned["labels"])
            if apply_milestone:
                _gh.update_issue_milestone(issue_number, milestone_number)
            if apply_status:
                _gh.set_project_status(project["id"], item["item_id"], status_field["id"], status_option_id)
            results.append({**planned, "dry_run": False, "ok": True})
        except Exception as e:
            results.append({"issue_number": issue_number, "title": item.get("title"), "ok": False, "error": str(e)})

    if not dry_run:
        _board_items_cache["items"] = None  # force a fresh full fetch next load — don't show stale pre-edit state
    return jsonify({"results": results})


if __name__ == "__main__":
    # Defaults preserve today's behavior (localhost-only, debug/auto-reload on)
    # for a plain `python app.py`. The Dockerfile overrides FLASK_HOST=0.0.0.0
    # (required for the container's port mapping to reach it at all) and
    # FLASK_DEBUG=0 (Werkzeug's debugger is a real RCE risk on any port that's
    # actually reachable from outside the process — see the "host it on the
    # network" discussion earlier; a container's mapped port counts as that).
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
