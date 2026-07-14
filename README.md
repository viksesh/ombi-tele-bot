# Telegram Ombi Bot

A Telegram bot that allows users to request movies and TV shows through Ombi with an intuitive chat-based interface featuring posters, descriptions, and easy navigation.

## Features

- 🎬 **Request Movies** - Search and request movies with poster images and descriptions
- 📺 **Request TV Shows** - Search and request TV shows with poster images and descriptions
- 🖼️ **Visual Results** - Browse results with posters, ratings, and descriptions
- 🔄 **Easy Navigation** - Scroll through multiple results with Previous/Next buttons
- 💬 **Chat-Based** - Everything stays within Telegram chat (no external apps)
- 🔗 **IMDb Support** - Search using IMDb links or titles
- ✨ **Telegram Mini App** - Optional in-Telegram web UI with instant search, posters, status badges, and one-tap requests (messaging flow still fully supported)
- 🔔 **Ombi Notifications** - Receive notifications when requests are approved/denied (via AWS Lambda webhook)
- Dockerized for easy deployment on Ubuntu servers

## Prerequisites

- Docker installed on your Ubuntu server
- Ombi instance running and accessible
- Telegram Bot Token (obtain from [@BotFather](https://t.me/botfather))
- Ombi API Key (obtain from your Ombi settings)

## Environment Variables

The following environment variables are required:

- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token
- `OMBI_URL` - Your Ombi instance URL (e.g., `http://ombi:3579` or `https://ombi.example.com`)
  - **If running in Docker with Organizr**: Use the Docker service name (e.g., `http://ombi:3579`)
  - **If accessible via Organizr reverse proxy**: Use the full URL (e.g., `https://ombi.yourdomain.com` or `https://yourdomain.com/ombi`)
  - **If running locally**: Use `http://localhost:3579`
- `OMBI_API_KEY` - Your Ombi API key
- `OMBI_REQUEST_USER` - (Optional) Ombi username to make requests on behalf of (e.g., `requests`). If not set, requests will be made using the API key only.
- `LOG_LEVEL` - (Optional) Logging level: `DEBUG`, `INFO` (default), `WARNING`, or `ERROR`. Set to `WARNING` or `ERROR` to reduce log volume and storage usage.
- `LOG_FILE` - (Optional) Path to log file. If set, logs will be written to file with rotation (10MB max, 5 backups). Default: logs to console only (Docker captures these).
- `ENABLE_GROUP_AUTH` - (Optional) Feature flag to restrict bot usage to members of authorized Telegram group(s). Set to `true`, `1`, or `yes` to enable. When enabled, only users who are members of at least one of the groups specified by `AUTHORIZED_GROUP_CHAT_ID` or `AUTHORIZED_GROUP_CHAT_IDS` can use the bot. Default: disabled (all users can use the bot).
- `AUTHORIZED_GROUP_CHAT_ID` or `AUTHORIZED_GROUP_CHAT_IDS` - (Optional) Telegram group chat ID(s) where authorized users must be members. Required if `ENABLE_GROUP_AUTH` is enabled. Can be a single group chat ID or a comma-separated list of group chat IDs. The bot must be added to all specified groups. See below for instructions on finding group chat IDs.
  - **Single group**: `AUTHORIZED_GROUP_CHAT_ID=-1001234567890`
  - **Multiple groups**: `AUTHORIZED_GROUP_CHAT_IDS=-1001234567890,-1009876543210,-1001112223334`
  - Both environment variable names are supported for backward compatibility
- `WEBAPP_URL` - (Optional) Public HTTPS URL of the mini app (e.g., `https://requests.yourdomain.com`). When set, the bot starts the mini app web server, shows an "✨ Open Mini App" button in the main menu, and sets the chat menu button to open the app. Telegram requires HTTPS, so front the web server with a reverse proxy. Leave unset to run messaging-only.
- `WEBAPP_PORT` - (Optional) Port the mini app web server listens on inside the container. Default: `8080`.
- `WEBAPP_INIT_DATA_MAX_AGE` - (Optional) Max age in seconds of Telegram WebApp auth data before it's rejected. Default: `86400` (24h).
- `MAX_REQUESTS_PER_DAY` - (Optional) Maximum number of successful requests a single user may submit per day (resets at UTC midnight). `0` (default) disables the limit. Counts are held in-memory and reset on restart.

### Telegram Mini App

The mini app is an optional web UI that opens inside Telegram and shares all business logic with the messaging flow (same search pipeline, IMDb link resolution, availability statuses, group authorization, and auto-approve rules).

Setup:

1. Set `WEBAPP_URL` to a public HTTPS URL that proxies to the bot container's `WEBAPP_PORT` (8080 by default). Any reverse proxy works (nginx, Caddy, Organizr, Cloudflare Tunnel).
2. Restart the bot. It will serve the mini app and register the chat menu button automatically.
3. (Optional) In [@BotFather](https://t.me/botfather), you can also configure the menu button / Main Mini App for nicer presentation.

Security: every API call from the mini app is authenticated by validating Telegram's signed `initData` against the bot token (HMAC), and the same group-membership rules as the chat bot are enforced.

#### Reverse proxy requirements (important)

Telegram's webview is strict about how the mini app is served. If it opens to a **blank screen**, the cause is almost always the reverse proxy, not the bot:

- **Valid CA certificate required.** Telegram refuses to load a mini app served with a self-signed certificate (the page just stays blank). Use Let's Encrypt / a trusted CA — not nginx's default snakeoil cert. If the domain isn't configured in nginx at all, requests fall through to the default server (self-signed cert + `403`), which looks the same.
- **No SSO in front of it.** Don't put the mini app behind Organizr / Authelia / any `auth_request` SSO. Telegram's webview carries no SSO session cookie, so it gets blocked. The mini app authenticates itself via Telegram's signed `initData`.

Example nginx vhost proxying `WEBAPP_URL` to the container's `WEBAPP_PORT` (8095 in the provided `docker-compose.yml`):

```nginx
server {
    listen 443 ssl;
    server_name requests.example.com;

    ssl_certificate     /etc/letsencrypt/live/requests.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/requests.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8095;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

To issue the certificate, start with an **HTTP-only** version of this block (`listen 80;`, no
`ssl_*` lines), then run `certbot --nginx -d requests.example.com` — certbot obtains the cert
and rewrites the block to add the `listen 443 ssl;` + `ssl_certificate*` lines and an
HTTP→HTTPS redirect. (Adding the `ssl_certificate` lines *before* the cert exists makes
`nginx -t` fail with `cannot load certificate ... No such file or directory`, which also
blocks certbot.)

Verify with `curl -s -o /dev/null -w "%{http_code}\n" https://requests.example.com/` — it
should return `200` with a valid cert (no `-k` needed) before the mini app will load in Telegram.

### Group Authorization Feature

When `ENABLE_GROUP_AUTH` is enabled, the bot will verify that users are members of at least one of the authorized groups before allowing them to:
- Start the bot (`/start` command)
- Search for movies/TV shows
- Submit requests

**How to find your group chat ID:**

1. **Method 1: Using @userinfobot**
   - Add `@userinfobot` to your Telegram group
   - The bot will send a message with the group's chat ID

2. **Method 2: Using the bot itself**
   - Add your bot to the group
   - Check the bot logs - when the bot receives a message from the group, it will log the chat ID
   - Look for log entries containing the chat ID

3. **Method 3: Using Telegram API**
   - Forward a message from the group to `@userinfobot` or use a Telegram client that shows chat IDs
   - The chat ID will be a negative number (e.g., `-1001234567890`)

**Important notes:**
- The bot must be added to all authorized group chats
- Users must be active members of at least one authorized group (not left or kicked)
- Users who are members of ANY of the authorized groups will be granted access
- If the bot is not in a group or a group chat ID is incorrect, that group will be skipped and other groups will still be checked
- The authorization check happens on every interaction, so users who leave all authorized groups will immediately lose access

### Notification Webhook (AWS Lambda)

Webhook notifications are handled by a separate AWS Lambda function. See the [`lambda/`](lambda/) directory for deployment instructions.

The Lambda function handles:
- ✅ **Request Approved** → Sends message to Telegram group thread
- ✅ **Request Denied** → Sends message to Telegram group thread

## Docker Deployment

### Build the Docker image

```bash
docker build -t ombi-tele-bot .
```

### Run the container

```bash
docker run -d \
  --name ombi-tele-bot \
  -e TELEGRAM_BOT_TOKEN="your_telegram_bot_token" \
  -e OMBI_URL="http://ombi:3579" \
  -e OMBI_API_KEY="your_ombi_api_key" \
  ombi-tele-bot
```

**Note**: No port mapping needed - the bot uses Telegram polling. Webhook notifications are handled by AWS Lambda (see [`lambda/`](lambda/) directory).

### Using Docker Compose

A `docker-compose.yml` file is included in the repository. Update it with your environment variables:

```bash
# Copy and edit environment variables
cp .env.example .env
# Edit .env with your values

# Start the container
docker-compose up -d

# View logs
docker-compose logs -f ombi-tele-bot
```

**Note**: No ports need to be exposed - the bot uses Telegram polling and doesn't require any incoming connections.

## Local Development

### Quick Start (Mac/Linux)

1. **Install dependencies:**

```bash
pip install -r requirements.txt
```

Or use a virtual environment (recommended):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. **Create a `.env` file** in the project root:

```bash
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OMBI_URL=http://localhost:3579
OMBI_API_KEY=your_ombi_api_key
```

4. **Run the bot:**

**Option A: Using the helper script**
```bash
./run_local.sh
```

**Option B: Manual start**
```bash
python bot.py
```

See [TESTING.md](TESTING.md) for detailed local testing instructions.


## Usage

### Requesting Content

1. Start a chat with your bot on Telegram
2. Send `/start` to see the main menu
3. Choose "🎬 Request Movie" or "📺 Request TV Show"
4. Enter the title or paste an IMDb link (e.g., `The Matrix` or `https://www.imdb.com/title/tt0133093/`)
5. Browse results with posters, descriptions, and ratings
6. Use "Next ▶️" / "◀️ Previous" to navigate through results
7. Click "✅ Request" to submit your request
8. Click "❌ Cancel" to return to the main menu

### Setting Up Notification Webhooks

Webhook notifications are handled by a separate AWS Lambda function. See the [`lambda/`](lambda/) directory for complete setup instructions.

**Quick Setup:**
1. Deploy the Lambda function (see `lambda/README.md`)
2. Get your API Gateway endpoint URL
3. Configure Ombi webhook to point to the Lambda endpoint
4. Enable "Request Approved" and "Request Denied" notifications in Ombi

The Lambda function handles:
- ✅ **Request Approved** → Sends message to Telegram group thread
- ✅ **Request Denied** → Sends message to Telegram group thread

## Production Deployment

The bot uses polling and doesn't require any exposed ports or reverse proxy. Webhook notifications are handled by AWS Lambda (see [`lambda/`](lambda/) directory).

### Architecture Overview

```
Docker Container (Bot)
  └── Polling: Bot → Telegram API (outbound only, no ports needed)

Webhook: Ombi → AWS Lambda (API Gateway) → Telegram
```

### Security Considerations

1. **No Exposed Ports**: The bot uses polling and doesn't require any exposed ports. This reduces the attack surface.

2. **Lambda Environment**: Configure Lambda environment variables securely (see `lambda/README.md`). Use AWS Secrets Manager or environment variables for sensitive data.

3. **Network Isolation**: Use Docker networks to isolate services. The bot only needs to communicate with:
   - Ombi (internal network)
   - Telegram API (internet, outbound HTTPS only)

### Testing the Lambda Webhook

After deploying Lambda, test the webhook endpoint:

```bash
curl -X POST https://your-api-gateway-url.amazonaws.com/prod/notifications/ombi \
  -H "Content-Type: application/json" \
  -H "User-Agent: Ombi/4.47.1 (https://ombi.io/)" \
  -d '{
    "notificationType": "RequestApproved",
    "title": "Test Movie",
    "type": "Movie",
    "year": "2024"
  }'
```

You should receive a Telegram notification in your configured group thread.

## Troubleshooting

- **Bot not responding**: Check that `TELEGRAM_BOT_TOKEN` is set correctly
- **Ombi API errors**: Verify `OMBI_URL` and `OMBI_API_KEY` are correct
- **No results found**: Try refining your search query or use an IMDb link
- **Poster images not showing**: This is normal if Ombi doesn't have poster data - the bot will show text-only results

## Ombi URL Configuration (Organizr Setup)

If your Ombi is running within an Organizr setup, the `OMBI_URL` depends on how you're accessing it:

### Option 1: Docker Network (Recommended for Docker deployment)
If both Ombi and the bot are in the same Docker network (e.g., via docker-compose):
```bash
OMBI_URL=http://ombi:3579
```
Use the Docker service/container name as the hostname. This is the most reliable option when both services are containerized.

### Option 2: Organizr Reverse Proxy (Subdomain)
If Ombi is accessible via a subdomain through Organizr's reverse proxy:
```bash
OMBI_URL=https://ombi.yourdomain.com
```

### Option 3: Organizr Reverse Proxy (Subdirectory)
If Ombi is accessible via a subdirectory path:
```bash
OMBI_URL=https://yourdomain.com/ombi
```
**Note**: Make sure the path doesn't include `/ombi` twice if Ombi's base path is already configured in its settings.

### Option 4: Direct Access (Local Testing)
If testing locally and Ombi is accessible directly on your machine:
```bash
OMBI_URL=http://localhost:3579
```

### Getting 401 (Organizr SSO blocking the API)

If you point `OMBI_URL` at a public subdirectory URL (e.g. `https://yourdomain.com/ombi`)
and requests fail with an nginx **401 Authorization Required** HTML page (not a JSON
error from Ombi), your reverse proxy is protecting `/ombi/` with Organizr SSO
(`auth_request`). Browser sessions pass because they carry the Organizr cookie, but the
bot only sends an `ApiKey` header, so the SSO subrequest rejects it before it reaches Ombi.

This works in Docker because the bot talks to `http://ombi:3579` internally and never
passes through nginx. It only surfaces when running locally against the public URL.

**Fix:** let the Ombi API path bypass SSO (the API is already protected by the `ApiKey`
header). Add a more-specific `location` *above* the existing `/ombi/` block in your nginx
config:

```nginx
location /ombi/api/ {
  # Ombi's API authenticates via the ApiKey header, so bypass Organizr SSO.
  auth_request off;
  proxy_pass http://127.0.0.1:3579/ombi/api/;
}

location /ombi/ {
  auth_request /organizr-auth/0;
  proxy_pass http://127.0.0.1:3579/ombi/;
}
```

Then `nginx -t && systemctl reload nginx`. To keep the API private to your machine while
testing, add `allow YOUR.IP;` and `deny all;` inside the `/ombi/api/` block.

### Finding Your Ombi URL
1. **Check your Organizr tabs** - Right-click the Ombi tab and copy the URL
2. **Check Docker Compose** - Look for the Ombi service name in your `docker-compose.yml`
3. **Check Organizr settings** - Look at the reverse proxy configuration in Organizr
4. **Test the API directly**:
   ```bash
   curl -H "ApiKey: YOUR_API_KEY" http://your-ombi-url/api/v1/Status
   ```
   If this returns JSON, the URL is correct.

### Common Organizr Configurations

**If using SWAG/Nginx Proxy Manager with Organizr:**
- Usually: `https://ombi.yourdomain.com` or `https://yourdomain.com/ombi`

**If using Traefik with Organizr:**
- Usually: `https://ombi.yourdomain.com` (based on Traefik labels)

**If all services are in the same Docker network:**
- Use the service name: `http://ombi:3579` (no HTTPS needed internally)

## License

MIT

