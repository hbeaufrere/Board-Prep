"""
ACZM MCQ Generator
Extracts articles from veterinary journals and generates ACZM-style MCQs
"""

import os
import random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify, request
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')

PUBMED_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Journal configurations with search sources
JOURNALS = {
    "JZWM": {
        "name": "Journal of Zoo and Wildlife Medicine",
        "abbrev": "JZWM",
        "query": '"J Zoo Wildl Med"[Journal]',
        "exclude": None,
        "source": "pubmed"
    },
    "JAMS": {
        "name": "Journal of Avian Medicine and Surgery",
        "abbrev": "JAMS",
        "query": '"J Avian Med Surg"[Journal]',
        "exclude": None,
        "source": "pubmed"
    },
    "JWD": {
        "name": "Journal of Wildlife Diseases",
        "abbrev": "JWD",
        "query": '"J Wildl Dis"[Journal]',
        "exclude": "Letter",
        "source": "pubmed"
    },
    "JHMS": {
        "name": "Journal of Herpetological Medicine and Surgery",
        "abbrev": "JHMS",
        "issn": "1529-9651",
        "exclude": None,
        "source": "crossref"
    }
}


def get_date_range(months=12):
    """Get date range for the specified number of months"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=months * 30)
    return start_date.strftime("%Y/%m/%d"), end_date.strftime("%Y/%m/%d")


def search_crossref(months=12, journal="JHMS", max_results=200):
    """Search CrossRef API for articles from a journal by ISSN"""
    j_info = JOURNALS.get(journal, JOURNALS["JHMS"])
    issn = j_info.get('issn')

    if not issn:
        print(f"No ISSN configured for {journal}")
        return []

    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=months * 30)
    from_date = start_date.strftime("%Y-%m-%d")

    articles = []
    try:
        # CrossRef API endpoint for works by ISSN
        url = "https://api.crossref.org/journals/{}/works".format(issn)
        params = {
            "filter": f"from-pub-date:{from_date}",
            "rows": max_results,
            "sort": "published",
            "order": "desc"
        }
        headers = {
            "User-Agent": "ACZMMCQGenerator/1.0 (mailto:admin@example.com)"
        }

        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        items = data.get('message', {}).get('items', [])

        for idx, item in enumerate(items):
            # Get title
            title_list = item.get('title', [])
            title = title_list[0] if title_list else 'No title'

            # Get abstract
            abstract = item.get('abstract', 'No abstract available')
            if abstract and abstract.startswith('<'):
                # Strip HTML tags from abstract
                import re
                abstract = re.sub('<[^<]+?>', '', abstract)

            # Get authors
            authors_list = item.get('author', [])
            authors = []
            for author in authors_list[:5]:
                given = author.get('given', '')
                family = author.get('family', '')
                if given and family:
                    authors.append(f"{given} {family}")
                elif family:
                    authors.append(family)
            authors_str = ", ".join(authors) + ("..." if len(authors_list) > 5 else "")

            # Get publication date
            pub_date_parts = item.get('published-print', {}).get('date-parts', [[]])
            if not pub_date_parts or not pub_date_parts[0]:
                pub_date_parts = item.get('published-online', {}).get('date-parts', [[]])

            year = str(pub_date_parts[0][0]) if pub_date_parts and pub_date_parts[0] else ''

            # Get DOI and URL
            doi = item.get('DOI', '')
            url = f"https://doi.org/{doi}" if doi else item.get('URL', '')

            articles.append({
                "pmid": f"crossref_{idx}",
                "title": title,
                "abstract": abstract if abstract else "No abstract available",
                "authors": authors_str,
                "pub_date": year,
                "journal": j_info['name'],
                "url": url
            })

    except requests.RequestException as e:
        print(f"Error searching CrossRef: {e}")
    except Exception as e:
        print(f"Error parsing CrossRef response: {e}")

    return articles


def get_crossref_article_count(months=12, journal="JHMS"):
    """Get article count from CrossRef API"""
    j_info = JOURNALS.get(journal, JOURNALS["JHMS"])
    issn = j_info.get('issn')

    if not issn:
        return 0

    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=months * 30)
        from_date = start_date.strftime("%Y-%m-%d")

        url = "https://api.crossref.org/journals/{}/works".format(issn)
        params = {
            "filter": f"from-pub-date:{from_date}",
            "rows": 0  # Just get count, no results
        }
        headers = {
            "User-Agent": "ACZMMCQGenerator/1.0 (mailto:admin@example.com)"
        }

        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        return data.get('message', {}).get('total-results', 0)
    except Exception as e:
        print(f"Error getting CrossRef count: {e}")
        return 0


def get_article_count(months=12, journal="JZWM"):
    """Get total article count for a specific journal"""
    j_info = JOURNALS.get(journal, JOURNALS["JZWM"])

    # Route to CrossRef for journals not in PubMed
    if j_info.get('source') == 'crossref':
        return get_crossref_article_count(months, journal)

    # PubMed for other journals
    start_date, end_date = get_date_range(months)

    j_info = JOURNALS.get(journal, JOURNALS["JZWM"])
    journal_query = j_info['query']
    if j_info['exclude']:
        journal_query = f"({journal_query} NOT {j_info['exclude']}[Publication Type])"

    query = f'{journal_query} AND ("{start_date}"[Date - Publication] : "{end_date}"[Date - Publication])'

    search_url = f"{PUBMED_BASE_URL}/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmax": 0,  # Don't need IDs, just the count
        "retmode": "json"
    }

    try:
        response = requests.get(search_url, params=search_params, timeout=30)
        response.raise_for_status()
        search_results = response.json()
        return int(search_results.get("esearchresult", {}).get("count", 0))
    except requests.RequestException as e:
        print(f"Error getting article count: {e}")
        return 0


def search_articles(months=12, journal="all"):
    """Search for articles from specified journal(s) - routes to PubMed or CrossRef"""
    # Handle single journal that uses CrossRef (not in PubMed)
    if journal != "all":
        j_info = JOURNALS.get(journal, JOURNALS["JZWM"])
        if j_info.get('source') == 'crossref':
            return search_crossref(months, journal, max_results=200)

    # Handle "all" - combine PubMed and CrossRef results
    if journal == "all":
        all_articles = []
        # Get PubMed articles
        pubmed_articles = search_pubmed_only(months, "all_pubmed")
        all_articles.extend(pubmed_articles)
        # Get CrossRef articles (JHMS)
        for j_key, j_info in JOURNALS.items():
            if j_info.get('source') == 'crossref':
                crossref_articles = search_crossref(months, j_key, max_results=200)
                all_articles.extend(crossref_articles)
        return all_articles

    # Default to PubMed search
    return search_pubmed_only(months, journal)


def search_pubmed_only(months=12, journal="all_pubmed"):
    """Search PubMed for articles from specified journal(s)"""
    start_date, end_date = get_date_range(months)

    # Build search query based on journal selection
    if journal == "all" or journal == "all_pubmed":
        # Search all PubMed journals (exclude CrossRef journals)
        journal_queries = []
        for j_key, j_info in JOURNALS.items():
            if j_info.get('source') == 'crossref':
                continue  # Skip CrossRef journals (not in PubMed)
            jq = j_info['query']
            if j_info['exclude']:
                jq = f"({jq} NOT {j_info['exclude']}[Publication Type])"
            journal_queries.append(jq)
        journal_query = "(" + " OR ".join(journal_queries) + ")"
    else:
        # Search specific journal
        j_info = JOURNALS.get(journal, JOURNALS["JZWM"])
        journal_query = j_info['query']
        if j_info['exclude']:
            journal_query = f"({journal_query} NOT {j_info['exclude']}[Publication Type])"

    query = f'{journal_query} AND ("{start_date}"[Date - Publication] : "{end_date}"[Date - Publication])'

    # First, search for article IDs
    search_url = f"{PUBMED_BASE_URL}/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmax": 200,
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
2. Provide 5 answer options (A, B, C, D, E) with 1 correct answer and 4 distractors
3. Indicate the correct answer
4. Provide a brief explanation
5. Provide ONE key learning point from this article

Format each question as:
QUESTION [number]:
[Question text]

A) [Option A]
B) [Option B]
C) [Option C]
D) [Option D]
E) [Option E]

CORRECT ANSWER: [Letter]

EXPLANATION: [Brief explanation of why this is correct and why other options are incorrect]

KEY LEARNING POINT: [One major takeaway from this article that board candidates should remember]

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
            "article_authors": article['authors'],
            "article_journal": article['journal'],
            "article_year": article['pub_date'],
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
        "article_authors": article['authors'],
        "article_journal": article['journal'],
        "article_year": article['pub_date'],
        "questions": f"""QUESTION 1:
Based on the study "{article['title']}", which of the following statements is most accurate regarding the findings?

A) [Review the abstract to determine the correct answer]
B) [Alternative interpretation]
C) [Common misconception]
D) [Unrelated finding]
E) [Another distractor]

CORRECT ANSWER: [To be determined after reviewing full article]

EXPLANATION: Please review the full article at {article['url']} to determine the correct answer and explanation.

---
{error_note}"""
    }


def send_email(recipient_email, subject, html_content):
    """Send email using Resend API"""
    resend_api_key = os.getenv('RESEND_API_KEY')
    email_from = os.getenv('EMAIL_FROM', 'ACZM Board Prep <onboarding@resend.dev>')

    print(f"[RESEND] To: {recipient_email}")
    print(f"[RESEND] From: {email_from}")
    print(f"[RESEND] API Key configured: {bool(resend_api_key)}")

    if not resend_api_key:
        return False, "Email not configured. Please set RESEND_API_KEY environment variable."

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": email_from,
                "to": [recipient_email],
                "subject": subject,
                "html": html_content
            },
            timeout=30
        )

        print(f"[RESEND] Response status: {response.status_code}")
        print(f"[RESEND] Response: {response.text}")

        if response.status_code == 200:
            return True, "Email sent successfully"
        else:
            error_msg = response.json().get('message', response.text)
            return False, f"Resend error: {error_msg}"

    except Exception as e:
        print(f"[RESEND] Error: {e}")
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
    return render_template('index.html', journals=JOURNALS)


@app.route('/api/articles')
def get_articles():
    """API endpoint to fetch articles"""
    months = int(request.args.get('months', 12))
    journal = request.args.get('journal', 'all')
    articles = search_articles(months, journal)
    return jsonify({
        "success": True,
        "count": len(articles),
        "articles": articles,
        "date_range": get_date_range(months),
        "journal": journal
    })


@app.route('/api/journal-stats')
def get_journal_stats():
    """API endpoint to get article counts by journal for chart (no limit)"""
    months = int(request.args.get('months', 12))

    stats = {}
    for j_key, j_info in JOURNALS.items():
        # Get count for all journals (CrossRef is fast enough now)
        count = get_article_count(months, j_key)
        stats[j_key] = {
            "name": j_info['name'],
            "abbrev": j_info['abbrev'],
            "count": count
        }

    return jsonify({
        "success": True,
        "stats": stats,
        "months": months
    })


@app.route('/api/generate-mcq', methods=['POST'])
def generate_mcq():
    """API endpoint to generate MCQs"""
    data = request.json
    num_questions = int(data.get('num_questions', 5))
    months = int(data.get('months', 12))
    journal = data.get('journal', 'all')
    filter_word = data.get('filter_word', '').strip().lower()

    # Fetch articles using the selected time period and journal
    articles = search_articles(months, journal)

    if not articles:
        return jsonify({
            "success": False,
            "error": f"No articles found in the selected time period ({months} months)"
        })

    # Filter articles by filter word in title (if provided)
    if filter_word:
        articles = [a for a in articles if filter_word in a['title'].lower()]
        if not articles:
            return jsonify({
                "success": True,
                "mcq_results": [],
                "related_articles": [],
                "message": f"No articles found with '{filter_word}' in title"
            })

    # Filter articles with abstracts
    articles_with_abstracts = [a for a in articles if a['abstract'] != "No abstract available"]

    if not articles_with_abstracts:
        articles_with_abstracts = articles

    # Randomly select articles for questions
    num_articles = min(num_questions, len(articles_with_abstracts))
    selected_articles = random.sample(articles_with_abstracts, num_articles)

    # Get PMIDs of selected articles
    selected_pmids = {a['pmid'] for a in selected_articles}

    # Get related articles (articles not selected, up to 10)
    related_articles = [a for a in articles_with_abstracts if a['pmid'] not in selected_pmids][:10]

    # Generate MCQs in parallel for faster processing
    mcq_results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(generate_mcq_from_article, article, 1): article for article in selected_articles}
        for future in as_completed(futures):
            try:
                mcq = future.result(timeout=60)
                mcq_results.append(mcq)
            except Exception as e:
                article = futures[future]
                mcq_results.append({
                    "article_title": article['title'],
                    "article_pmid": article['pmid'],
                    "article_url": article['url'],
                    "article_authors": article['authors'],
                    "article_journal": article['journal'],
                    "article_year": article['pub_date'],
                    "questions": f"Error generating question: {str(e)}"
                })

    return jsonify({
        "success": True,
        "mcq_results": mcq_results,
        "related_articles": related_articles
    })


@app.route('/api/send-email', methods=['POST'])
def send_mcq_email():
    """API endpoint to send pre-generated MCQs via email"""
    data = request.json
    recipient_email = data.get('recipient_email')
    mcq_results = data.get('mcq_results')

    print(f"[EMAIL] Attempting to send email to: {recipient_email}")
    print(f"[EMAIL] Number of questions: {len(mcq_results) if mcq_results else 0}")

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


@app.route('/api/monthly-update', methods=['POST'])
def monthly_update():
    """API endpoint to generate monthly literature update summary"""
    # Fetch articles from the past month (1 month)
    months = 1

    # Get articles by journal - fetch all in parallel for speed
    journal_articles = {}
    all_articles = []

    def fetch_journal(j_key):
        j_info = JOURNALS[j_key]
        if j_info.get('source') == 'crossref':
            return j_key, search_crossref(months, j_key, max_results=100)
        else:
            return j_key, search_pubmed_only(months, j_key)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_journal, j_key): j_key for j_key in JOURNALS.keys()}
        for future in as_completed(futures):
            try:
                j_key, articles = future.result(timeout=30)
                journal_articles[j_key] = articles
                all_articles.extend(articles)
            except Exception as e:
                j_key = futures[future]
                print(f"Error fetching {j_key}: {e}")
                journal_articles[j_key] = []

    # Count articles by journal
    journal_counts = {j_key: len(articles) for j_key, articles in journal_articles.items()}

    if not all_articles:
        return jsonify({
            "success": True,
            "journal_counts": journal_counts,
            "topics_summary": "No articles found in the past month.",
            "key_points": ""
        })

    # Filter to articles with abstracts for summary
    articles_with_abstracts = [a for a in all_articles if a['abstract'] != "No abstract available"]

    if not articles_with_abstracts:
        articles_with_abstracts = all_articles[:20]  # Use first 20 if none have abstracts
    else:
        # Limit to 30 articles to avoid token limits
        articles_with_abstracts = articles_with_abstracts[:30]

    # Prepare article summaries for Claude
    article_summaries = []
    for a in articles_with_abstracts:
        # Get journal abbreviation
        journal_abbrev = a['journal']
        for j_key, j_info in JOURNALS.items():
            if j_info['name'] == a['journal']:
                journal_abbrev = j_info['abbrev']
                break

        article_summaries.append(f"**{a['title']}** ({journal_abbrev}, {a['pub_date']})\nAbstract: {a['abstract'][:500]}...")

    articles_text = "\n\n---\n\n".join(article_summaries)

    # Generate summary using Claude
    api_key = os.getenv('ANTHROPIC_API_KEY')

    if not api_key:
        return jsonify({
            "success": True,
            "journal_counts": journal_counts,
            "topics_summary": "API key not configured. Cannot generate summary.",
            "key_points": ""
        })

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        prompt = f"""You are reviewing the latest zoological medicine literature from the past month. Below are recent articles from veterinary journals (JZWM = Journal of Zoo and Wildlife Medicine, JAMS = Journal of Avian Medicine and Surgery, JWD = Journal of Wildlife Diseases, JHMS = Journal of Herpetological Medicine and Surgery).

ARTICLES:
{articles_text}

Please provide:

1. **TOPICS SUMMARY**: Organize the literature by major topics/themes (e.g., Infectious Diseases, Anesthesia, Surgery, Nutrition, Reproduction, Conservation Medicine, etc.). For each topic, briefly summarize the key findings from relevant articles. List which journals contributed to each topic.

2. **10 KEY POINTS FOR ACZM EXAMINATION**: Extract the 10 most important clinical or scientific takeaways that would be relevant for the American College of Zoological Medicine board examination. These should be specific, factual points that candidates should remember. Number them 1-10.

Format your response exactly as:

TOPICS SUMMARY:
[Your organized summary by topics]

---

10 KEY POINTS FOR ACZM EXAMINATION:
1. [Point 1]
2. [Point 2]
...
10. [Point 10]"""

        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=3000,
            timeout=120.0,  # 2 minute timeout
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        response_text = message.content[0].text

        # Split response into topics summary and key points
        if "---" in response_text:
            parts = response_text.split("---", 1)
            topics_summary = parts[0].strip()
            key_points = parts[1].strip() if len(parts) > 1 else ""
        else:
            topics_summary = response_text
            key_points = ""

        return jsonify({
            "success": True,
            "journal_counts": journal_counts,
            "topics_summary": topics_summary,
            "key_points": key_points
        })

    except Exception as e:
        print(f"Error generating monthly update: {e}")
        return jsonify({
            "success": False,
            "error": f"Error generating summary: {str(e)}",
            "journal_counts": journal_counts
        })


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)
