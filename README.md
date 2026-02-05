# JZWM MCQ Generator - ACZM Board Prep

A web application that extracts recent articles from the Journal of Zoo and Wildlife Medicine (JZWM) via PubMed and generates ACZM-style multiple choice questions for board preparation.

## Features

- **Automatic Article Extraction**: Fetches articles from the last 3 months from JZWM via PubMed API
- **MCQ Generation**: Creates ACZM board-style multiple choice questions using Claude AI
- **Email Delivery**: Sends generated questions to selected recipients
- **Preview Mode**: View generated questions before sending

## Setup

### 1. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

Copy the example environment file and configure your settings:

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Anthropic API Key for MCQ generation
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Email Configuration (Gmail example)
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_email@gmail.com
SMTP_PASSWORD=your_app_password_here
EMAIL_FROM=your_email@gmail.com

# Flask Configuration
FLASK_SECRET_KEY=generate_a_random_secret_key
FLASK_DEBUG=False
```

### Gmail Setup

To use Gmail for sending emails:
1. Enable 2-Factor Authentication on your Google account
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Use the App Password (not your regular password) in `SMTP_PASSWORD`

### 3. Run the Application

```bash
python app.py
```

The application will be available at `http://localhost:5000`

## Usage

1. **View Articles**: When the app loads, it automatically fetches recent JZWM articles from PubMed
2. **Select Recipient**: Choose an email recipient from the dropdown
3. **Set Question Count**: Enter the number of MCQ questions to generate (1-20)
4. **Preview** (Optional): Click "Preview Questions" to see generated questions before sending
5. **Send**: Click "Generate & Send Email" to create questions and email them

## Adding Email Recipients

To add more recipients, edit the `EMAIL_RECIPIENTS` list in `app.py`:

```python
EMAIL_RECIPIENTS = [
    {"email": "hbeaufrere@ucdavis.edu", "name": "H. Beaufrere"},
    {"email": "another@email.com", "name": "Another Person"},
]
```

## API Endpoints

- `GET /api/articles` - Fetch recent JZWM articles from PubMed
- `POST /api/generate-mcq` - Generate MCQ questions (JSON body: `{num_questions: 5}`)
- `POST /api/send-email` - Generate and send MCQ email (JSON body: `{recipient_email: "...", num_questions: 5}`)
- `GET /api/recipients` - Get list of email recipients

## Production Deployment

For production, use gunicorn:

```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

## License

For educational and board preparation purposes.
