"""
PubMed Article Extractor and MCQ Generator
Extracts articles from Journal of Zoo and Wildlife Medicine and generates ACZM-style MCQs
"""

import os
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, request
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')

# Configuration
EMAIL_RECIPIENTS = [
    {"email": "hbeaufrere@ucdavis.edu", "name": "H. Beaufrere"}
]

PUBMED_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
JOURNAL_NAME = "J Zoo Wildl Med"  # PubMed abbreviation


def get_date_range(months=12):
    """Get date range for the specified number of months"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=months * 30)
    return start_date.strftime("%Y/%m/%d"), end_date.strftime("%Y/%m/%d")


def search_pubmed_articles(months=12):
    """Search PubMed for articles from Journal of Zoo and Wildlife Medicine"""
    start_date, end_date = get_date_range(months)

    # Build search query
    query = f'"{JOURNAL_NAME}"[Journal] AND ("{start_date}"[Date - Publication] : "{end_date}"[Date - Publication])'

    # First, search for article IDs
    search_url = f"{PUBMED_BASE_URL}/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmax": 100,
        "retmode": "json",
        "sort": "pub_date"
    }

    try:
        response = requests.get(search_url, params=search_params, timeout=30)
        response.raise_for_status()
        search_results = response.json()

        id_list = search_results.get("esearchresult", {}).get("idlist", [])

        if not id_list:
            return []

        # Fetch article details
        return fetch_article_details(id_list)

    except requests.RequestException as e:
        print(f"Error searching PubMed: {e}")
        return []


def fetch_article_details(pmid_list):
    """Fetch detailed information for articles by PMID"""
    if not pmid_list:
        return []

    fetch_url = f"{PUBMED_BASE_URL}/efetch.fcgi"
    fetch_params = {
        "db": "pubmed",
        "id": ",".join(pmid_list),
        "retmode": "xml",
        "rettype": "abstract"
    }

    try:
        response = requests.get(fetch_url, params=fetch_params, timeout=30)
        response.raise_for_status()

        # Parse XML response
        from xml.etree import ElementTree as ET
        root = ET.fromstring(response.content)

        articles = []
        for article in root.findall(".//PubmedArticle"):
            article_data = parse_article_xml(article)
            if article_data:
                articles.append(article_data)

        return articles

    except requests.RequestException as e:
        print(f"Error fetching article details: {e}")
        return []


def parse_article_xml(article_element):
    """Parse individual article XML element"""
    try:
        medline = article_element.find(".//MedlineCitation")
        if medline is None:
            return None

        pmid_elem = medline.find(".//PMID")
        pmid = pmid_elem.text if pmid_elem is not None else "Unknown"

        article = medline.find(".//Article")
        if article is None:
            return None

        # Title - use itertext() to capture text within nested tags (italics, etc.)
        title_elem = article.find(".//ArticleTitle")
        if title_elem is not None:
            title = "".join(title_elem.itertext())
        else:
            title = "No title"

        # Abstract - use itertext() to capture text within nested tags (italics, etc.)
        abstract_parts = article.findall(".//Abstract/AbstractText")
        if abstract_parts:
            abstract_texts = []
            for part in abstract_parts:
                part_text = "".join(part.itertext())
                if part_text:
                    # Add label if present (for structured abstracts)
                    label = part.get("Label", "")
                    if label:
                        abstract_texts.append(f"{label}: {part_text}")
                    else:
                        abstract_texts.append(part_text)
            abstract = " ".join(abstract_texts) if abstract_texts else "No abstract available"
        else:
            abstract = "No abstract available"

        # Authors
        authors = []
        for author in article.findall(".//AuthorList/Author"):
            last_name = author.find("LastName")
            fore_name = author.find("ForeName")
            if last_name is not None:
                name = last_name.text
                if fore_name is not None:
                    name = f"{fore_name.text} {name}"
                authors.append(name)

        # Publication date
        pub_date = article.find(".//Journal/JournalIssue/PubDate")
        date_str = ""
        if pub_date is not None:
            year = pub_date.find("Year")
            month = pub_date.find("Month")
            if year is not None:
                date_str = year.text
                if month is not None:
                    date_str = f"{month.text} {date_str}"

        # Journal info
        journal = article.find(".//Journal/Title")
        journal_name = journal.text if journal is not None else JOURNAL_NAME

        return {
            "pmid": pmid,
            "title": title,
            "abstract": abstract,
            "authors": ", ".join(authors[:5]) + ("..." if len(authors) > 5 else ""),
            "pub_date": date_str,
            "journal": journal_name,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        }

    except Exception as e:
        print(f"Error parsing article: {e}")
        return None


def generate_mcq_from_article(article, num_questions=1):
    """Generate ACZM-style MCQ from an article using Claude API"""
    api_key = os.getenv('ANTHROPIC_API_KEY')

    if not api_key:
        return generate_fallback_mcq(article, num_questions, "ANTHROPIC_API_KEY environment variable is not set")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""Based on the following veterinary article abstract, generate {num_questions} multiple choice question(s) in ACZM (American College of Zoological Medicine) board examination style.

Article Title: {article['title']}

Abstract: {article['abstract']}

For each question:
1. Create a clinically relevant question that tests understanding of the key findings or concepts
2. Provide 4 answer options (A, B, C, D)
3. Indicate the correct answer
4. Provide a brief explanation

Format each question as:
QUESTION [number]:
[Question text]

A) [Option A]
B) [Option B]
C) [Option C]
D) [Option D]

CORRECT ANSWER: [Letter]

EXPLANATION: [Brief explanation of why this is correct and why other options are incorrect]

---

Make questions appropriate for board-level veterinary specialists focusing on zoo and wildlife medicine."""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        return {
            "article_title": article['title'],
            "article_pmid": article['pmid'],
            "article_url": article['url'],
            "questions": message.content[0].text
        }

    except Exception as e:
        print(f"Error generating MCQ with API: {e}")
        return generate_fallback_mcq(article, num_questions, str(e))


def generate_fallback_mcq(article, num_questions=1, error_msg=None):
    """Generate a basic template MCQ when API is unavailable"""
    error_note = f"Error: {error_msg}" if error_msg else "Note: API key not configured. Please add your ANTHROPIC_API_KEY to generate AI-powered questions."
    return {
        "article_title": article['title'],
        "article_pmid": article['pmid'],
        "article_url": article['url'],
        "questions": f"""QUESTION 1:
Based on the study "{article['title']}", which of the following statements is most accurate regarding the findings?

A) [Review the abstract to determine the correct answer]
B) [Alternative interpretation]
C) [Common misconception]
D) [Unrelated finding]

CORRECT ANSWER: [To be determined after reviewing full article]

EXPLANATION: Please review the full article at {article['url']} to determine the correct answer and explanation.

---
{error_note}"""
    }


def send_email(recipient_email, subject, html_content):
    """Send email with MCQ questions"""
    smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
    smtp_port = int(os.getenv('SMTP_PORT', 587))
    smtp_username = os.getenv('SMTP_USERNAME')
    smtp_password = os.getenv('SMTP_PASSWORD')
    email_from = os.getenv('EMAIL_FROM', smtp_username)

    if not smtp_username or not smtp_password:
        return False, "Email not configured. Please set SMTP_USERNAME and SMTP_PASSWORD in .env file."

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = email_from
        msg['To'] = recipient_email

        html_part = MIMEText(html_content, 'html')
        msg.attach(html_part)

        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.sendmail(email_from, recipient_email, msg.as_string())

        return True, "Email sent successfully"

    except smtplib.SMTPAuthenticationError as e:
        return False, f"Email authentication failed. Check your SMTP_USERNAME and SMTP_PASSWORD. Error: {str(e)}"
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {str(e)}"
    except Exception as e:
        return False, f"Error sending email: {str(e)}"


def format_mcq_email(mcq_results):
    """Format MCQ results as HTML email"""
    html = """
    <html>
    <head>
        <style>
            body { font-family: Arial, sans-serif; line-height: 1.6; color: #333; }
            .header { background-color: #2c5282; color: white; padding: 20px; text-align: center; }
            .question-block { background-color: #f7fafc; border-left: 4px solid #2c5282; padding: 15px; margin: 20px 0; }
            .article-info { font-size: 0.9em; color: #666; margin-bottom: 10px; }
            .question { font-weight: bold; margin-bottom: 10px; }
            .options { margin-left: 20px; }
            .answer { background-color: #c6f6d5; padding: 10px; margin-top: 10px; border-radius: 5px; }
            .explanation { background-color: #bee3f8; padding: 10px; margin-top: 10px; border-radius: 5px; }
            a { color: #2c5282; }
            hr { border: none; border-top: 1px solid #e2e8f0; margin: 30px 0; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>ACZM Board Prep - MCQ Questions</h1>
            <p>Generated from Journal of Zoo and Wildlife Medicine</p>
        </div>
        <div style="padding: 20px;">
    """

    for i, mcq in enumerate(mcq_results, 1):
        html += f"""
        <div class="question-block">
            <div class="article-info">
                <strong>Source Article:</strong> {mcq['article_title']}<br>
                <a href="{mcq['article_url']}" target="_blank">PubMed Link (PMID: {mcq['article_pmid']})</a>
            </div>
            <pre style="white-space: pre-wrap; font-family: Arial, sans-serif;">{mcq['questions']}</pre>
        </div>
        """
        if i < len(mcq_results):
            html += "<hr>"

    html += """
        </div>
        <div style="background-color: #edf2f7; padding: 15px; text-align: center; font-size: 0.9em; color: #666;">
            <p>Generated by PubMed MCQ Generator for ACZM Board Preparation</p>
        </div>
    </body>
    </html>
    """

    return html


# Routes
@app.route('/')
def index():
    """Main page"""
    return render_template('index.html', recipients=EMAIL_RECIPIENTS)


@app.route('/api/articles')
def get_articles():
    """API endpoint to fetch articles"""
    months = int(request.args.get('months', 12))
    articles = search_pubmed_articles(months)
    return jsonify({
        "success": True,
        "count": len(articles),
        "articles": articles,
        "date_range": get_date_range(months)
    })


@app.route('/api/generate-mcq', methods=['POST'])
def generate_mcq():
    """API endpoint to generate MCQs"""
    data = request.json
    num_questions = int(data.get('num_questions', 5))
    months = int(data.get('months', 12))

    # Fetch articles using the selected time period
    articles = search_pubmed_articles(months)

    if not articles:
        return jsonify({
            "success": False,
            "error": f"No articles found in the selected time period ({months} months)"
        })

    # Filter articles with abstracts
    articles_with_abstracts = [a for a in articles if a['abstract'] != "No abstract available"]

    if not articles_with_abstracts:
        articles_with_abstracts = articles

    # Randomly select articles for questions
    num_articles = min(num_questions, len(articles_with_abstracts))
    selected_articles = random.sample(articles_with_abstracts, num_articles)

    # Generate MCQs
    mcq_results = []
    for article in selected_articles:
        mcq = generate_mcq_from_article(article, 1)
        mcq_results.append(mcq)

    return jsonify({
        "success": True,
        "mcq_results": mcq_results
    })


@app.route('/api/send-email', methods=['POST'])
def send_mcq_email():
    """API endpoint to send pre-generated MCQs via email"""
    data = request.json
    recipient_email = data.get('recipient_email')
    mcq_results = data.get('mcq_results')

    if not recipient_email:
        return jsonify({
            "success": False,
            "error": "No recipient email provided"
        })

    if not mcq_results or len(mcq_results) == 0:
        return jsonify({
            "success": False,
            "error": "No questions to send. Please generate questions first."
        })

    # Format email
    html_content = format_mcq_email(mcq_results)
    subject = f"ACZM Board Prep - {len(mcq_results)} MCQ Questions from JZWM"

    # Send email
    success, message = send_email(recipient_email, subject, html_content)

    return jsonify({
        "success": success,
        "message": message,
        "mcq_count": len(mcq_results)
    })


@app.route('/api/recipients')
def get_recipients():
    """Get list of email recipients"""
    return jsonify({
        "success": True,
        "recipients": EMAIL_RECIPIENTS
    })


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)
