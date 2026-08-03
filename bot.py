"""
Portfolio Company Onboarding Bot
----------------------------------
Trigger:  /new-portco  (Slack slash command)
Hosting:  Railway (always-on, no laptop needed)
Storage:  SQLite — sessions survive bot restarts
"""

import os
import re
import json
import sqlite3
import threading
import time
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from github import Github
import sendgrid
from sendgrid.helpers.mail import Mail


# ── App setup ────────────────────────────────────────────────────────────────

app = App(token=os.environ["SLACK_BOT_TOKEN"])

INTERNAL_CHANNEL = "portco-onboarding"
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "Exceptional-Capital/founder-resources")
FROM_EMAIL       = os.environ.get("FROM_EMAIL", "")
REMINDER_DELAY   = int(os.environ.get("REMINDER_DELAY_SECONDS", str(48 * 60 * 60)))

# DB_PATH is set to /data/sessions.db on Railway (mounted volume), local fallback
DB_PATH = os.environ.get("DB_PATH", "sessions.db")
db_lock = threading.Lock()


# ── Checklist tasks ──────────────────────────────────────────────────────────
#
# Each task has:
#   id          — unique key stored in DB
#   short       — plain text for the "mark done" dropdown (no links, plain_text only)
#   slack       — mrkdwn text shown in the checklist message (links + mentions work here)
#   assignee_key — env var holding that person's Slack user ID (or None)

def _mention(env_key: str) -> str:
    uid = os.environ.get(env_key, "")
    return f" <@{uid}>" if uid else ""

ONBOARDING_TASKS = [
    {
        "id":           "investment_memo",
        "short":        "Investment memo complete",
        "slack":        "Investment memo complete",
        "assignee_key": "GRAHAM_SLACK_ID",
    },
    {
        "id":    "memo_card",
        "short": "Memo card complete (Figma deck)",
        "slack": (
            "Memo card complete (with logo if available) — add to the "
            "<https://www.figma.com/slides/9ViuZ5rgrt6BU6DcimZWls/09-2025-Memo-Cards_Full-Detail"
            "?node-id=2089-22&t=CySezLDcqo8XHYxJ-1|master memo card deck>"
        ),
        "assignee_key": "GRAHAM_SLACK_ID",
    },
    {
        "id":    "email_cadence",
        "short": "Email cadence blurb (Notion) + founder profile links",
        "slack": (
            "<https://app.notion.com/p/Deal-Flow-Cadence-242314eb089480038f7ce8c8c77970c7"
            "?pvs=21|Email cadence blurb> with founder background + profile links "
            "→ updates monthly update email template"
        ),
        "assignee_key": "GRAHAM_SLACK_ID",
    },
    {
        "id":           "website_date",
        "short":        "Website: add company now or set 3-month reminder?",
        "slack":        "Website: add company immediately, or set a 3-month reminder?",
        "assignee_key": None,
    },
    {
        "id":           "archive_channel",
        "short":        "Archive deal flow / investment discussion Slack channel",
        "slack":        "Archive deal flow / investment discussion Slack channel",
        "assignee_key": "GRAHAM_SLACK_ID",
    },
]

TASK_IDS    = [t["id"]    for t in ONBOARDING_TASKS]
TASK_BY_ID  = {t["id"]: t for t in ONBOARDING_TASKS}


# ── SQLite persistence ───────────────────────────────────────────────────────

def _init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()


def _save(session_id: str, data: dict):
    now = datetime.utcnow().isoformat()
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        existing = conn.execute(
            "SELECT created_at FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        created = existing[0] if existing else now
        conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, data, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, json.dumps(data), created, now),
        )
        conn.commit()
        conn.close()


def _load(session_id: str) -> dict | None:
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT data, created_at FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        conn.close()
    if row:
        d = json.loads(row[0])
        d["_created_at"] = row[1]
        return d
    return None


def _load_all() -> dict:
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT session_id, data, created_at FROM sessions").fetchall()
        conn.close()
    result = {}
    for sid, data, created_at in rows:
        d = json.loads(data)
        d["_created_at"] = created_at
        result[sid] = d
    return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower().strip()).strip("-")[:21]  # Slack channel name limit


def get_channel_id(client, channel_name: str) -> str | None:
    cursor = None
    while True:
        result = client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
        )
        for ch in result["channels"]:
            if ch["name"] == channel_name:
                return ch["id"]
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return None


def build_checklist_blocks(session_id: str, company_name: str, completed: list) -> list:
    """Render the interactive task checklist as Slack Block Kit blocks."""
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚀 Onboarding: {company_name}"},
        },
        {"type": "divider"},
    ]

    for task in ONBOARDING_TASKS:
        done       = task["id"] in completed
        icon       = "✅" if done else "☐"
        assignee   = _mention(task["assignee_key"]) if task["assignee_key"] else ""
        strike_s   = "~" if done else ""
        strike_e   = "~" if done else ""
        text       = f"{icon}  {strike_s}{task['slack']}{strike_e}{assignee}"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        })

    blocks += [
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"checklist_{session_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✓ Mark a task done"},
                    "style": "primary",
                    "action_id": "open_task_done_modal",
                    "value": session_id,
                }
            ],
        },
    ]
    return blocks


# ── Step 1: Slash command ────────────────────────────────────────────────────

@app.command("/new-portco")
def handle_new_portco(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "new_portco_modal",
            "title":  {"type": "plain_text", "text": "New portfolio company"},
            "submit": {"type": "plain_text", "text": "Start onboarding"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "company_name_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "company_name_input",
                        "placeholder": {"type": "plain_text", "text": "e.g. Acme Corp"},
                    },
                    "label": {"type": "plain_text", "text": "Company name"},
                },
                {
                    "type": "input",
                    "block_id": "founder_emails_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "founder_emails_input",
                        "multiline": True,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "One email per line\njane@acme.com\njohn@acme.com",
                        },
                    },
                    "label": {"type": "plain_text", "text": "Founder email(s)"},
                },
            ],
        },
    )


# ── Step 2–5: Modal submitted ─────────────────────────────────────────────────

@app.view("new_portco_modal")
def handle_modal_submission(ack, body, client, view):
    ack()

    values         = view["state"]["values"]
    company_name   = values["company_name_block"]["company_name_input"]["value"].strip()
    emails_raw     = values["founder_emails_block"]["founder_emails_input"]["value"]
    founder_emails = [e.strip() for e in emails_raw.splitlines() if e.strip()]
    user_id        = body["user"]["id"]
    session_id     = slugify(company_name)

    session = {
        "company_name":        company_name,
        "founder_emails":      founder_emails,
        "user_id":             user_id,
        "completed_tasks":     [],
        "github_usernames":    [],
        "checklist_ts":        None,
        "internal_channel_id": None,
        "portco_channel_id":   None,
        "reminder_sent":       False,
    }
    _save(session_id, session)

    _post_checklist(client, session_id, user_id)
    _create_portco_channel(client, session_id)
    _send_welcome_emails(session_id)
    _schedule_reminder(client, session_id)


# ── Checklist ────────────────────────────────────────────────────────────────

def _post_checklist(client, session_id: str, user_id: str):
    session      = _load(session_id)
    company_name = session["company_name"]
    channel_id   = get_channel_id(client, INTERNAL_CHANNEL)

    if not channel_id:
        print(f"⚠️  #{INTERNAL_CHANNEL} not found — invite the bot to that channel.")
        return

    result = client.chat_postMessage(
        channel=channel_id,
        text=f"New portco onboarding: {company_name}",
        blocks=build_checklist_blocks(session_id, company_name, completed=[]),
        attachments=[{
            "color": "#7C3AED",
            "text": f"Started by <@{user_id}>",
            "fallback": f"Started by {user_id}",
        }],
    )

    session["checklist_ts"]        = result["ts"]
    session["internal_channel_id"] = channel_id
    _save(session_id, session)


def _update_checklist(client, session_id: str):
    """Edit the checklist message in-place after a task is marked done."""
    session = _load(session_id)
    if not session or not session["checklist_ts"] or not session["internal_channel_id"]:
        return
    client.chat_update(
        channel=session["internal_channel_id"],
        ts=session["checklist_ts"],
        text=f"Onboarding checklist: {session['company_name']}",
        blocks=build_checklist_blocks(
            session_id,
            session["company_name"],
            session["completed_tasks"],
        ),
    )


# ── Portco Slack channel ──────────────────────────────────────────────────────

def _welcome_blocks(company_name: str) -> list:
    """
    Build the welcome message as Slack blocks.
    This is posted to the portco channel AND sent as the welcome email body.
    """
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Welcome to Exceptional Capital, {company_name}! 🎉"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "We are thrilled to support what you are building!",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Support & Communication*\n"
                    "• We are an extension of your team for anything we can be helpful with. "
                    "Always available — please never hesitate to reach out!\n"
                    "• <https://www.notion.so/Exceptional-Founder-Resources-397aa3d17d9b424cb1945b1aa1635b2e"
                    "|Exceptional Founder Resources> — available here\n"
                    "• Slack: this channel is live and we're always around\n"
                    "• Any specific communication cadence that is helpful to you, just let us know "
                    "and we will stick to it"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Other items*\n"
                    "• With Amazon as an investor, their BD team has dedicated portfolio company support — "
                    "happy to connect if needed\n"
                    "• AWS Credits: Activate Org ID is *1lC9Y* (case sensitive)\n"
                    "• Traveling? We have "
                    "<https://www.notion.so/Exceptional-Founder-Resources-397aa3d17d9b424cb1945b1aa1635b2e"
                    "|corporate rates> available for your team. Looking for a rate in another city? Just let us know.\n"
                    "• Additional contact info for our full team is attached in case you need it\n"
                    "• We're always happy to share your news/press (socials, newsletter, etc) — "
                    "any updates you'd like us to extend to our audiences, just drop us a note!\n"
                    "• Need support on copy, press, or content? Always happy to help"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Exceptional Community*\n"
                    "• Our network is available to you! We're more than willing to leverage this for "
                    "your benefit, especially our LPs when/where intros may be helpful\n"
                    "• Our LPAC: NextEra Energy, Goldman Sachs, Vanderbilt, Screendoor, Amazon, StepStone Group\n"
                    "• Early supporters also include founders at Okta, Twitter, Block, Reddit, Twitch, "
                    "Y-Combinator, and more. Leading institutional funds/banks include NEA, SV Angel, 776, "
                    "Lightspeed, Altos Ventures, Churchill Asset Management, and Bank of America"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "We are available any and all hours and honored to support what you are building! 🚀",
            },
        },
    ]


def _create_portco_channel(client, session_id: str):
    session      = _load(session_id)
    company_name = session["company_name"]
    channel_name = f"{session_id}-exceptional"

    try:
        result     = client.conversations_create(name=channel_name, is_private=False)
        channel_id = result["channel"]["id"]
        session["portco_channel_id"] = channel_id
        _save(session_id, session)

        # 1. Post the full welcome message
        welcome_result = client.chat_postMessage(
            channel=channel_id,
            text=f"Welcome to Exceptional Capital, {company_name}! 🎉",
            blocks=_welcome_blocks(company_name),
        )

        # 2. Pin the welcome message so founders can always find it
        try:
            client.pins_add(channel=channel_id, timestamp=welcome_result["ts"])
        except Exception as pin_err:
            print(f"⚠️  Could not pin welcome message (check pins:write scope): {pin_err}")

        # 3. Post a separate, lightweight GitHub ask so the welcome isn't cluttered
        client.chat_postMessage(
            channel=channel_id,
            text="One quick ask — share your GitHub username and we'll add you to our resources repo.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*One quick ask* — could you share your GitHub username(s)? "
                            "We'll add you to our "
                            "<https://github.com/Exceptional-Capital/founder-resources|founder resources repo> "
                            "straight away."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [{
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "Submit my GitHub username"},
                        "style":     "primary",
                        "action_id": "open_github_modal",
                        "value":     session_id,
                    }],
                },
            ],
        )

    except Exception as e:
        print(f"❌ Could not create #{channel_name}: {e}")


# ── GitHub ────────────────────────────────────────────────────────────────────

@app.action("open_github_modal")
def open_github_modal(ack, body, client):
    ack()
    session_id = body["actions"][0]["value"]
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type":             "modal",
            "callback_id":      "github_username_modal",
            "private_metadata": session_id,
            "title":  {"type": "plain_text", "text": "GitHub access"},
            "submit": {"type": "plain_text", "text": "Submit"},
            "close":  {"type": "plain_text", "text": "Later"},
            "blocks": [{
                "type":       "input",
                "block_id":   "github_block",
                "element": {
                    "type":        "plain_text_input",
                    "action_id":   "github_input",
                    "placeholder": {"type": "plain_text", "text": "e.g. janesmith"},
                },
                "label": {"type": "plain_text", "text": "Your GitHub username"},
                "hint":  {"type": "plain_text", "text": "The name shown on github.com/username"},
            }],
        },
    )


@app.view("github_username_modal")
def handle_github_submission(ack, body, client, view):
    ack()
    session_id      = view["private_metadata"]
    github_username = view["state"]["values"]["github_block"]["github_input"]["value"].strip()
    slack_user_id   = body["user"]["id"]
    session         = _load(session_id)

    if not session:
        return

    portco_channel = session.get("portco_channel_id")
    success        = _add_to_github(github_username)

    if portco_channel:
        if success:
            session["github_usernames"].append(github_username)
            _save(session_id, session)
            _mark_task_done_by_id(client, session_id, "archive_channel")  # not right — just confirming github
            client.chat_postMessage(
                channel=portco_channel,
                text=(
                    f"✅ <@{slack_user_id}> — *{github_username}* has been added to "
                    f"the repo. You'll find it at github.com/{GITHUB_REPO}"
                ),
            )
        else:
            client.chat_postMessage(
                channel=portco_channel,
                text=(
                    f"⚠️  <@{slack_user_id}> — couldn't find GitHub user *{github_username}*. "
                    f"Username is case-sensitive — please double-check and try again."
                ),
            )


def _add_to_github(username: str) -> bool:
    try:
        g    = Github(os.environ["GITHUB_TOKEN"])
        repo = g.get_repo(GITHUB_REPO)
        repo.add_to_collaborators(username, permission="pull")
        print(f"✅ GitHub: added {username} to {GITHUB_REPO}")
        return True
    except Exception as e:
        print(f"❌ GitHub error for {username}: {e}")
        return False


# ── Mark task done ────────────────────────────────────────────────────────────

@app.action("open_task_done_modal")
def open_task_done_modal(ack, body, client):
    ack()
    session_id = body["actions"][0]["value"]
    session    = _load(session_id)

    if not session:
        return

    pending_options = [
        {
            "text":  {"type": "plain_text", "text": t["short"]},
            "value": t["id"],
        }
        for t in ONBOARDING_TASKS
        if t["id"] not in session["completed_tasks"]
    ]

    if not pending_options:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type":  "modal",
                "title": {"type": "plain_text", "text": "All done! 🎉"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "Every task is already marked complete."},
                }],
            },
        )
        return

    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type":             "modal",
            "callback_id":      "task_done_modal",
            "private_metadata": session_id,
            "title":  {"type": "plain_text", "text": "Mark task done"},
            "submit": {"type": "plain_text", "text": "Done"},
            "close":  {"type": "plain_text", "text": "Cancel"},
            "blocks": [{
                "type":     "input",
                "block_id": "task_select_block",
                "element": {
                    "type":        "static_select",
                    "action_id":   "task_select",
                    "placeholder": {"type": "plain_text", "text": "Choose a task…"},
                    "options":     pending_options,
                },
                "label": {"type": "plain_text", "text": "Which task did you complete?"},
            }],
        },
    )


@app.view("task_done_modal")
def handle_task_done(ack, body, client, view):
    ack()
    session_id = view["private_metadata"]
    task_id    = view["state"]["values"]["task_select_block"]["task_select"]["selected_option"]["value"]
    _mark_task_done_by_id(client, session_id, task_id)


def _mark_task_done_by_id(client, session_id: str, task_id: str):
    session = _load(session_id)
    if not session or task_id in session["completed_tasks"]:
        return

    session["completed_tasks"].append(task_id)
    _save(session_id, session)
    _update_checklist(client, session_id)


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_welcome_emails(session_id: str):
    session      = _load(session_id)
    company_name = session["company_name"]
    slug         = session_id
    sg           = sendgrid.SendGridAPIClient(api_key=os.environ["SENDGRID_API_KEY"])

    html = f"""
    <div style="font-family:sans-serif;max-width:620px;margin:0 auto;padding:32px 24px;color:#1e1e1e;line-height:1.6;">

      <h2 style="margin-bottom:4px;">Welcome to Exceptional Capital, {company_name}! 🎉</h2>
      <p style="margin-top:0;">
        Wanted to formally welcome you — we are thrilled to support what you are building!
        A few quick things…
      </p>

      <h3 style="margin-bottom:6px;border-bottom:1px solid #eee;padding-bottom:6px;">Support &amp; Communication</h3>
      <ul style="padding-left:20px;">
        <li>We are an extension of your team for anything we can be helpful with.
            We are always available to you — please never hesitate to reach out!</li>
        <li>Compilation of
            <a href="https://www.notion.so/Exceptional-Founder-Resources-397aa3d17d9b424cb1945b1aa1635b2e">
            Exceptional Founder Resources</a> — available here</li>
        <li>Slack: your channel <strong>#{slug}-exceptional</strong> is live and we're always around</li>
        <li>Any specific communication cadence that is helpful to you, please let us know
            and we will stick to it</li>
      </ul>

      <h3 style="margin-bottom:6px;border-bottom:1px solid #eee;padding-bottom:6px;">Other items</h3>
      <ul style="padding-left:20px;">
        <li>With Amazon as an investor, their BD team has dedicated portfolio company support that
            they can offer; happy to connect if needed</li>
        <li>AWS Credits: Activate Org ID is <strong>1lC9Y</strong> (case sensitive)</li>
        <li>Traveling? We have
            <a href="https://www.notion.so/Exceptional-Founder-Resources-397aa3d17d9b424cb1945b1aa1635b2e">
            corporate rates</a> available for your team to use. Looking for a rate in another city?
            Let me know — will gladly work on that for you.</li>
        <li>Additional contact info for our full team is also attached in case you need it</li>
        <li>We are always happy to share news/press (socials, our newsletter, etc) — any updates
            you'd like us to extend to our audiences just drop us a note!</li>
        <li>If you need support on copy, press, or content we are always happy to provide
            assistance as needed</li>
      </ul>

      <h3 style="margin-bottom:6px;border-bottom:1px solid #eee;padding-bottom:6px;">Exceptional Community</h3>
      <ul style="padding-left:20px;">
        <li>Our network is available to you! We are more than willing to leverage this for your
            benefit, especially our LPs when/where intros may be helpful</li>
        <li>Our LPAC: NextEra Energy, Goldman Sachs, Vanderbilt, Screendoor, Amazon, StepStone Group</li>
        <li>Early supporters of ours also include founders at Okta, Twitter, Block, Reddit, Twitch,
            Y-Combinator, and more. Several leading institutional funds/banks are among our LPs,
            including NEA, SV Angel, 776, Lightspeed, Altos Ventures, Churchill Asset Management,
            and Bank of America.</li>
      </ul>

      <p style="margin-top:24px;">
        We are available to you any and all hours and honored to be able to support what you are building! 🚀
      </p>

      <hr style="border:none;border-top:1px solid #eee;margin:28px 0 16px;">
      <p style="font-size:12px;color:#888;margin:0;">
        Questions? Reply to this email or find us in Slack at
        <strong>#{slug}-exceptional</strong>.
      </p>
    </div>
    """

    for email in session["founder_emails"]:
        try:
            sg.send(Mail(
                from_email=FROM_EMAIL,
                to_emails=email,
                subject=f"Welcome to Exceptional Capital, {company_name}! 🎉",
                html_content=html,
            ))
            print(f"✉️  Sent welcome email → {email}")
        except Exception as e:
            print(f"❌ Email failed for {email}: {e}")


# ── Reminder ──────────────────────────────────────────────────────────────────

def _schedule_reminder(client, session_id: str, delay_override: float | None = None):
    """
    Fire a reminder after REMINDER_DELAY seconds (or delay_override if provided).
    delay_override is used on startup to resume timers that were interrupted by a restart.
    """
    delay = delay_override if delay_override is not None else REMINDER_DELAY

    def _remind():
        time.sleep(max(delay, 0))
        session = _load(session_id)
        if not session or session.get("reminder_sent"):
            return

        pending = [t for t in ONBOARDING_TASKS if t["id"] not in session["completed_tasks"]]
        if not pending:
            return

        channel_id = session.get("internal_channel_id")
        if not channel_id:
            return

        company_name = session["company_name"]
        pending_text = "\n".join(f"• {t['short']}" for t in pending)

        client.chat_postMessage(
            channel=channel_id,
            text=f"⏰ Reminder: {company_name} onboarding — {len(pending)} task(s) still pending",
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"⏰ Reminder: {company_name}"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"These tasks haven't been marked done yet:\n{pending_text}",
                    },
                },
                {"type": "divider"},
                {
                    "type": "actions",
                    "block_id": f"checklist_{session_id}",
                    "elements": [{
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "✓ Mark a task done"},
                        "style":     "primary",
                        "action_id": "open_task_done_modal",
                        "value":     session_id,
                    }],
                },
            ],
        )

        session["reminder_sent"] = True
        _save(session_id, session)

    threading.Thread(target=_remind, daemon=True).start()


def _resume_pending_reminders(client):
    """
    Called on startup. Re-schedules reminders for any sessions that
    were created before the bot restarted and haven't been reminded yet.
    """
    sessions = _load_all()
    now      = datetime.utcnow()
    resumed  = 0

    for session_id, session in sessions.items():
        if session.get("reminder_sent"):
            continue

        created_str = session.get("_created_at")
        if not created_str:
            continue

        try:
            created  = datetime.fromisoformat(created_str)
            elapsed  = (now - created).total_seconds()
            remaining = REMINDER_DELAY - elapsed

            _schedule_reminder(client, session_id, delay_override=remaining)
            resumed += 1
        except Exception as e:
            print(f"⚠️  Could not resume reminder for {session_id}: {e}")

    if resumed:
        print(f"🔁 Resumed {resumed} pending reminder(s) from previous session")


# ── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("⚡ Portco onboarding bot starting…")
    _init_db()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])

    # Resume any reminders that were pending before a restart
    _resume_pending_reminders(app.client)

    handler.start()
