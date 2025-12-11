# Architecture Overview

## How Telegram Bot Communication Works

### Polling (Current Implementation)

The bot uses **polling** to receive messages from Telegram:

```
┌─────────────────┐                    ┌──────────────┐
│  Docker Bot     │  HTTP GET requests │  Telegram    │
│  (Your Server)  │ ──────────────────>│  API Server  │
│                 │  "Any new messages?"│              │
│                 │ <────────────────── │              │
│                 │  Returns messages  │              │
└─────────────────┘                    └──────────────┘
```

**Key Points:**
- ✅ **Bot initiates connections** - Makes outbound HTTP requests to `api.telegram.org`
- ✅ **No inbound connections** - Telegram never connects to your bot
- ✅ **No exposed ports needed** - Bot only makes outbound requests
- ✅ **Works behind NAT/firewall** - No port forwarding required

### Polling vs Webhooks

| Method | Direction | Ports Needed | Use Case |
|--------|-----------|--------------|----------|
| **Polling** (current) | Bot → Telegram | None (outbound only) | Simple, works everywhere |
| **Webhooks** | Telegram → Bot | Yes (inbound 443/80) | High volume, needs HTTPS |

### Why No WEBAPP_URL?

The `WEBAPP_URL` was only needed for **Telegram Web Apps** (mini-apps that open in Telegram's in-app browser). Since we removed the webapp feature and use in-chat buttons instead, no URL is needed.

### Current Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Docker Container (Ubuntu Server)                       │
│  ┌───────────────────────────────────────────────────┐  │
│  │  bot.py                                          │  │
│  │  - Connects to Telegram API (outbound)          │  │
│  │  - Connects to Ombi API (internal network)      │  │
│  │  - Uses polling to receive messages             │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
         │                    │
         │                    │
    ┌────▼────┐         ┌────▼────┐
    │Telegram │         │  Ombi   │
    │   API   │         │  (LAN)  │
    └─────────┘         └─────────┘

┌─────────────────────────────────────────────────────────┐
│  AWS Lambda (Separate Service)                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │  webhook_handler.py                               │  │
│  │  - Receives webhooks from Ombi                    │  │
│  │  - Sends notifications to Telegram                │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
         │                    │
         │                    │
    ┌────▼────┐         ┌────▼────┐
    │  Ombi   │         │Telegram │
    │ Webhook │         │   API   │
    └─────────┘         └─────────┘
```

### Network Requirements

**Bot Container:**
- ✅ Outbound HTTPS to `api.telegram.org` (port 443)
- ✅ Outbound HTTP/HTTPS to Ombi (internal network)
- ❌ No inbound ports needed
- ❌ No public IP needed
- ❌ No reverse proxy needed

**Lambda Function:**
- ✅ Receives webhooks from Ombi (via API Gateway)
- ✅ Outbound HTTPS to `api.telegram.org` (port 443)
- ✅ Public endpoint (API Gateway) for Ombi to send webhooks

### Summary

- **Bot uses polling**: Makes outbound requests to Telegram, no inbound needed
- **No WEBAPP_URL**: Only needed for Telegram Web Apps (which we don't use)
- **No exposed ports**: Bot only makes outbound connections
- **Works in Docker**: No special networking configuration needed
- **Webhook separate**: Handled by AWS Lambda, not the bot container

