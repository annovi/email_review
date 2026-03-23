# Email Reviewer

A comprehensive email review tool for managing large inboxes with AI-powered categorization.

## Features

- **📊 Folder Overview** - See all folders with email counts and review progress
- **🏷️ Auto-Categorization** - Automatically categorize emails by sender domain and keywords
- **🤖 AI Analysis** - Use Claude to analyze and categorize emails (optional)
- **✅ Review Workflow** - Mark emails as Useful or Not Useful
- **📦 Batch Processing** - Review emails in manageable batches
- **🔍 Smart Search** - Filter by sender, subject, or unread status
- **📄 Export** - Generate summary reports of your decisions

## Project Structure

```
email_review/
├── main.py              # Main application entry point
├── src/
│   ├── __init__.py
│   ├── config.py        # Configuration settings
│   ├── imap_client.py   # IMAP connection and email parsing
│   ├── storage.py       # Data persistence (JSON)
│   ├── categorizer.py   # Rule-based and AI categorization
│   └── ui.py            # Terminal UI utilities
├── data/                # Stored decisions and categories
│   ├── decisions.json
│   └── categories.json
└── requirements.txt
```

## Setup

1. **Get an App-Specific Password** from Apple:
   - Go to https://appleid.apple.com
   - Sign-In and Security → App-Specific Passwords
   - Generate a password and label it "Email Reviewer"

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure credentials** in `src/config.py`:
   ```python
   EMAIL_ADDRESS = "yourname@mac.com"
   APP_PASSWORD = "your-app-specific-password"
   ```

4. **Run the application**:
   ```bash
   python main.py
   ```

## Usage

### Interactive Mode
```bash
python main.py
```

### Show Statistics Only
```bash
python main.py --stats
```

## Categories

Emails are automatically categorized into:

| Category | Description |
|----------|-------------|
| 📰 newsletters | News digests, daily updates, blogs |
| 🏷️ promotions | Marketing, sales, discounts |
| 👥 social | LinkedIn, social networks |
| 💰 finance | Banks, investments, payments |
| ✈️ travel | Airlines, hotels, bookings |
| 📦 shopping | Orders, shipping, receipts |
| 💪 fitness | Fitness, nutrition, health |
| 🔔 notifications | Alerts, system notifications |
| 📧 other | Uncategorized |

## AI Features (Optional)

To enable AI-powered analysis:

1. Install the Anthropic package:
   ```bash
   pip install anthropic
   ```

2. Copy `.secrets.example` to `.secrets` and add your API key:
   ```bash
   cp .secrets.example .secrets
   # Edit .secrets with your credentials
   ```

3. Use `[a]` during review to get AI analysis

## Data Storage

- **decisions.json** - Your useful/not-useful decisions
- **categories.json** - Email category assignments

Both files are stored in the `data/` directory and persist between sessions.

## License

MIT
