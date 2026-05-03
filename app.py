"""
ACZM MCQ Generator
Extracts articles from veterinary journals and generates ACZM-style MCQs
"""

import os
import random
import re
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


def _strip_markdown_emphasis(text):
    """Remove markdown emphasis markers (**, *, __, _) that look like noise
    in plain-text output. Bullet dashes and numbered lists are left alone.
    """
    if not text:
        return text
    # Bold first, then italics, for both * and _ flavors.
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)', r'\1', text, flags=re.DOTALL)
    # Convert leading "* " bullets to "- " (do not touch "1. " numbered lists).
    text = re.sub(r'(?m)^(\s*)\*\s+', r'\1- ', text)
    return text


def get_date_range(months=12):
    """Get date range for the specified number of months.

    Adds a small lookback buffer so that articles indexed near the
    boundary aren't missed when a monthly run is 1-2 days late.
    """
    end_date = datetime.now()
    # 7-day buffer absorbs late runs and indexing lag for quarterly journals.
    buffer_days = 7
    start_date = end_date - timedelta(days=months * 30 + buffer_days)
    return start_date.strftime("%Y/%m/%d"), end_date.strftime("%Y/%m/%d")


def search_crossref(months=12, journal="JHMS", max_results=200):
    """Search CrossRef API for articles from a journal by ISSN"""
    j_info = JOURNALS.get(journal, JOURNALS["JHMS"])
    issn = j_info.get('issn')

    if not issn:
        print(f"No ISSN configured for {journal}")
        return []

    # Calculate date range with buffer (matches get_date_range).
    end_date = datetime.now()
    start_date = end_date - timedelta(days=months * 30 + 7)
    from_date = start_date.strftime("%Y-%m-%d")

    articles = []
    try:
        # CrossRef API endpoint for works by ISSN.
        # Filter on index-date (when CrossRef ingested the record) instead of
        # pub-date so articles whose stated publication date predates the
        # rolling window are still picked up if they were just indexed.
        url = "https://api.crossref.org/journals/{}/works".format(issn)
        params = {
            "filter": f"from-index-date:{from_date}",
            "rows": max_results,
            "sort": "indexed",
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
        start_date = end_date - timedelta(days=months * 30 + 7)
        from_date = start_date.strftime("%Y-%m-%d")

        url = "https://api.crossref.org/journals/{}/works".format(issn)
        params = {
            "filter": f"from-index-date:{from_date}",
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

    # Filter on Entry Date (when PubMed indexed the article) rather than
    # Publication Date so quarterly journals like JAMS aren't dropped when
    # their issue date falls outside the rolling window.
    query = f'{journal_query} AND ("{start_date}"[Date - Entry] : "{end_date}"[Date - Entry])'

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

    # Filter on Entry Date (when PubMed indexed the article) rather than
    # Publication Date so quarterly journals like JAMS aren't dropped when
    # their issue date falls outside the rolling window.
    query = f'{journal_query} AND ("{start_date}"[Date - Entry] : "{end_date}"[Date - Entry])'

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

IMPORTANT CONSTRAINT: Do NOT ask questions that require memorizing specific numeric values such as drug doses, laboratory reference ranges, blood values, measurement thresholds, or any other precise numbers. Questions should test conceptual understanding, clinical reasoning, species-specific biology, and diagnostic/treatment principles — not the ability to recall exact figures.

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
            model="claude-opus-4-7",
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


def find_related_articles_ai(mcq_results, candidate_articles):
    """Use AI to find articles most related to the generated questions"""
    if not candidate_articles or not mcq_results:
        return candidate_articles[:10]

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return candidate_articles[:10]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        # Extract question topics from MCQ results
        question_summaries = []
        for mcq in mcq_results:
            question_summaries.append(f"- {mcq['article_title']}")

        # Prepare candidate articles list
        candidates_text = []
        for idx, article in enumerate(candidate_articles[:30]):  # Limit to 30 candidates
            candidates_text.append(f"{idx + 1}. {article['title']}")

        prompt = f"""Based on these MCQ question topics:
{chr(10).join(question_summaries)}

From the following articles, select the 10 most related articles that would help students learn more about these topics. Return ONLY the numbers of the 10 most relevant articles, separated by commas (e.g., "3, 7, 12, 1, 15, 8, 22, 5, 19, 11").

Articles:
{chr(10).join(candidates_text)}

Return only the numbers, nothing else:"""

        message = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=100,
            timeout=30.0,
            messages=[{"role": "user", "content": prompt}]
        )

        # Parse response to get article indices
        response = message.content[0].text.strip()
        indices = [int(x.strip()) - 1 for x in response.split(',') if x.strip().isdigit()]

        # Get the selected articles in order
        related = []
        for idx in indices:
            if 0 <= idx < len(candidate_articles):
                related.append(candidate_articles[idx])
            if len(related) >= 10:
                break

        return related if related else candidate_articles[:10]

    except Exception as e:
        print(f"Error finding related articles: {e}")
        return candidate_articles[:10]


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
                "pool_size": 0,
                "message": f"No articles found with '{filter_word}' in title"
            })

    # Filter articles with abstracts
    articles_with_abstracts = [a for a in articles if a['abstract'] != "No abstract available"]

    if not articles_with_abstracts:
        articles_with_abstracts = articles

    # Pool size = number of candidate articles questions were randomized from.
    pool_size = len(articles_with_abstracts)

    # Randomly select articles for questions, with JWD weighted at 75% of other journals
    num_articles = min(num_questions, len(articles_with_abstracts))
    def is_jwd(article):
        j = article.get('journal', '').lower()
        return 'wildl' in j and 'dis' in j

    weights = [0.75 if is_jwd(a) else 1.0 for a in articles_with_abstracts]
    total = sum(weights)
    norm_weights = [w / total for w in weights]
    selected_articles = random.choices(articles_with_abstracts, weights=norm_weights, k=num_articles)
    # Deduplicate while preserving order (random.choices can repeat)
    seen = set()
    unique_selected = []
    for a in selected_articles:
        key = a['pmid']
        if key not in seen:
            seen.add(key)
            unique_selected.append(a)
    # If deduplication reduced count, fill from remaining articles
    if len(unique_selected) < num_articles:
        remaining = [a for a in articles_with_abstracts if a['pmid'] not in seen]
        remaining_weights = [0.75 if is_jwd(a) else 1.0 for a in remaining]
        rem_total = sum(remaining_weights)
        if rem_total > 0 and remaining:
            rem_norm = [w / rem_total for w in remaining_weights]
            extras = random.choices(remaining, weights=rem_norm,
                                    k=num_articles - len(unique_selected))
            unique_selected.extend(extras)
    selected_articles = unique_selected[:num_articles]

    # Get PMIDs of selected articles
    selected_pmids = {a['pmid'] for a in selected_articles}

    # Get candidate articles (articles not selected)
    candidate_articles = [a for a in articles_with_abstracts if a['pmid'] not in selected_pmids]

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

    # Use AI to find truly related articles
    related_articles = find_related_articles_ai(mcq_results, candidate_articles)

    return jsonify({
        "success": True,
        "mcq_results": mcq_results,
        "related_articles": related_articles,
        "pool_size": pool_size
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
    # Fetch articles from the past month (1 month) - use same function as UI
    months = 1
    all_articles = search_articles(months, "all")

    # Count articles by journal using flexible matching
    journal_counts = {j_key: 0 for j_key in JOURNALS.keys()}
    for article in all_articles:
        article_journal = (article.get('journal') or '').lower()
        if 'zoo' in article_journal and 'wildl' in article_journal:
            journal_counts['JZWM'] += 1
        elif 'avian' in article_journal:
            journal_counts['JAMS'] += 1
        elif 'wildl' in article_journal and 'dis' in article_journal:
            journal_counts['JWD'] += 1
        elif 'herpetol' in article_journal:
            journal_counts['JHMS'] += 1

    if not all_articles:
        return jsonify({
            "success": True,
            "journal_counts": journal_counts,
            "topics_summary": "No articles found in the past month.",
            "key_points": "",
            "articles": []
        })

    # Filter to articles with abstracts for summary
    articles_with_abstracts = [a for a in all_articles if a['abstract'] != "No abstract available"]
    articles_without_abstracts = [a for a in all_articles if a['abstract'] == "No abstract available"]

    if not articles_with_abstracts:
        # Edge case: nothing has abstracts. Fall back to titles only so the
        # request doesn't fail outright; the model will note the limitation.
        articles_with_abstracts = all_articles[:20]
        articles_without_abstracts = []
    else:
        # Round-robin reorder by journal so the [N] reference numbering and
        # citation distribution interleave the journals (no cap: every
        # abstract-bearing article is sent to the model).
        def _journal_key(article):
            j = (article.get('journal') or '').lower()
            if 'avian' in j:
                return 'JAMS'
            if 'zoo' in j and 'wildl' in j:
                return 'JZWM'
            if 'wildl' in j and 'dis' in j:
                return 'JWD'
            if 'herpetol' in j:
                return 'JHMS'
            return 'OTHER'

        from collections import OrderedDict
        buckets = OrderedDict()
        for a in articles_with_abstracts:
            buckets.setdefault(_journal_key(a), []).append(a)

        interleaved = []
        while any(buckets.values()):
            for k in list(buckets.keys()):
                if buckets[k]:
                    interleaved.append(buckets[k].pop(0))
        articles_with_abstracts = interleaved

    # Prepare numbered article summaries for Claude. The numbering becomes the
    # reference list shown at the bottom of the report; the model is asked to
    # cite using these same [N] tokens.
    article_summaries = []
    for idx, a in enumerate(articles_with_abstracts, start=1):
        # Get journal abbreviation
        journal_abbrev = a['journal']
        for j_key, j_info in JOURNALS.items():
            if j_info['name'] == a['journal']:
                journal_abbrev = j_info['abbrev']
                break

        # Send the full abstract; only truncate pathologically long ones so
        # the prompt stays well-bounded. Most abstracts are well under 4000
        # chars so this rarely triggers.
        abstract_text = a['abstract']
        if len(abstract_text) > 4000:
            abstract_text = abstract_text[:4000] + '...'

        article_summaries.append(
            f"[{idx}] {a['title']} ({journal_abbrev}, {a['pub_date']})\n"
            f"Abstract: {abstract_text}"
        )

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

Each article is preceded by its reference number in square brackets, e.g. [1], [2]. You MUST cite the supporting article(s) for every claim using these same bracketed reference numbers. Use [3] for a single source and [1, 4, 7] for multiple. Place the citation at the end of the sentence or clause it supports.

ARTICLES:
{articles_text}

Please provide a substantive, exam-oriented review with two sections.

SECTION 1 - TOPICS SUMMARY
Organize the literature by major topics/themes (e.g., Infectious Diseases, Anesthesia, Surgery, Nutrition, Reproduction, Conservation Medicine, Pathology, Toxicology, Imaging, Pharmacology). For each topic:
- Write 2-4 sentences synthesizing the key findings across the relevant articles, not just one-liners.
- Include species/taxa studied, the clinical or scientific question, and the practical takeaway.
- Mention which journals contributed (e.g., "JZWM, JWD").
- Where relevant, briefly note pathophysiology, diagnostic approach, treatment, or epidemiology that an ACZM candidate would be expected to understand.
- Cite the supporting articles with bracketed reference numbers, e.g., [2, 5].
Aim for 4-8 topics covering the breadth of the literature, with enough depth to actually study from.

SECTION 2 - 12 KEY POINTS FOR ACZM EXAMINATION
Extract the 12 most important clinical or scientific takeaways for the American College of Zoological Medicine board examination. Each point should be a complete, specific, exam-ready statement (2-3 sentences) that explains what to know AND why it matters clinically, and MUST end with the bracketed reference number(s) of the source article(s), e.g. "... resulting in higher anesthetic mortality. [4]". Avoid vague generalities. Number the points 1-12.

CRITICAL FORMATTING RULES:
- Output plain text only. Do NOT use any markdown formatting.
- Do NOT use asterisks (*) for bold, italics, or bullet points anywhere in your response.
- Do NOT use underscores (_) for emphasis.
- Use UPPERCASE for section headers and topic names instead of bold.
- Use simple dashes (-) or numbers for lists, not asterisks.
- Only cite reference numbers that actually appear in the ARTICLES list above. Do not invent numbers.

Format your response exactly as:

TOPICS SUMMARY:

TOPIC NAME 1 (contributing journals):
2-4 sentences synthesizing the findings, with species, clinical question, practical takeaway, and bracketed citations like [1, 3].

TOPIC NAME 2 (contributing journals):
2-4 sentences ... [2, 5].

(continue for all topics)

---

12 KEY POINTS FOR ACZM EXAMINATION:
1. 2-3 sentence exam-ready point ending with [N] or [N, M].
2. 2-3 sentence exam-ready point ending with [N] or [N, M].
...
12. 2-3 sentence exam-ready point ending with [N] or [N, M]."""

        message = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=6000,
            timeout=180.0,  # 3 minute timeout for the longer response
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        response_text = message.content[0].text

        # Safety net: strip any stray markdown asterisks/underscores the
        # model may still emit. We keep dashes and digits intact.
        response_text = _strip_markdown_emphasis(response_text)

        # Cost FYI for the operator. claude-opus-4-7 standard pricing:
        # $5/M input tokens, $25/M output tokens.
        usage = getattr(message, 'usage', None)
        input_tokens = getattr(usage, 'input_tokens', 0) if usage else 0
        output_tokens = getattr(usage, 'output_tokens', 0) if usage else 0
        cost_usd = (input_tokens * 5.0 / 1_000_000) + (output_tokens * 25.0 / 1_000_000)

        # Split response into topics summary and key points by anchoring on
        # the explicit KEY POINTS header. Splitting on a "---" separator is
        # unreliable now that the richer prompt produces longer topic blocks
        # (the model often uses --- between topics as a natural break, which
        # would clobber the second section).
        kp_match = re.search(
            r'(?im)^\s*(?:\d+\s+)?KEY POINTS FOR ACZM EXAMINATION\s*:?',
            response_text,
        )
        if kp_match:
            topics_summary = response_text[:kp_match.start()].strip()
            # Drop a trailing "---" separator from the topics summary if one
            # is present immediately before the key points header.
            topics_summary = re.sub(r'\n\s*-{3,}\s*$', '', topics_summary).strip()
            # The frontend renders its own heading for the key points list,
            # so strip the model's heading line to avoid duplication.
            key_points = response_text[kp_match.end():].lstrip(' :\n').rstrip()
        else:
            topics_summary = response_text.strip()
            key_points = ""

        # The reference list returned to the client must mirror the numbered
        # list fed to the model so the [N] citations in the summary align
        # with the entries shown at the bottom of the report.
        articles_list = [{
            "title": a['title'],
            "authors": a['authors'],
            "journal": a['journal'],
            "year": a['pub_date'],
            "url": a['url']
        } for a in articles_with_abstracts]

        # Articles whose abstracts weren't available (common for JHMS via
        # CrossRef). They can't be summarized but we still surface them so
        # readers can find them manually.
        articles_no_abstract = [{
            "title": a['title'],
            "authors": a['authors'],
            "journal": a['journal'],
            "year": a['pub_date'],
            "url": a['url']
        } for a in articles_without_abstracts]

        return jsonify({
            "success": True,
            "journal_counts": journal_counts,
            "topics_summary": topics_summary,
            "key_points": key_points,
            "articles": articles_list,
            "articles_without_abstracts": articles_no_abstract,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost_usd, 4)
            }
        })

    except Exception as e:
        print(f"Error generating monthly update: {e}")
        return jsonify({
            "success": False,
            "error": f"Error generating summary: {str(e)}",
            "journal_counts": journal_counts,
            "articles": []
        })


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)
