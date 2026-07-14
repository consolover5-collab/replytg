# replytg

LLM reply suggestions sidecar for [telegram-business-bridge](https://github.com/AndyShaman/telegram-business-bridge):
a standalone daemon that watches incoming private messages (already collected by the
bridge), generates a few reply options in your personal style (`REPLYTG_VARIANT_COUNT`,
2 by default), and sends a card with buttons to a separate Telegram bot. The approved
option is delivered to your contact **as you**, through the bridge's standard draft
mechanism.

[Русский README](README.ru.md)

```
┌─ @your_replytg_bot ─────────────┐
│ 💬 Masha: "Free tomorrow?"      │
│                                 │
│ 1️⃣ hey) yep, after lunch works  │
│ 2️⃣ can't promise, I'll text you │
│                                 │
│ [1️⃣] [2️⃣] [🔄 More] [✍️ Own] [❌]│
└─────────────────────────────────┘
```

## How it works

- An incoming message opens a "wave": incoming messages accumulate for 10 minutes (configurable).
- If you replied yourself during that window, the LLM is never called.
- Otherwise — one generation: one card with `REPLYTG_VARIANT_COUNT` options (2 by
  default) in your control bot.
- Used a suggestion (a numbered button or ✍️) — one hour of silence for that chat.
- Didn't use it and didn't reply — the same card is re-sent after
  `REPLYTG_REPEAT_AFTER_SEC` (2 hours by default), up to `REPLYTG_REPEAT_MAX_COUNT`
  times (1 by default; `0` turns reminders off). Reminders reuse the last generated
  options and don't call the LLM again.
- A new message from the contact starts a fresh wave.
- **There is no auto-send and never will be**: every outgoing text requires your button
  press, and options are always shown in full in the card.
- The first run listens to future messages only — the bridge's accumulated history is
  never replayed.
- Delivery status arrives as a reply to the card ("✅ Sent" / "⚠️ ...").

| Setting | Default | Purpose |
|---|---:|---|
| `REPLYTG_VARIANT_COUNT` | `2` | Reply options shown in the card |
| `REPLYTG_REPEAT_AFTER_SEC` | `7200` | Interval between reminders |
| `REPLYTG_REPEAT_MAX_COUNT` | `1` | Max reminder resends; `0` disables |

Changing `REPLYTG_REPEAT_MAX_COUNT` only takes effect for cards created afterwards — a
reminder already scheduled for an existing card can still arrive on its old schedule.

Exactly two integration points with the bridge:

| What | How |
|---|---|
| Reading incoming/outgoing | `bridge.db` read-only (SQLite WAL) |
| Sending the approved reply | insert a draft with `status='approved'` — the bridge does the rest |

replytg never sees the business bot's token.

## Requirements

- A running [telegram-business-bridge](https://github.com/AndyShaman/telegram-business-bridge)
  (a version with the `drafts` table and `approved` status; checked automatically at startup).
- Python ≥ 3.12, [uv](https://docs.astral.sh/uv/).
- Any OpenAI-compatible LLM API (endpoint + key + model).

## Setup

```bash
git clone https://github.com/consolover5-collab/replytg ~/projects/replytg
cd ~/projects/replytg       # the path matters: the systemd unit expects it
uv sync
install -m 600 .env.example .env   # fill in: bot token, your user id, paths, LLM
cp deploy/replytg.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now replytg
```

Cloning elsewhere? Adjust `WorkingDirectory`/`EnvironmentFile` in the unit.

Create the control bot via [@BotFather](https://t.me/BotFather) (a regular bot — NOT the
one connected to Telegram Business). Send it `/start` after launch.

At startup the daemon verifies the bridge.db schema and that there is exactly one
business connection owned by `REPLYTG_OWNER_ID`. Any mismatch aborts startup with a
clear error.

## Style profile

To make suggestions sound like you rather than a polite robot, put a description of
your writing style + examples into `data/style-profile.md` (template:
`docs/style-profile.example.md`). Two ways:

1. **Auto-build**: `uv run replytg-style` — builds the profile from
   "incoming → your reply" pairs accumulated in bridge.db (needs ≥10 pairs; replies
   sent by replytg itself are excluded so the model doesn't train on itself).
2. **Retrospective**: official Telegram Desktop export (Settings → Advanced →
   Export data) and build the profile from it however you like.

The daemon works without a profile, but suggestions will be generic.

## Privacy

- Your conversations leave the machine via two paths: requests to the LLM API you
  chose (chat history + new incoming messages in the prompt — pick your provider
  consciously) and the cards in your control bot (message fragments and reply options
  pass through Telegram's servers like any bot message).
- Invalid LLM responses are logged by error type only, never by content.
- `data/` (style profile, state) is chmod 0700 with built-in protection against
  ending up in git (the `.gitignore` entry covers the whole `data/` directory, not a
  `data/*` glob).
- Message content is treated as untrusted: instructions inside conversations are
  ignored by the LLM, and nothing can be sent without your button press — by design.
- Delivery is best-effort: if the process dies in the narrow window between your
  button press and the draft insert, the confirmed reply may not go out (the card
  stays without "✅" — that's your signal to check the chat). No persistent send
  queue in v0.1.

## Configuration

Everything lives in `.env` (see `.env.example`): wave window, silence duration,
repeat interval and max repeat count, variant count and length limit, chat blocklist,
LLM model/endpoint.

## License

MIT
