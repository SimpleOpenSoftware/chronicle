# Email Summarizer Plugin

Automatically sends email summaries when conversations complete.

## Features

- 📧 **Automatic Email Delivery**: Sends emails when conversations finish
- 🤖 **LLM-Powered Summaries**: Uses your configured LLM to generate intelligent summaries
- 🎨 **Beautiful HTML Emails**: Professional-looking emails with proper formatting
- 📱 **Plain Text Fallback**: Ensures compatibility with all email clients
- ⚡ **Async Processing**: Non-blocking email sending
- 🔒 **Secure SMTP**: TLS/SSL encryption support

## How It Works

1. User completes a conversation (via OMI device or file upload)
2. Plugin receives `conversation.complete` event
3. Retrieves user email from database
4. Generates LLM summary (2-3 sentences)
5. Formats beautiful HTML and plain text emails
6. Sends email via configured SMTP server

## Configuration Architecture

Chronicle uses a clean three-file separation for plugin configuration:

1. **`backends/advanced/.env`** - Secrets only (SMTP credentials, API keys)
   - Gitignored for security
   - Never commit to version control

2. **`plugins/email_summarizer/config.yml`** - Plugin-specific settings
   - Email content options (subject prefix, max sentences, etc.)
   - References environment variables using `${VAR_NAME}` syntax
   - Defaults work for most users - typically no editing needed

3. **`config/plugins.yml`** - Orchestration only
   - `enabled` flag
   - Event subscriptions
   - Trigger conditions

This separation keeps secrets secure and configuration organized. See the
[plugin configuration guide](../../docs/backend/plugin-configuration.md) for details.

## Configuration

### Step 1: Get SMTP Credentials

#### For Gmail (Recommended for Testing):

1. **Enable 2-Factor Authentication** on your Google account
2. Go to Google Account → Security → 2-Step Verification
3. Scroll down to **App passwords**
4. Generate an app password for "Mail"
5. Copy the 16-character password (no spaces)

#### For Other Providers:

- **Outlook/Hotmail**: smtp.office365.com:587
- **Yahoo**: smtp.mail.yahoo.com:587
- **Custom SMTP**: Use your provider's settings

### Step 2: Configure Environment Variables

Add to `backends/advanced/.env`:

```bash
# Email Summarizer Plugin
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password-here  # Gmail App Password (16 chars, no spaces)
SMTP_USE_TLS=true
FROM_EMAIL=noreply@chronicle.ai
FROM_NAME=Chronicle AI
```

### Step 3: Enable Plugin

Add to `config/plugins.yml` (orchestration only):

```yaml
plugins:
  email_summarizer:
    enabled: true
    events:
      - conversation.complete
    condition:
      type: always
```

**That's it!** Plugin-specific settings are already configured in:
- **`plugins/email_summarizer/config.yml`** - Email content options (subject prefix, max sentences, etc.)
- **SMTP credentials** are automatically read from `.env` via environment variable references

You typically don't need to edit `config.yml` - the defaults work for most users. If you want to customize email content settings, see the Configuration Options section below.

### Step 4: Restart Backend

```bash
cd backends/advanced
docker compose restart
```

## Configuration Options

All configuration options below are in **`plugins/email_summarizer/config.yml`** and have sensible defaults. You typically don't need to modify these unless you want to customize email content.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `smtp_host` | string | `smtp.gmail.com` | SMTP server hostname |
| `smtp_port` | integer | `587` | SMTP server port (587 for TLS, 465 for SSL) |
| `smtp_username` | string | **Required** | SMTP authentication username |
| `smtp_password` | string | **Required** | SMTP authentication password |
| `smtp_use_tls` | boolean | `true` | Use STARTTLS encryption |
| `from_email` | string | **Required** | Sender email address |
| `from_name` | string | `Chronicle AI` | Sender display name |
| `subject_prefix` | string | `Conversation Summary` | Email subject prefix |
| `summary_max_sentences` | integer | `3` | Maximum sentences in LLM summary |
| `include_conversation_id` | boolean | `true` | Show conversation ID in email |
| `include_duration` | boolean | `true` | Show conversation duration |

## Email Template

### Subject Line
```
Conversation Summary - Jan 15, 2025 at 10:30 AM
```

### Email Body
```
📋 SUMMARY
[LLM-generated 2-3 sentence summary of key points]

📝 FULL TRANSCRIPT
[Complete conversation transcript]

📊 METADATA
Duration: 5m 30s
Conversation ID: 507f1f77bc...
```

## Testing

### Test SMTP Connection

```bash
cd backends/advanced
uv run python -m advanced_omi_backend.services.email_service
```

This will:
- Test SMTP connectivity
- Send a test email to your SMTP username
- Verify configuration

### Test Plugin Integration

1. Start the backend with plugin enabled
2. Upload a test audio file or use OMI device
3. Wait for conversation to complete
4. Check your email inbox

## Troubleshooting

### "Authentication failed"

**For Gmail:**
- Make sure you're using an **App Password**, not your regular password
- Enable 2-Factor Authentication first
- App password should be 16 characters (xxxx xxxx xxxx xxxx)

**For other providers:**
- Verify username and password are correct
- Check if "less secure apps" needs to be enabled

### "Connection timeout"

- Check `smtp_host` and `smtp_port` are correct
- Verify firewall allows outbound SMTP connections
- Try port 465 with SSL instead of 587 with TLS

### "No email received"

- Check user has email configured in database
- Look for plugin logs: `docker compose logs -f chronicle-backend | grep EmailSummarizer`
- Verify plugin is enabled in `plugins.yml`
- Check spam/junk folder

### "Empty summary" or "LLM error"

- Verify LLM service is configured and running
- Check LLM API keys are valid
- Plugin will fall back to truncated transcript if LLM fails

## 🔒 Security Best Practices

### NEVER Commit Secrets to Version Control

Always use environment variable references in configuration files:

```yaml
# plugins/email_summarizer/config.yml
smtp_password: ${SMTP_PASSWORD}  # Reference to environment variable
```

```bash
# backends/advanced/.env (gitignored)
SMTP_PASSWORD=xnetcqctkkfgzllh  # Actual secret stored safely
```

### How Configuration Works

The plugin system automatically:
- ✅ Loads settings from `plugins/email_summarizer/config.yml`
- ✅ Expands `${ENV_VAR}` references from `backends/advanced/.env`
- ✅ Merges orchestration settings (enabled, events) from `config/plugins.yml`
- ✅ Prevents accidental secret commits (only .env has secrets, and it's gitignored)

**Always use the setup wizard** instead of manual configuration:
```bash
uv run python backends/advanced/src/advanced_omi_backend/plugins/email_summarizer/setup.py
```

### Additional Security Tips

1. **Never commit SMTP passwords** to git (use .env only)
2. **Use environment variable references** (`${SMTP_PASSWORD}`) in YAML files
3. **Enable TLS/SSL** for encrypted SMTP connections
4. **Gmail App Passwords** are safer than account passwords
5. **Rotate credentials** periodically
6. **Review commits** before pushing to ensure no hardcoded secrets

## Development

### File Structure

```
plugins/email_summarizer/
├── __init__.py           # Plugin exports
├── plugin.py             # Main plugin logic
├── templates.py          # Email HTML/text templates
└── README.md             # This file
```

### Key Methods

- `on_conversation_complete()` - Main event handler
- `_get_user_email()` - Fetch user email from database
- `_generate_summary()` - Generate LLM summary with fallback
- `_format_subject()` - Format email subject line

### Dependencies

- `advanced_omi_backend.database` - MongoDB access
- `advanced_omi_backend.llm_client` - LLM generation
- `advanced_omi_backend.services.email_service` - SMTP email sending

## Future Enhancements

- [ ] Email templates customization
- [ ] User preference for email frequency
- [ ] Unsubscribe link
- [ ] Email digests (daily/weekly summaries)
- [ ] Rich formatting for action items
- [ ] Attachment support (audio files)
- [ ] Multiple recipient support
- [ ] Email open tracking

## Support

- **Issues**: [GitHub Issues](https://github.com/chronicle-ai/chronicle/issues)
- **Discussions**: [GitHub Discussions](https://github.com/chronicle-ai/chronicle/discussions)
- **Documentation**: [Chronicle Docs](https://github.com/chronicle-ai/chronicle)

## License

GNU Affero General Public License v3.0 only - see project LICENSE file for details.
