"""
ACZM MCQ Generator
Extracts articles from veterinary journals and generates ACZM-style MCQs
"""

import calendar
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

# Model used by the original MCQ generator.
MCQ_MODEL = "claude-opus-4-7"
# Model used for the monthly resident study material.
STUDY_MODEL = "claude-opus-5"

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


def _first_text(message):
    """Extract the first text block from a Claude response.

    Models that think (Opus 5 thinks by default) return thinking blocks
    alongside the answer, so content[0] is not necessarily the text.
    A safety refusal returns no text block at all.
    """
    if getattr(message, 'stop_reason', None) == 'refusal':
        raise RuntimeError("The model declined to answer this request.")
    for block in getattr(message, 'content', []) or []:
        if getattr(block, 'type', None) == 'text':
            return block.text
    return ""


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


def get_calendar_month_bounds(year, month):
    """Return (first_moment, last_moment) datetimes for a calendar month.

    Month length comes from calendar.monthrange, so the window is the real
    nominal month (28, 29, 30 or 31 days) rather than a 30-day approximation.
    February is therefore correct in both common and leap years.
    """
    last_day = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, 0, 0, 0)
    end = datetime(year, month, last_day, 23, 59, 59)
    return start, end


def previous_calendar_month(today=None):
    """Return (year, month) of the last complete calendar month."""
    today = today or datetime.now()
    if today.month == 1:
        return today.year - 1, 12
    return today.year, today.month - 1


def parse_month_param(value):
    """Parse a 'YYYY-MM' string into (year, month).

    Anything missing or malformed falls back to the last complete
    calendar month, which is the sensible default for a monthly digest.
    """
    if value:
        match = re.match(r'^\s*(\d{4})-(\d{1,2})\s*$', str(value))
        if match:
            year, month = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12 and 1900 <= year <= 2999:
                return year, month
    return previous_calendar_month()


def month_label(year, month):
    """Human-readable month name, e.g. 'July 2026'."""
    return f"{calendar.month_name[month]} {year}"


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
    # Hard client-side cutoff for safety: anything older than this is rejected
    # even if CrossRef returns it. We allow ~30 extra days of slack on top of
    # the rolling window to absorb late metadata registration.
    cutoff_date = end_date - timedelta(days=months * 30 + 30)

    articles = []
    try:
        # Filter on created-date (when CrossRef first received the record,
        # close to the article's actual publication time). Earlier code used
        # index-date, but CrossRef re-indexes old records whenever publishers
        # touch their metadata, which surfaced years-old articles in the
        # monthly window. created-date is stable and is what we actually want.
        url = "https://api.crossref.org/journals/{}/works".format(issn)
        params = {
            "filter": f"from-created-date:{from_date}",
            "rows": max_results,
            "sort": "created",
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
            # Defense in depth: drop anything whose created-date is older
            # than the cutoff, in case CrossRef's filter behaves loosely.
            created_parts = item.get('created', {}).get('date-parts', [[]])
            if created_parts and created_parts[0]:
                cp = created_parts[0]
                try:
                    created_dt = datetime(
                        cp[0],
                        cp[1] if len(cp) > 1 else 1,
                        cp[2] if len(cp) > 2 else 1,
                    )
                    if created_dt < cutoff_date:
                        continue
                except (TypeError, ValueError):
                    pass

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
            "filter": f"from-created-date:{from_date}",
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


def build_pubmed_journal_query(journal="all_pubmed"):
    """Build the journal portion of a PubMed query string."""
    if journal in ("all", "all_pubmed"):
        journal_queries = []
        for j_key, j_info in JOURNALS.items():
            if j_info.get('source') == 'crossref':
                continue  # Not indexed in PubMed
            jq = j_info['query']
            if j_info['exclude']:
                jq = f"({jq} NOT {j_info['exclude']}[Publication Type])"
            journal_queries.append(jq)
        return "(" + " OR ".join(journal_queries) + ")"

    j_info = JOURNALS.get(journal, JOURNALS["JZWM"])
    journal_query = j_info['query']
    if j_info['exclude']:
        journal_query = f"({journal_query} NOT {j_info['exclude']}[Publication Type])"
    return journal_query


def search_pubmed_range(start_dt, end_dt, journal="all_pubmed", max_results=200):
    """Search PubMed between two explicit datetimes (inclusive).

    Unlike search_pubmed_only, which uses a rolling "N x 30 days" window,
    this takes exact boundaries so a caller can request a true calendar
    month.
    """
    start_date = start_dt.strftime("%Y/%m/%d")
    end_date = end_dt.strftime("%Y/%m/%d")
    journal_query = build_pubmed_journal_query(journal)

    # Entry Date (when PubMed indexed the article) rather than Publication
    # Date, for the same reason as the rolling-window search: quarterly
    # journals would otherwise fall outside a one-month window.
    query = f'{journal_query} AND ("{start_date}"[Date - Entry] : "{end_date}"[Date - Entry])'

    search_url = f"{PUBMED_BASE_URL}/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "pub_date"
    }

    try:
        response = requests.get(search_url, params=search_params, timeout=30)
        response.raise_for_status()
        id_list = response.json().get("esearchresult", {}).get("idlist", [])
        if not id_list:
            return []
        return fetch_article_details(id_list)
    except requests.RequestException as e:
        print(f"Error searching PubMed range: {e}")
        return []


def search_crossref_range(start_dt, end_dt, journal="JHMS", max_results=200):
    """Search CrossRef between two explicit dates (inclusive) by ISSN."""
    j_info = JOURNALS.get(journal, JOURNALS["JHMS"])
    issn = j_info.get('issn')
    if not issn:
        return []

    from_date = start_dt.strftime("%Y-%m-%d")
    until_date = end_dt.strftime("%Y-%m-%d")

    articles = []
    try:
        url = "https://api.crossref.org/journals/{}/works".format(issn)
        params = {
            "filter": f"from-created-date:{from_date},until-created-date:{until_date}",
            "rows": max_results,
            "sort": "created",
            "order": "desc"
        }
        headers = {"User-Agent": "ACZMMCQGenerator/1.0 (mailto:admin@example.com)"}

        response = requests.get(url, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        items = response.json().get('message', {}).get('items', [])

        for idx, item in enumerate(items):
            # Defense in depth: enforce the window client-side too.
            created_parts = item.get('created', {}).get('date-parts', [[]])
            if created_parts and created_parts[0]:
                cp = created_parts[0]
                try:
                    created_dt = datetime(
                        cp[0],
                        cp[1] if len(cp) > 1 else 1,
                        cp[2] if len(cp) > 2 else 1,
                    )
                    if created_dt < start_dt.replace(hour=0, minute=0, second=0) or created_dt > end_dt:
                        continue
                except (TypeError, ValueError):
                    pass

            articles.append(_crossref_item_to_article(item, idx, j_info))

    except requests.RequestException as e:
        print(f"Error searching CrossRef range: {e}")
    except Exception as e:
        print(f"Error parsing CrossRef range response: {e}")

    return articles


def _crossref_item_to_article(item, idx, j_info):
    """Normalize a CrossRef work item into our article dict shape."""
    title_list = item.get('title', [])
    title = title_list[0] if title_list else 'No title'

    abstract = item.get('abstract', 'No abstract available')
    if abstract and abstract.startswith('<'):
        abstract = re.sub('<[^<]+?>', '', abstract)

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

    pub_date_parts = item.get('published-print', {}).get('date-parts', [[]])
    if not pub_date_parts or not pub_date_parts[0]:
        pub_date_parts = item.get('published-online', {}).get('date-parts', [[]])
    year = str(pub_date_parts[0][0]) if pub_date_parts and pub_date_parts[0] else ''

    doi = item.get('DOI', '')
    url = f"https://doi.org/{doi}" if doi else item.get('URL', '')

    return {
        "pmid": f"crossref_{doi or idx}",
        "title": title,
        "abstract": abstract if abstract else "No abstract available",
        "authors": authors_str,
        "pub_date": year,
        "journal": j_info['name'],
        "url": url
    }


def search_articles_range(start_dt, end_dt, journal="all", max_results=200):
    """Search all configured sources between two explicit datetimes."""
    if journal != "all":
        j_info = JOURNALS.get(journal, JOURNALS["JZWM"])
        if j_info.get('source') == 'crossref':
            return search_crossref_range(start_dt, end_dt, journal, max_results)
        return search_pubmed_range(start_dt, end_dt, journal, max_results)

    all_articles = list(search_pubmed_range(start_dt, end_dt, "all_pubmed", max_results))
    for j_key, j_info in JOURNALS.items():
        if j_info.get('source') == 'crossref':
            all_articles.extend(
                search_crossref_range(start_dt, end_dt, j_key, max_results)
            )
    return all_articles


def fetch_article_details(pmid_list):
    """Fetch detailed information for articles by PMID.

    Requests are split into chunks so that large result sets (e.g. a
    three-year window) don't blow up the efetch URL, and the chunks are
    fetched in parallel. Original PMID ordering is preserved.
    """
    if not pmid_list:
        return []

    chunk_size = 100
    chunks = [pmid_list[i:i + chunk_size] for i in range(0, len(pmid_list), chunk_size)]

    if len(chunks) == 1:
        return _fetch_article_chunk(chunks[0])

    results = [[] for _ in chunks]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_article_chunk, chunk): i
                   for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            i = futures[future]
            try:
                results[i] = future.result(timeout=60)
            except Exception as e:
                print(f"Error fetching article chunk {i}: {e}")
                results[i] = []

    return [article for chunk_result in results for article in chunk_result]


def _fetch_article_chunk(pmid_list):
    """Fetch and parse a single batch of PMIDs."""
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


def generate_mcq_from_article(article, num_questions=1, focus_hint=None,
                              model=MCQ_MODEL, effort=None):
    """Generate ACZM-style MCQ from an article using Claude API.

    focus_hint steers the question toward a particular angle. It is used
    when more than one question has to come from the same article so the
    two questions don't end up testing the same fact.
    """
    api_key = os.getenv('ANTHROPIC_API_KEY')

    if not api_key:
        return generate_fallback_mcq(article, num_questions, "ANTHROPIC_API_KEY environment variable is not set")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        focus_line = f"\nFOCUS FOR THIS QUESTION: {focus_hint}\n" if focus_hint else ""

        prompt = f"""Based on the following veterinary article abstract, generate {num_questions} multiple choice question(s) in ACZM (American College of Zoological Medicine) board examination style.

Article Title: {article['title']}

Abstract: {article['abstract']}
{focus_line}
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

        # max_tokens is a ceiling, not a cost: keep it generous so that
        # models which think by default (Opus 5) have room for the
        # reasoning plus the full question.
        create_kwargs = {
            "model": model,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        }
        if effort:
            create_kwargs["output_config"] = {"effort": effort}

        message = client.messages.create(**create_kwargs)

        usage = getattr(message, 'usage', None)
        return {
            "article_title": article['title'],
            "article_pmid": article['pmid'],
            "article_url": article['url'],
            "article_authors": article['authors'],
            "article_journal": article['journal'],
            "article_year": article['pub_date'],
            "questions": _first_text(message),
            "input_tokens": getattr(usage, 'input_tokens', 0) if usage else 0,
            "output_tokens": getattr(usage, 'output_tokens', 0) if usage else 0
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


# ---------------------------------------------------------------------------
# Monthly study material for residents
# ---------------------------------------------------------------------------

# Model pricing used for the cost FYI shown in the UI (claude-opus-5
# standard pricing, USD per million tokens).
PRICE_INPUT_PER_MTOK = 5.0
PRICE_OUTPUT_PER_MTOK = 25.0

# Angles used when a month has fewer articles than requested questions, so a
# second question drawn from the same article tests something different.
FOCUS_HINTS = [
    None,
    "Focus on a different aspect of this study than its headline result - for "
    "example the diagnostic approach, the methodology, or the species biology "
    "involved.",
    "Focus on the clinical, epidemiologic, or conservation implications of this "
    "study rather than on its primary finding.",
]


def _journal_bucket(article):
    """Map an article's journal name onto one of our four journal keys."""
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


def count_articles_by_journal(articles):
    """Count articles per configured journal."""
    counts = {j_key: 0 for j_key in JOURNALS.keys()}
    for article in articles:
        key = _journal_bucket(article)
        if key in counts:
            counts[key] += 1
    return counts


def interleave_by_journal(articles):
    """Round-robin articles across journals.

    Keeps the [N] reference numbering (and therefore the citation spread in
    the generated summary) from clustering on whichever journal happened to
    publish the most that month.
    """
    from collections import OrderedDict
    buckets = OrderedDict()
    for article in articles:
        buckets.setdefault(_journal_bucket(article), []).append(article)

    interleaved = []
    while any(buckets.values()):
        for key in list(buckets.keys()):
            if buckets[key]:
                interleaved.append(buckets[key].pop(0))
    return interleaved


def journal_abbrev_for(article):
    """Short journal label for an article dict."""
    for j_info in JOURNALS.values():
        if j_info['name'] == article.get('journal'):
            return j_info['abbrev']
    return article.get('journal') or ''


def build_numbered_article_text(articles):
    """Render articles as the numbered [N] block fed to the model."""
    summaries = []
    for idx, article in enumerate(articles, start=1):
        abstract_text = article['abstract']
        if len(abstract_text) > 4000:
            abstract_text = abstract_text[:4000] + '...'
        summaries.append(
            f"[{idx}] {article['title']} ({journal_abbrev_for(article)}, {article['pub_date']})\n"
            f"Abstract: {abstract_text}"
        )
    return "\n\n---\n\n".join(summaries)


def generate_literature_summary(articles, label, num_key_points=10):
    """Summarize a month of literature and extract exam-ready key points.

    Returns a dict with 'topics_summary', 'key_points' and token usage.
    """
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return {
            "topics_summary": "API key not configured. Cannot generate summary.",
            "key_points": "",
            "input_tokens": 0,
            "output_tokens": 0
        }

    articles_text = build_numbered_article_text(articles)

    prompt = f"""You are reviewing the zoological medicine literature published during {label}. Below are the articles indexed that month from veterinary journals (JZWM = Journal of Zoo and Wildlife Medicine, JAMS = Journal of Avian Medicine and Surgery, JWD = Journal of Wildlife Diseases, JHMS = Journal of Herpetological Medicine and Surgery).

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

SECTION 2 - {num_key_points} KEY LEARNING POINTS
Extract the {num_key_points} most important clinical or scientific takeaways from this month's literature for the American College of Zoological Medicine board examination. Each point should be a complete, specific, exam-ready statement (2-3 sentences) that explains what to know AND why it matters clinically, and MUST end with the bracketed reference number(s) of the source article(s), e.g. "... resulting in higher anesthetic mortality. [4]". Avoid vague generalities. Number the points 1-{num_key_points}.

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

{num_key_points} KEY LEARNING POINTS:
1. 2-3 sentence exam-ready point ending with [N] or [N, M].
2. 2-3 sentence exam-ready point ending with [N] or [N, M].
...
{num_key_points}. 2-3 sentence exam-ready point ending with [N] or [N, M]."""

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=STUDY_MODEL,
        # Headroom for thinking (on by default on Opus 5) plus the report.
        max_tokens=16000,
        output_config={"effort": "high"},
        timeout=240.0,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = _strip_markdown_emphasis(_first_text(message))

    # Anchor on the key-points header rather than on a "---" separator: the
    # model uses --- between topic blocks too, which would truncate wrongly.
    kp_match = re.search(
        r'(?im)^\s*(?:\d+\s+)?KEY (?:LEARNING )?POINTS(?: FOR ACZM EXAMINATION)?\s*:?',
        response_text,
    )
    if kp_match:
        topics_summary = response_text[:kp_match.start()].strip()
        topics_summary = re.sub(r'\n\s*-{3,}\s*$', '', topics_summary).strip()
        key_points = response_text[kp_match.end():].lstrip(' :\n').rstrip()
    else:
        topics_summary = response_text.strip()
        key_points = ""

    # Strip the model's own "TOPICS SUMMARY:" header; the client renders one.
    topics_summary = re.sub(r'(?i)^\s*TOPICS SUMMARY\s*:?\s*\n+', '', topics_summary)

    usage = getattr(message, 'usage', None)
    return {
        "topics_summary": topics_summary,
        "key_points": key_points,
        "input_tokens": getattr(usage, 'input_tokens', 0) if usage else 0,
        "output_tokens": getattr(usage, 'output_tokens', 0) if usage else 0
    }


def select_articles_for_questions(articles, num_questions):
    """Pick the articles that questions will be written from.

    Distinct articles are drawn first (weighted so JWD contributes at 75% of
    the other journals, matching the main generator). If the month yielded
    fewer articles than requested questions, articles are reused with a
    different focus hint so the extra questions aren't near-duplicates.
    Returns a list of (article, focus_hint) tuples.
    """
    if not articles:
        return []

    def is_jwd(article):
        j = (article.get('journal') or '').lower()
        return 'wildl' in j and 'dis' in j

    pool = list(articles)
    weights = [0.75 if is_jwd(a) else 1.0 for a in pool]

    chosen = []
    while pool and len(chosen) < num_questions:
        pick = random.choices(range(len(pool)), weights=weights, k=1)[0]
        chosen.append(pool.pop(pick))
        weights.pop(pick)

    assignments = [(article, None) for article in chosen]

    round_idx = 1
    while len(assignments) < num_questions:
        hint = FOCUS_HINTS[min(round_idx, len(FOCUS_HINTS) - 1)]
        for article in chosen:
            if len(assignments) >= num_questions:
                break
            assignments.append((article, hint))
        round_idx += 1

    return assignments


def generate_mcq_batch(assignments, max_workers=10):
    """Generate one MCQ per assignment in parallel, preserving order."""
    results = [None] * len(assignments)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(generate_mcq_from_article, article, 1, hint,
                            STUDY_MODEL, "medium"): idx
            for idx, (article, hint) in enumerate(assignments)
        }
        for future in as_completed(futures):
            idx = futures[future]
            article, _ = assignments[idx]
            try:
                results[idx] = future.result(timeout=120)
            except Exception as e:
                results[idx] = {
                    "article_title": article['title'],
                    "article_pmid": article['pmid'],
                    "article_url": article['url'],
                    "article_authors": article['authors'],
                    "article_journal": article['journal'],
                    "article_year": article['pub_date'],
                    "questions": f"Error generating question: {str(e)}",
                    "input_tokens": 0,
                    "output_tokens": 0
                }

    return [r for r in results if r is not None]


def find_related_articles_recent(topic_titles, years=3, exclude_keys=None,
                                 limit=10, max_candidates=300):
    """Pick further reading from the past `years` of the tracked journals.

    Candidates are pulled from the whole window (not just the digest month)
    and ranked by the model against the topics covered this month.
    """
    exclude_keys = exclude_keys or set()

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=int(365.25 * years))

    candidates = search_articles_range(start_dt, end_dt, "all", max_results=400)

    # Drop the articles the questions were written from, plus duplicates.
    seen = set()
    filtered = []
    for article in candidates:
        key = article.get('url') or article.get('pmid')
        if not key or key in exclude_keys or key in seen:
            continue
        seen.add(key)
        filtered.append(article)

    if not filtered:
        return []

    # Keep the prompt bounded. A random sample spreads the choice across the
    # full three years instead of favouring the most recently indexed.
    if len(filtered) > max_candidates:
        shortlist = random.sample(filtered, max_candidates)
    else:
        shortlist = filtered

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key or not topic_titles:
        return shortlist[:limit]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        topics_block = "\n".join(f"- {t}" for t in topic_titles[:40])
        candidates_block = "\n".join(
            f"{idx + 1}. {a['title']} ({journal_abbrev_for(a)}, {a['pub_date']})"
            for idx, a in enumerate(shortlist)
        )

        prompt = f"""This month's zoological medicine literature covered these articles/topics:
{topics_block}

From the list below (articles published in the past {years} years), select the {limit} that an ACZM board candidate should read to build background and context around those topics. Prefer articles that are thematically related but NOT duplicates of the list above, and spread the selection across different topics and journals.

CANDIDATE ARTICLES:
{candidates_block}

Return ONLY the numbers of the {limit} selected articles, separated by commas (e.g., "3, 7, 12, 1, 15, 8, 22, 5, 19, 11"). Nothing else."""

        message = client.messages.create(
            model=STUDY_MODEL,
            # Ranking a candidate list is a shallow task: keep effort low so
            # this doesn't dominate the request's wall clock.
            max_tokens=4000,
            output_config={"effort": "low"},
            timeout=120.0,
            messages=[{"role": "user", "content": prompt}]
        )

        response = _first_text(message).strip()
        indices = [int(x.strip()) - 1 for x in re.split(r'[,\s]+', response) if x.strip().isdigit()]

        related = []
        picked = set()
        for idx in indices:
            if 0 <= idx < len(shortlist) and idx not in picked:
                picked.add(idx)
                related.append(shortlist[idx])
            if len(related) >= limit:
                break

        # Top up if the model returned fewer than requested.
        for article in shortlist:
            if len(related) >= limit:
                break
            if article not in related:
                related.append(article)

        return related[:limit]

    except Exception as e:
        print(f"Error selecting related articles: {e}")
        return shortlist[:limit]


def _article_ref(article):
    """Trim an article dict down to what the client needs for citations."""
    return {
        "title": article['title'],
        "authors": article['authors'],
        "journal": article['journal'],
        "journal_abbrev": journal_abbrev_for(article),
        "year": article['pub_date'],
        "url": article['url']
    }


@app.route('/api/monthly-study-material', methods=['POST'])
def monthly_study_material():
    """Generate the full monthly study package for residents.

    Covers one true calendar month (28/29/30/31 days as appropriate) and
    returns: a literature summary, MCQs, key learning points, and further
    reading drawn from the past three years.
    """
    data = request.json or {}
    year, month = parse_month_param(data.get('month'))
    num_questions = int(data.get('num_questions', 10))
    num_key_points = int(data.get('num_key_points', 10))
    num_related = int(data.get('num_related', 10))
    related_years = int(data.get('related_years', 3))

    start_dt, end_dt = get_calendar_month_bounds(year, month)
    label = month_label(year, month)
    days_in_month = calendar.monthrange(year, month)[1]

    period = {
        "month": f"{year:04d}-{month:02d}",
        "label": label,
        "start": start_dt.strftime("%Y-%m-%d"),
        "end": end_dt.strftime("%Y-%m-%d"),
        "days": days_in_month
    }

    all_articles = search_articles_range(start_dt, end_dt, "all", max_results=300)
    journal_counts = count_articles_by_journal(all_articles)

    if not all_articles:
        return jsonify({
            "success": True,
            "period": period,
            "journal_counts": journal_counts,
            "topics_summary": f"No articles were indexed from the tracked journals during {label}.",
            "key_points": "",
            "mcq_results": [],
            "references": [],
            "articles_without_abstracts": [],
            "related_articles": []
        })

    with_abstracts = [a for a in all_articles if a['abstract'] != "No abstract available"]
    without_abstracts = [a for a in all_articles if a['abstract'] == "No abstract available"]

    if not with_abstracts:
        # Nothing has an abstract this month. Fall back to titles so the
        # request still returns something useful.
        with_abstracts = all_articles
        without_abstracts = []
    else:
        with_abstracts = interleave_by_journal(with_abstracts)

    assignments = select_articles_for_questions(with_abstracts, num_questions)
    exclude_keys = {a.get('url') or a.get('pmid') for a, _ in assignments}
    topic_titles = [a['title'] for a in with_abstracts]

    # The three long-running pieces are independent, so run them together:
    # the request is bounded by the slowest one rather than their sum.
    summary_result = None
    mcq_results = []
    related_articles = []
    summary_error = None

    with ThreadPoolExecutor(max_workers=3) as executor:
        f_summary = executor.submit(
            generate_literature_summary, with_abstracts, label, num_key_points
        )
        f_mcq = executor.submit(generate_mcq_batch, assignments)
        f_related = executor.submit(
            find_related_articles_recent, topic_titles, related_years,
            exclude_keys, num_related
        )

        try:
            summary_result = f_summary.result(timeout=240)
        except Exception as e:
            print(f"Error generating literature summary: {e}")
            summary_error = str(e)

        try:
            mcq_results = f_mcq.result(timeout=240)
        except Exception as e:
            print(f"Error generating MCQs: {e}")

        try:
            related_articles = f_related.result(timeout=240)
        except Exception as e:
            print(f"Error gathering related articles: {e}")

    if summary_result is None:
        summary_result = {
            "topics_summary": f"Error generating summary: {summary_error}"
            if summary_error else "Summary unavailable.",
            "key_points": "",
            "input_tokens": 0,
            "output_tokens": 0
        }

    input_tokens = summary_result.get('input_tokens', 0) + sum(
        m.get('input_tokens', 0) for m in mcq_results
    )
    output_tokens = summary_result.get('output_tokens', 0) + sum(
        m.get('output_tokens', 0) for m in mcq_results
    )
    cost_usd = ((input_tokens * PRICE_INPUT_PER_MTOK / 1_000_000)
                + (output_tokens * PRICE_OUTPUT_PER_MTOK / 1_000_000))

    return jsonify({
        "success": True,
        "period": period,
        "journal_counts": journal_counts,
        "article_count": len(all_articles),
        "topics_summary": summary_result['topics_summary'],
        "key_points": summary_result['key_points'],
        "mcq_results": mcq_results,
        # Numbering here mirrors the [N] citations in the summary and the
        # key learning points.
        "references": [_article_ref(a) for a in with_abstracts],
        "articles_without_abstracts": [_article_ref(a) for a in without_abstracts],
        "related_articles": [_article_ref(a) for a in related_articles],
        "related_years": related_years,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost_usd, 4)
        }
    })


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000)
