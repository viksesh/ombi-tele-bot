# Scalability & Resource Usage

## Polling vs Webhooks for 100 Users

### How Polling Works

```
Bot: "Any updates?" → Telegram API
Telegram: "Yes, here are 5 messages from different users"
Bot: Processes all 5 messages
Bot: "Any updates?" → Telegram API (waits ~1 second)
Telegram: "No new updates"
Bot: "Any updates?" → Telegram API (waits ~1 second)
...
```

**Key Point**: One polling request checks for updates from ALL users, not per-user.

### Resource Consumption

**Polling (Current Setup):**
- ✅ **CPU**: Minimal (~1-2% idle, spikes during message processing)
- ✅ **Memory**: ~50-100MB (Python + dependencies)
- ✅ **Network**: One HTTP request every 1-2 seconds (very low bandwidth)
- ✅ **Concurrent Users**: Scales well - one request checks all users

**For 100 Users:**
- Same polling frequency regardless of user count
- Only difference: More messages to process when they arrive
- Message processing is async and efficient

### Performance Characteristics

| Metric | Polling (100 users) | Notes |
|--------|---------------------|-------|
| HTTP Requests/sec | ~0.5-1 | One request every 1-2 seconds |
| CPU Usage (idle) | 1-2% | Very low when no activity |
| CPU Usage (active) | 5-15% | Spikes during message processing |
| Memory | 50-100MB | Stable, doesn't grow with users |
| Network Bandwidth | <1 KB/s | Minimal |
| Latency | 1-2 seconds | Time between polling requests |

### Bottlenecks (Not Polling)

The actual bottlenecks are:
1. **Ombi API response time** - When searching/requesting
2. **Message processing** - Parsing, formatting, sending responses
3. **Telegram API rate limits** - 30 messages/second per bot

### Is Polling Sufficient for 100 Users?

**Yes, absolutely!** Here's why:

1. **Polling checks all users in one request** - Not per-user
2. **python-telegram-bot handles concurrency** - Processes multiple updates efficiently
3. **Async message handling** - Can handle bursts of messages
4. **Telegram rate limits are the constraint** - Not polling itself

### When to Consider Webhooks

Consider webhooks if:
- ❌ **>1000 concurrent users** (polling starts to lag)
- ❌ **Need <1 second latency** (polling has 1-2s delay)
- ❌ **Very high message volume** (>1000 messages/minute)

For 100 users, polling is:
- ✅ Simpler (no HTTPS/port management)
- ✅ More reliable (no webhook delivery issues)
- ✅ Easier to debug
- ✅ Sufficient performance

### Docker Resource Recommendations

**Minimum (for 100 users):**
```yaml
deploy:
  resources:
    limits:
      cpus: '0.5'      # Half a CPU core
      memory: 256M     # 256MB RAM
    reservations:
      cpus: '0.25'     # Quarter CPU core
      memory: 128M     # 128MB RAM
```

**Recommended (comfortable margin):**
```yaml
deploy:
  resources:
    limits:
      cpus: '1.0'      # One CPU core
      memory: 512M     # 512MB RAM
    reservations:
      cpus: '0.5'      # Half CPU core
      memory: 256M     # 256MB RAM
```

### Optimizing Polling (if needed)

If you notice delays with 100+ users, you can tune polling:

```python
# In bot.py, modify run_polling:
application.run_polling(
    allowed_updates=Update.ALL_TYPES,
    drop_pending_updates=True,  # Ignore old updates on restart
    poll_interval=0.5,          # Check every 0.5 seconds (default: 1.0)
    timeout=20,                  # Request timeout (default: 10)
    bootstrap_retries=5,         # Retry on startup failures
    read_timeout=2,              # Read timeout
    write_timeout=2,             # Write timeout
    connect_timeout=2,           # Connect timeout
)
```

### Monitoring

Watch these metrics:
- **Message processing time** - Should be <100ms per message
- **Ombi API latency** - Should be <500ms
- **Memory usage** - Should stay stable (no leaks)
- **Error rate** - Should be <1%

### Conclusion

**For 100 concurrent users:**
- ✅ Polling is sufficient and recommended
- ✅ Resource usage is minimal (~100MB RAM, <1 CPU core)
- ✅ Works perfectly in Docker on Ubuntu
- ✅ Simpler than webhooks (no HTTPS/ports needed)
- ✅ No scalability concerns until 500+ users

The current implementation is production-ready for your use case!

