# Local Trigger

This is a local deterministic daemon for the `yeuei/gpt---github---codex`
handoff repository. It polls a configured remote branch, reads only explicit
coordination trailers, and writes its audit trail to local SQLite—not Git.

## Safety and routing

- Global pause and each direction remain separate dashboard toggles. The dashboard also
  has one explicit **自动审批模式** button that couples the approval gate and submit behavior.
- **Approval mode is the default.** A detected event enters `awaiting approval`;
  select **Approve this event** before the daemon touches ChatGPT or launches
  the configured Agent command.
- When **自动审批模式** is enabled, `approval_required=false` and
  `auto_submit=true`: new events are routed immediately, and existing
  `awaiting approval` events are drained once. The user is intentionally
  choosing unattended external actions by pressing this button.
- Turning the mode off restores per-event approval and fill-only browser
  behavior (`approval_required=true`, `auto_submit=false`). Events already
  marked `dispatched` are never resent; `needs human` failures are not
  automatically retried.
- `agent → ChatGPT Web` uses the `open-browser-use` CLI with a fixed,
  configured conversation URL. The default is fill-only; submitting is opt-in.
- `ChatGPT Web → agent` starts only the user-configured local command. An empty
  command causes a visible `needs human` event rather than a surprise process.
- The first poll establishes a baseline; it never replays historical commits.
- Every event is deduplicated by `Coordination-Event-Id`, or by commit SHA when
  a legacy commit lacks that trailer.

## Install and run

```bash
cd trigger
cp config.example.json config.local.json
# Set conversation_url; leave agent.command empty until ready.
python3 trigger.py
```

Open <http://127.0.0.1:8765>. The service listens only on loopback.

The dashboard shows the last OBU health result and includes **检测浏览器连接**.
The daemon keeps one long-lived, configuration-stable OBU broker session and periodically pings it;
when a stale `active.json` points to a deleted socket, it removes only that
registry entry and retries. A disconnected state is surfaced as `无法连接`
with the exact CLI error so the user can reconnect the selected Chrome profile.

Before enabling the browser direction, the user must manually run
`open-browser-use setup` and enable the Chrome extension in the chosen profile.
The daemon validates the configured profile with `open-browser-use ping`; it
does not choose another profile or inspect browser credentials.

## Event contract

An event-bearing commit ends with:

```text
Coordination-Origin: agent | chatgpt
Coordination-Event-Id: stable-event-id
Coordination-Caused-By: parent-event-id   # optional
```

The event payload is only a wake-up. Both parties must re-read GitHub HEAD and
the relevant coordination files before acting. A `needs human` status captures
missing browser setup, a missing conversation URL, or an unconfigured agent
command without retrying in a loop.
