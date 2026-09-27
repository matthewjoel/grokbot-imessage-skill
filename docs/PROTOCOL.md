# iMessage Helper Bridge Protocol

This document describes the JSON-based request/response protocol that both Claude Cowork and Grok Bot use to communicate with the macOS helper.

Protocol version: `1.3`

Clients should call `status` before their first message operation and require a
compatible `protocol_version`. Minor versions add backward-compatible actions or
fields; a future major-version mismatch must fail closed with upgrade guidance.

Protocol history:

- `1.3` — optional outbound image attachment on `send_preview` / `send`.
  Text-only requests are unchanged. The send nonce binds the image identity
  (path, filename, mime, size, sha256) when an image is present.
- `1.2` — adds the manager-only `list_chats` action, the bridge role table
  below, and the `bridge_role` / `allowed_actions` fields in `status`.
- `1.1` — `status` action and protocol compatibility reporting.

## Bridge Roles

Every bridge has a role. The worker reads it from `IMESSAGE_BRIDGE_ROLE`
(set by the wrapper; the DIY installers set nothing, which means `host`) and
enforces the table below **inside the worker**, before any database is opened.
Hiding an action in a host, plugin, or app layer is not sufficient and is not
relied on. Unknown role values fail closed: no action is served.

| Action | `host` | `manager` | Returns message bodies |
| --- | --- | --- | --- |
| `status` | yes | yes | no |
| `review`, `search`, `chat_history` | yes | **no** | yes |
| `response_stats` | yes | no | no |
| `contacts_lookup` | yes (policy-filtered) | yes | no |
| `send_preview`, `send` | yes | no | draft only |
| `list_chats` | **no** | yes | no |

A `host` bridge is what an AI host talks to. A `manager` bridge is operated by
a local management tool for policy discovery and diagnostics; it never serves a
body-returning action, and a host bridge never enumerates chats. Requesting an
action the role does not permit returns:

```json
{
  "id": "abc123",
  "ok": false,
  "error": "action not permitted on this bridge",
  "bridge_role": "host",
  "allowed_actions": ["chat_history", "contacts_lookup", "response_stats", "review", "search", "send", "send_preview", "status"]
}
```

`allowed_actions` in every error response, including `unknown action`, is the
list for the current role, not the worker's full action table.

## Architecture

```
  AI Host (agent context)              macOS (your local machine)
  -----------------------              -------------------------
  Writes request-<id>.json  -->  launchd watches control/requests/  -->
  Reads  response-<id>.json  <-- grokbot-imessage-helper (wrapper)   -->
                                 helper.py (FDA-granted, reads
                                 chat.db + AddressBook, drives osascript)
```

The AI host runs in a sandboxed environment (Linux for Cowork, potentially remote for Grok Bot) that cannot directly access `~/Library/Messages/chat.db`. A launchd agent bridges the two sides:

1. The AI host writes a JSON request file to `<bridge-folder>/control/requests/request-<id>.json`
2. launchd fires the helper within ~1 second (WatchPaths with 1s ThrottleInterval)
3. The helper reads the request, processes it, and writes a response to `<bridge-folder>/control/responses/response-<id>.json`
4. The AI host polls for and reads the response (typically 2–5s total)
5. The AI host deletes the response immediately after parsing it

## Bridge Folder Layout

In a standard installation, the bridge folder contains both code and runtime
state. A hardened installation keeps `bin/` under a root-owned code root and the
remaining directories under the user-owned bridge root:

```
<code-root>/
└── bin/
    ├── grokbot-imessage-helper       # Compiled C wrapper (FDA target)
    ├── grokbot-imessage-confirm        # Native, fail-safe send confirmation
    ├── helper.py                     # Python worker
    └── send_gate.py                  # Send nonce validation

<bridge-folder>/
├── control/
│   ├── requests/                     # AI host writes here
│   ├── responses/                    # Helper writes here
│   └── log.txt                       # Helper diagnostics
├── contacts/
│   ├── blocked_chats.txt             # User-maintained blocklist
│   ├── allowed_chats.txt             # Standard-mode optional allowlist
│   └── read_policy.txt                # blocklist or allowlist
└── nonces/                            # Short-lived send-preview nonces
```

For a standard install, `<code-root>` and `<bridge-folder>` are the same path.
Hardened allowlist entries live separately in a root-owned config directory.

## Request Format

All requests are JSON files with this structure:

```json
{
  "id": "<unique-request-id>",
  "action": "<action-name>",
  "params": { /* action-specific parameters */ }
}
```

Use a UUID or timestamp for `id`. The `id` must be unique within the bridge folder's lifetime.
The top-level JSON value and `params` must be objects, and `action` must be a
string. Request files are limited to 64 KiB. Invalid requests receive an error
response and do not prevent later queued requests from being processed.

## Response Format

All responses are JSON files with this structure:

```json
{
  "id": "<same-as-request-id>",
  "ok": true,
  "action": "<action-name>",
  /* action-specific response fields */
}
```

On error:

```json
{
  "id": "<same-as-request-id>",
  "ok": false,
  "error": "Error message"
}
```

## Actions

### `status` — Compatibility and installation checks

`status` does not read `chat.db` or return message content.

```json
{"id": "abc123", "action": "status", "params": {}}
```

The response includes `helper_version`, `protocol_version`, `bridge_role`,
`allowed_actions` (sorted, for this role), `code_root`, `bridge_root`,
`python_version`, policy metadata, and local boolean checks for the database,
request directories, and native confirmation helper.

---

### `review` — Triage recent messages

**Request:**
```json
{
  "id": "abc123",
  "action": "review",
  "params": {
    "days": 2
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "review",
  "days": 2,
  "counts": {
    "needs_reply": 3,
    "low_priority": 5,
    "skip": 12,
    "total_messages": 156
  },
  "needs_reply": [
    {
      "chat_id": "+14155551234",
      "label": "Alex Example",
      "contact_name": "Alex Example",
      "display_name": "",
      "last_ts": "2026-08-10T14:32:15",
      "last_text": "Are we still on for Thursday?",
      "context": [
        {
          "ts": "2026-08-10T14:32:15",
          "me": false,
          "text": "Are we still on for Thursday?"
        }
      ],
      "msg_count": 1
    }
  ],
  "low_priority": [ /* same structure as needs_reply */ ],
  "skip_summary": [
    {
      "chat_id": "chat123456789",
      "label": "Uber",
      "last_ts": "2026-08-10T09:15:00"
    }
  ]
}
```

Sorts threads into three buckets: `needs_reply` (actionable, full text included), `low_priority` (can wait, full text included), and `skip_summary` (summary only, no text — typically automated messages).

---

### `search` — Find messages by substring

**Request:**
```json
{
  "id": "abc123",
  "action": "search",
  "params": {
    "term": "dinner plans",
    "days": 30,
    "limit": 100
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "search",
  "term": "dinner plans",
  "days": 30,
  "match_count": 3,
  "matches": [
    {
      "chat_id": "+14155551234",
      "contact_name": "Alice",
      "ts": "2026-08-05T18:22:00",
      "is_from_me": false,
      "text": "Let's finalize dinner plans for Friday"
    }
  ]
}
```

Case-insensitive substring search across all threads. Results are sorted by date (newest first).

---

### `chat_history` — Recent messages in one thread

**Request:**
```json
{
  "id": "abc123",
  "action": "chat_history",
  "params": {
    "chat": "Alex Example",
    "days": 14,
    "limit": 100
  }
}
```

`chat` accepts:
- Contact name (resolved via Contacts.app)
- Phone number (any format; last 10 digits are matched)
- Email address
- **Note:** Group chat IDs (e.g., `chat123456789`) are NOT supported for sending. They work for read-only actions like `chat_history` and `review`.

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "chat_history",
  "chat_query": "Alex Example",
  "resolved_substr": "5551234",
  "count": 2,
  "messages": [
    {
      "chat_id": "+14155551234",
      "contact_name": "Alex Example",
      "ts": "2026-08-10T14:32:15",
      "is_from_me": false,
      "text": "Are we still on for Thursday?"
    },
    {
      "chat_id": "+14155551234",
      "contact_name": "",
      "ts": "2026-08-10T14:35:00",
      "is_from_me": true,
      "text": "Yes! See you at 3pm"
    }
  ]
}
```

---

### `response_stats` — Average reply time to one contact

**Request:**
```json
{
  "id": "abc123",
  "action": "response_stats",
  "params": {
    "chat": "Alex Example",
    "hours": 24
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "response_stats",
  "chat_query": "Alex Example",
  "resolved_substr": "5551234",
  "hours": 24,
  "sample_size": 15,
  "avg_seconds": 1098,
  "avg_human": "18.3m",
  "median_seconds": 480,
  "min_seconds": 12,
  "max_seconds": 7200,
  "total_inbound_messages": 23,
  "total_outbound_messages": 19
}
```

Computes reply-time statistics over the specified window.

---

### `contacts_lookup` — Find matching contacts

**Request:**
```json
{
  "id": "abc123",
  "action": "contacts_lookup",
  "params": {
    "name": "Alex"
  }
}
```

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "contacts_lookup",
  "query": "Alex",
  "match_count": 1,
  "matches": [
    {
      "name": "Alex Example",
      "phone_last10": "4155551234"
    }
  ]
}
```

Searches Contacts.app by name. Returns up to 25 matches. Each match contains either `phone_last10` (last 10 digits of phone number) or `email` (email address), depending on the contact's identifier. Useful for disambiguating before `chat_history` or `send`.

---

### `list_chats` — Enumerate threads without content (manager only)

Available only on a `manager` bridge (see Bridge Roles). Lists the chats that
had activity inside a window so a management tool can show the user which
threads exist and let them build an allowlist or blocklist. The read policy is
deliberately not applied — the point is to see the threads a policy will be
written about — and the action **never selects `message.text` or
`message.attributedBody`**. A fixture database with sentinel bodies asserts that
no body reaches the response.

**Request:**
```json
{
  "id": "abc123",
  "action": "list_chats",
  "params": {
    "days": 365,
    "limit": 200,
    "include_groups": true,
    "query": "family"
  }
}
```

| Param | Type | Default | Bounds |
| --- | --- | --- | --- |
| `days` | number | `365` | `(0, 3650]` — its own bound, distinct from the 90-day cap on `review`/`search`/`chat_history` |
| `limit` | integer | `200` | `(0, 500]` |
| `include_groups` | boolean | `true` | must be a JSON boolean |
| `query` | string | none | ≤ 100 chars; case-insensitive substring match on `label`, `display_name`, `chat_id`, and participant handles; blank means no filter |

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "list_chats",
  "window_days": 365,
  "chat_count": 2,
  "truncated": false,
  "chats": [
    {
      "chat_id": "chat100200300",
      "kind": "group",
      "display_name": "Family",
      "label": "Family",
      "participants": ["+14155551234", "+14155559876", "bob@example.com"],
      "participant_count": 3,
      "service": "iMessage",
      "message_count": 41,
      "last_activity_date": "2026-08-14"
    },
    {
      "chat_id": "+14155551234",
      "kind": "direct",
      "display_name": "",
      "label": "Alice Example",
      "participants": ["+14155551234"],
      "participant_count": 1,
      "service": "iMessage",
      "message_count": 12,
      "last_activity_date": "2026-08-13"
    }
  ]
}
```

- Ordered by most recent activity first. `truncated` is `true` when more chats
  matched than `limit`.
- `kind` comes from `chat.style` (`43` group, `45` direct); the
  `chat…` identifier heuristic is the fallback.
- `label` is the group name, or a participant-derived label for unnamed
  groups, or the contact name for a direct chat; it falls back to `chat_id`.
- `participants` holds at most 10 handles; `participant_count` is the full
  count.
- `last_activity_date` is an ISO **day**, not a timestamp.

---

### `send_preview` — Dry-run a send (validation only)

**Request:**
```json
{
  "id": "abc123",
  "action": "send_preview",
  "params": {
    "to": "+14155551234",
    "text": "Confirmed for 3pm.",
    "service": "iMessage"
  }
}
```

`service` can be `"iMessage"`, `"SMS"`, or omitted (defaults to `iMessage`).

To attach one image, add an `image` object. `text` is then an optional caption
and may be omitted or `""`. Without `image`, `text` is still required.

```json
{
  "id": "abc123",
  "action": "send_preview",
  "params": {
    "to": "+14155551234",
    "text": "See you at 3.",
    "service": "iMessage",
    "image": {"path": "/Users/matthew/Pictures/photo.jpg"}
  }
}
```

`image.path` must be an absolute path to a regular file the helper can open.
No path component may be a symlink (on macOS, pass `/private/tmp` rather than
`/tmp`). The file must be a real image of type png, jpeg, gif, webp, heic, or
heif: the extension and the magic bytes must agree. Maximum size is 25 MiB
(26214400 bytes). Empty, missing, unreadable, and non-image files are rejected
before a nonce is minted. `service` may be `iMessage` or `SMS`; an SMS image
is handed to Messages as an MMS attachment and only delivers when that Mac's
text-message relay supports it.

**Response:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "send_preview",
  "preview": {
    "to": "+14155551234",
    "resolved_name": "Alex",
    "service": "iMessage",
    "text": "Confirmed for 3pm.",
    "text_length": 18,
    "blocked": false
  },
  "send_nonce": "Zk9...short-opaque-string",
  "send_nonce_ttl_seconds": 60
}
```

When an image was requested, `preview` also includes:

```json
"image": {
  "path": "/Users/matthew/Pictures/photo.jpg",
  "filename": "photo.jpg",
  "mime": "image/jpeg",
  "size": 24576,
  "sha256": "0123456789abcdef..."
}
```

**Critical:** The helper returns a `send_nonce` that **must** be echoed back in the subsequent `send` request. For text-only sends the nonce is bound to the exact `(to, text, service)` triple. When an image is present it is also bound to that image's path, filename, mime, size, and sha256. The nonce expires after `send_nonce_ttl_seconds` (default 60s). This enforces the preview-then-confirm gate at the helper level.

`send_preview` does **not** read `chat.db` and does **not** send anything. It validates the recipient, the body, and any image file, resolves the contact name, and checks the blocklist.

---

### `send` — Actually send the message

**Request:**
```json
{
  "id": "abc123",
  "action": "send",
  "params": {
    "to": "+14155551234",
    "text": "Confirmed for 3pm.",
    "service": "iMessage",
    "send_nonce": "Zk9...short-opaque-string"
  }
}
```

The `send_nonce` is the one returned by the preceding `send_preview`. The `to`, `text`, and `service` **must** match the preview exactly. An image send must also echo the preview's `image` object (`path`, `filename`, `mime`, `size`, and `sha256`). If any of these differ, or if the file's bytes no longer match that identity, the helper rejects the request. A payload mismatch raises `"send payload differs from preview"` and burns the nonce. A file that no longer matches the echoed identity is rejected before the nonce is consumed.

Image send request:

```json
{
  "id": "abc123",
  "action": "send",
  "params": {
    "to": "+14155551234",
    "text": "See you at 3.",
    "service": "iMessage",
    "send_nonce": "Zk9...short-opaque-string",
    "image": {
      "path": "/Users/matthew/Pictures/photo.jpg",
      "filename": "photo.jpg",
      "mime": "image/jpeg",
      "size": 24576,
      "sha256": "0123456789abcdef..."
    }
  }
}
```

**Native macOS confirmation dialog.** After nonce validation succeeds, the helper displays the resolved name, exact recipient address, service, and full message text in a scrollable read-only view. An image send changes the dialog title to say **Image Send** and adds the attachment filename, MIME type, size, and path, plus the caption (or `(no caption)`). Cancel is the keyboard default. The user must deliberately select **Send**; cancelling or waiting 60 seconds aborts the send.

The confirm helper binary must be rebuilt from `bin/confirm_imessage_send.m` before image sends will proceed. The Python helper refuses an image send when that binary does not contain `IMESSAGE_CONFIRM_IMAGE_V1`, so an old dialog cannot approve an attachment it does not display. Text-only sends still work with a current confirm helper. Rebuilding only the confirm helper does not change the FDA wrapper's CDHash. Re-running `install.sh` also rebuilds the wrapper, which can require granting Full Disk Access again.

**Response on success:**
```json
{
  "id": "abc123",
  "ok": true,
  "action": "send",
  "sent": {
    "to": "+14155551234",
    "resolved_name": "Alex",
    "service": "iMessage",
    "text_length": 18,
    "sent_at": "2026-08-12T00:15:42"
  }
}
```

An image send adds `sent.image` with the same identity object returned by `send_preview`.

**Response on error:**
```json
{
  "id": "abc123",
  "ok": false,
  "error": "send gate: missing nonce; call send_preview first"
}
```

Or:
```json
{
  "id": "abc123",
  "ok": false,
  "error": "send cancelled by user or timed out (60s dialog limit)"
}
```

Text-only sends embed `text` in the AppleScript passed to `/usr/bin/osascript`. They do not write the body to a file.

Image sends are different because Messages has to read a file. After the user confirms, the helper copies the bytes it already fingerprinted into a mode-`0700` temporary directory, hashes that copy, and passes only that staged path to AppleScript as `POSIX file`. The staged file is removed when `osascript` returns, including on failure. A caption is a second `send` in the same script, after the image. Messages' `send` command accepts text or a file, not both in one call. If the image `send` succeeds and the caption `send` fails, the image may already have been delivered.

**Send-gate validation (enforced helper-side):**

- The `send_nonce` must be present, fresh (within TTL), and match the payload.
- Nonces are single-use: consumed on first `send` attempt, deleted on any validation failure.
- Replaying a used nonce, sending without a nonce, or changing the payload after preview all result in rejection before the confirmation dialog appears.
- After nonce validation, the user must approve via the native macOS dialog.
- Text-only bodies must be 1–4000 chars with no C0 control bytes other than `\n`, `\r`, `\t`.
- With an image, `text` may be omitted or empty (caption). A non-empty caption has the same 4000-character rules.
- One image per send. Supported types: png, jpeg (`.jpg` / `.jpeg`), gif, webp, heic, heif. Max 25 MiB.
- Recipient must not be on `contacts/blocked_chats.txt`.
- **Group chat IDs are NOT supported as send targets.** Attempting to send to a `chatNNNNN` identifier will fail. Use individual phone numbers or email addresses only. Images go to that same individual recipient, not to a group.

---

## Redaction

Before returning any response that includes message text, the helper runs a regex-based redactor that masks:

- 2FA / verification codes (near keywords like `code`, `verification`, `OTP`, `passcode`)
- Credit-card-like digit runs (13–19 digits)
- US SSN patterns (`NNN-NN-NNNN`)

Redacted content is replaced with `[REDACTED-2FA]`, `[REDACTED-CARD]`, or `[REDACTED-SSN]` depending on the pattern matched.

**Known gaps:**
- Dot-separated credit cards (`4111.1111.1111.1111`)
- PIN-labelled codes (`Your PIN is 4829`)
- Slash-separated SSNs (`123/45/6789`)
- Bare codes with no keyword (`839201 to confirm`)
- API keys (Stripe, GitHub, OpenAI tokens)
- Bank account / routing numbers
- Home addresses
- Dates of birth

The thread-level read policy (see below) is the reliable filter; redaction is a second line of defense.

---

## Read Policy

`contacts/blocked_chats.txt` is checked **before** the redactor runs. Threads on the blocklist are dropped entirely—their text never enters the response JSON.

Format: one entry per line. Lines starting with `#` are ignored.

**Matches:**
- **Phone numbers:** last 10 digits compared (e.g., `+1-555-123-4567`, `5551234567`, `(555) 123-4567` all match)
- **Email addresses:** exact case-insensitive match
- **Group chat IDs:** exact case-insensitive match (not substring — `chat123` will not match `chat1234567890`)

**Example:**
```
# Therapist
+15551234567

# Attorney
lawyer@example.com

# Family group chat
chat123456789
```

Blocked threads are enforced for **both** inbound (read actions) and outbound (`send` actions).

When policy mode is `allowlist`, reads and contact lookup return only handles in
the allowlist; the blocklist still takes precedence. Hardened installs enforce
this mode with a root-owned list managed by `configure_allowlist.py`.

---

## Typical Request Flow (Pseudocode)

```python
import json, uuid, time, pathlib, os

bridge = pathlib.Path("<bridge-folder>")
req_id = uuid.uuid4().hex[:12]

# CRITICAL: Write to temp file first, then rename atomically.
# Direct write to request-*.json can cause launchd to fire mid-write.
tmp = bridge / "control" / "requests" / f".request-{req_id}.json.tmp"
final = bridge / "control" / "requests" / f"request-{req_id}.json"

tmp.write_text(
    json.dumps({"id": req_id, "action": "review", "params": {"days": 2}})
)
# Atomic rename publishes the complete request.
os.rename(tmp, final)

# Poll for response
resp_path = bridge / "control" / "responses" / f"response-{req_id}.json"
for _ in range(30):  # 15-second timeout
    if resp_path.exists():
        break
    time.sleep(0.5)

# Read response
data = json.loads(resp_path.read_text())
resp_path.unlink(missing_ok=True)
if data["ok"]:
    print(data)
else:
    print(f"Error: {data['error']}")
```

**Why atomic writes matter:** The helper watches `control/requests/` via launchd WatchPaths. If you write directly to `request-*.json`, launchd can fire before the write completes, causing JSON parse errors. Always use temp file + rename.

---

## Sending Flow (Preview → Confirm → Send)

**Recommended workflow:**

1. **Resolve the recipient.** If the user provided a name, call `contacts_lookup` first. If multiple matches, surface them and ask. **Note:** Group chat IDs (like `chat123456789`) will be rejected at the preview stage—only individual phone numbers or email addresses work for sending.
2. **Issue `send_preview`.** Show the user:
   - Resolved recipient name
   - Service (iMessage / SMS)
   - Full text and `text_length`
   - For an image: filename, MIME type, size, sha256, and path from `preview.image`
   - Whether `blocked: true` (if so, stop—don't prompt for approval)
   - If preview fails with "group chat IDs not supported", the recipient is a group—use an individual contact instead.
3. **Wait for explicit user approval in the AI chat.** Do not proceed without confirmation.
4. **Issue `send` with the `send_nonce` from step 2.** The `to`, `text`, and `service` must match the preview exactly. For an image, also echo the preview `image` object unchanged.
5. **The helper will display a native macOS dialog** showing the recipient, service, and message preview. Image sends also show the filename, type, size, and path. The user must click **Send** in this system dialog to complete the send (60-second timeout).
6. **If the AI chat approval takes >60s,** re-run `send_preview` to mint a fresh nonce. Do this as well if the image file changed after preview.
7. **Surface `sent.sent_at`, resolved name, and any `sent.image` identity** as confirmation.

---

## Permissions Required

### Full Disk Access (FDA)

Required to read `~/Library/Messages/chat.db`. The FDA grant is attached to the C wrapper's CDHash, not its path. Changing the source or compiler can produce a different CDHash, requiring re-grant.

**Grant location:**
```
System Settings → Privacy & Security → Full Disk Access
→ Add the exact wrapper path printed by the selected installer
```

### Updating an existing Mac install

Image send ships in this fork's source. It is not applied to a live helper
until that Mac is updated. For `/Users/matthew/imessage-bridge-grok` (or any
other checkout), install the new `bin/helper.py`, `bin/send_gate.py`, and
`bin/confirm_imessage_send.m`, then rebuild **only** the confirm helper:

```bash
clang -Wall -Wextra -Werror -fobjc-arc \
  -framework AppKit -framework Foundation \
  -o bin/grokbot-imessage-confirm bin/confirm_imessage_send.m
codesign --force --sign - --options runtime bin/grokbot-imessage-confirm
```

The FDA wrapper source did not change. Re-running `install.sh` also rebuilds
that wrapper and can change its CDHash, which means granting Full Disk Access
again. Prefer the confirm-helper rebuild above on an install that already has
FDA.

### Automation → Messages

Required to send. First send triggers a one-time prompt: *"grokbot-imessage-helper wants to control Messages."* Click **OK**.

**Grant location:**
```text
System Settings → Privacy & Security → Automation
→ grokbot-imessage-helper → Messages (toggle on)
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Requests pile up in `control/requests/`, no responses | FDA not granted yet | Grant FDA to the wrapper binary (path printed by `install.sh`) |
| `sqlite3.OperationalError: unable to open database file` in `log.txt` | FDA not granted, or grant stale | Re-add the wrapper in System Settings → Full Disk Access |
| First send fails with Automation prompt | macOS needs Automation permission | Click **OK** on the prompt; future sends will work |
| `send gate: missing nonce` | `send` called without prior `send_preview` | Always call `send_preview` first and pass the returned nonce |
| `send payload differs from preview` | Body, recipient, service, or image identity changed after preview | Re-run `send_preview` to mint a fresh nonce for the new payload |
| `image file does not match the previewed attachment` | The file's bytes, type, or size changed after preview | Re-run `send_preview` |
| `confirmation helper does not support image sends` | `grokbot-imessage-confirm` was not rebuilt | Rebuild the confirm helper; see below |
| Messages decode as empty | `attributedBody` parser failed | Check `control/log.txt` for a byte-count-only parser diagnostic |

---

## What the Helper Won't Do

- **No inbound attachment reading, stickers, audio, or Tapback reactions.** Outbound sends may include one image file to an individual recipient. Text-only sends are otherwise unchanged.
- **No message editing or deletion.** Once sent, a message is immutable from the helper's perspective.
- **No message effects** (balloons, confetti, invisible ink).
- **No group-chat sending.** Group chat IDs can be read (`review`, `chat_history`), but **cannot be used as send targets**. Sending requires individual phone numbers or email addresses.
- **No group-chat creation.** Cannot create new group chats.
- **Only reads local `chat.db`.** If a thread hasn't synced to this Mac, it won't appear in search/review.

---

## Files and Directories

| Path | Role |
|------|------|
| `control/requests/` | AI host writes request JSON here. Watched by launchd. |
| `control/responses/` | Helper writes mode-600 response JSON here. AI host reads and immediately deletes; helper reaps files older than one hour. |
| `control/log.txt` | Internal helper diagnostics. Launchd stdout/stderr go to `/dev/null` so they cannot follow a user-controlled log symlink. |
| `contacts/blocked_chats.txt` | User-maintained blocklist of sensitive chats. |
| `contacts/read_policy.txt` | Standard-mode read policy selector. Hardened mode overrides it. |
| `contacts/allowed_chats.txt` | Standard-mode optional allowlist. |
| `<code-root>/bin/grokbot-imessage-helper` | Locally signed C wrapper (the FDA target). |
| `<code-root>/bin/helper.py` | Python worker (reads chat.db, resolves contacts, redacts, drives osascript). |
| `<code-root>/bin/send_gate.py` | Nonce minting and validation for send-preview/send gate. |
| `nonces/` | Short-lived per-nonce files bound to previewed sends. TTL-reaped on every helper run. |

---

## Security Notes

- The bridge folder should be mode `700` (user-only access).
- Bridge runtime directories must be real, current-user-owned directories with
  no group/world permissions. The helper rejects symlinks and unsafe modes; it
  does not chmod existing objects.
- Requests must be current-user-owned regular files no larger than 64 KiB.
- The helper runs with **Full Disk Access**—a bug or compromise becomes a full-user-file-read primitive.
- Nonces are single-use and expire after 60s. A process that can write to the bridge can request readable content; hardened mode bounds that content to its root-owned allowlist. It cannot silently send without native user approval.
- See `SECURITY.md` for full threat-model details.

---

## License

This protocol is part of the `imessage-review` project, licensed under MIT.
