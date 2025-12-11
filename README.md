# Telegram Ombi Bot

A Telegram bot that allows users to request movies and TV shows through Ombi with an intuitive chat-based interface featuring posters, descriptions, and easy navigation.

## Features

- 🎬 **Request Movies** - Search and request movies with poster images and descriptions
- 📺 **Request TV Shows** - Search and request TV shows with poster images and descriptions
- 🖼️ **Visual Results** - Browse results with posters, ratings, and descriptions
- 🔄 **Easy Navigation** - Scroll through multiple results with Previous/Next buttons
- 💬 **Chat-Based** - Everything stays within Telegram chat (no external apps)
- 🔗 **IMDb Support** - Search using IMDb links or titles
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

In production, you'll typically use a reverse proxy (Nginx, Traefik, Caddy, etc.) instead of ngrok for the webapp. The webhook is handled by AWS Lambda.

### Architecture Overview

```
Internet → Reverse Proxy (HTTPS) → Docker Container (HTTP)
                                    └── Port 8080: Webapp (optional)

Webhook: Ombi → AWS Lambda → Telegram
```

### Option 1: Nginx Reverse Proxy

1. **Update your docker-compose.yml** (see `docker-compose.yml` in the repo)

2. **Configure Nginx** - Copy `nginx.example.conf` and update:
   - Replace `your-bot-domain.com` with your actual domain
   - Update SSL certificate paths
   - Adjust proxy_pass URLs if needed

3. **In Ombi webhook settings**, use:
   ```
   https://your-bot-domain.com/notifications/ombi
   ```

### Option 2: Traefik Reverse Proxy

1. **Update docker-compose.yml** - Add Traefik labels (see `traefik.example.yml`)

2. **Example docker-compose.yml with Traefik labels:**
   ```yaml
   services:
     ombi-tele-bot:
       # ... other config ...
       labels:
         - "traefik.enable=true"
         - "traefik.http.routers.ombi-bot-webhook.rule=Host(`your-bot-domain.com`) && PathPrefix(`/notifications/ombi`)"
         - "traefik.http.routers.ombi-bot-webhook.entrypoints=websecure"
         - "traefik.http.routers.ombi-bot-webhook.tls.certresolver=letsencrypt"
         - "traefik.http.services.ombi-bot-webhook.loadbalancer.server.port=8081"
   ```

3. **In Ombi webhook settings**, use:
   ```
   https://your-bot-domain.com/notifications/ombi
   ```

### Option 3: Cloudflare Tunnel (Cloudflared)

If you're using Cloudflare Tunnel, you can expose the webhook port directly:

1. **In your Cloudflare Tunnel config**, add:
   ```yaml
   ingress:
     - hostname: your-bot-domain.com
       service: http://ombi-tele-bot:8081
   ```

2. **In Ombi webhook settings**, use:
   ```
   https://your-bot-domain.com/notifications/ombi
   ```

### Port Configuration

- **Webhook**: Port 8081 (configurable via `WEBHOOK_PORT` env var)
- **Webapp**: Port 8080 (if using the webapp feature)

### Security Considerations

1. **HTTPS**: Always use HTTPS in production. The reverse proxy should handle SSL termination.

2. **Firewall**: Only expose the reverse proxy port (443/80) to the internet. Don't expose container ports directly.

3. **Lambda Environment**: Configure Lambda environment variables (see `lambda/README.md`).

4. **Network Isolation**: Use Docker networks to isolate services. The bot only needs to communicate with:
   - Ombi (internal network)
   - Telegram API (internet)
   - Reverse proxy (internal network, if using webapp)

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

