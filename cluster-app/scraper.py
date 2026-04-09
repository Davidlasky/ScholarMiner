#!/usr/bin/env python3
"""
Google Scholar scraper.

Uses SerpAPI to get IEEE paper links from Google Scholar (avoids IP blocking),
then uses Scrapling Fetcher to get full abstracts from each IEEE Xplore page.
"""

import logging
import os
import random
import re
import time
from contextlib import ExitStack
from html import unescape
import json
from urllib.parse import parse_qs, urlencode, urlparse

from serpapi import GoogleScholarSearch
from scrapling.fetchers import DynamicSession, Fetcher, FetcherSession, StealthySession

logger = logging.getLogger(__name__)

MAX_PAGES = int(os.environ.get("SCHOLAR_MAX_PAGES", "15"))
RESULTS_PER_PAGE = int(os.environ.get("SCHOLAR_RESULTS_PER_PAGE", "10"))
SCRAPLING_FETCH_MODE = os.environ.get("SCRAPLING_FETCH_MODE", "auto").lower()
STATIC_TIMEOUT_SECONDS = int(os.environ.get("SCRAPLING_STATIC_TIMEOUT_SECONDS", "30"))
BROWSER_TIMEOUT_MS = int(os.environ.get("SCRAPLING_BROWSER_TIMEOUT_MS", "45000"))
REQUEST_RETRIES = int(os.environ.get("SCRAPLING_RETRIES", "2"))
PAGE_DELAY_MIN_SECONDS = float(os.environ.get("SCHOLAR_DELAY_MIN_SECONDS", "0.5"))
PAGE_DELAY_MAX_SECONDS = float(os.environ.get("SCHOLAR_DELAY_MAX_SECONDS", "1.5"))
IEEE_TIMEOUT_SECONDS = int(os.environ.get("IEEE_TIMEOUT_SECONDS", "30"))
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")

VALID_FETCH_MODES = {"auto", "fetcher", "dynamic", "stealthy"}
RESULT_SELECTORS = (
    "div.gs_r.gs_or.gs_scl",
    "div.gs_ri",
)
RESULT_WAIT_SELECTOR = "div.gs_r.gs_or.gs_scl, div.gs_ri"
BLOCK_MARKERS = (
    "our systems have detected unusual traffic",
    "sorry/index",
    "prove you're human",
    "captcha",
)


class ScholarScrapeError(RuntimeError):
    """Raised when Google Scholar cannot be scraped reliably."""


def _normalize_whitespace(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _selector_text(selector):
    text_parts = selector.css("::text").getall()
    return _normalize_whitespace(
        " ".join(part.strip() for part in text_parts if part and part.strip())
    )


def _build_page_url(scholar_url, page_number):
    start = page_number * RESULTS_PER_PAGE
    parsed = urlparse(scholar_url)
    params = parse_qs(parsed.query)
    params["start"] = [str(start)]
    new_query = urlencode(params, doseq=True)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"


def _select_results(page):
    for selector in RESULT_SELECTORS:
        results = page.css(selector)
        if results:
            return results
    return []


def _page_text(page):
    body = getattr(page, "body", b"")
    if isinstance(body, bytes):
        encoding = getattr(page, "encoding", None) or "utf-8"
        return body.decode(encoding, errors="replace")
    return str(body)


def _decode_json_string(value):
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.encode("utf-8").decode("unicode_escape", errors="replace")


def _extract_longest_metadata_string(body_text, field_name):
    pattern = rf'"{re.escape(field_name)}":"((?:\\.|[^"\\])*)"'
    matches = re.findall(pattern, body_text)
    candidates = [
        unescape(_normalize_whitespace(_decode_json_string(match)))
        for match in matches
        if match not in {"true", "false"}
    ]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _extract_metadata_int(body_text, field_name):
    match = re.search(rf'"{re.escape(field_name)}":(?:")?(\d+)(?:")?', body_text)
    if not match:
        return None
    return int(match.group(1))


def _is_blocked_page(page):
    status = getattr(page, "status", None)
    if status in {403, 429}:
        return True

    page_text = _page_text(page).lower()
    return any(marker in page_text for marker in BLOCK_MARKERS)


def _fetch_order():
    if SCRAPLING_FETCH_MODE not in VALID_FETCH_MODES:
        logger.warning(
            "Unknown SCRAPLING_FETCH_MODE=%s, defaulting to auto", SCRAPLING_FETCH_MODE
        )
        return ("fetcher", "dynamic", "stealthy")

    if SCRAPLING_FETCH_MODE == "auto":
        return ("fetcher", "dynamic", "stealthy")

    return (SCRAPLING_FETCH_MODE,)


def _get_static_session(stack, session_cache):
    if "fetcher" not in session_cache:
        session_cache["fetcher"] = stack.enter_context(
            FetcherSession(
                impersonate=["chrome", "edge", "firefox"],
                stealthy_headers=True,
                follow_redirects=True,
                timeout=STATIC_TIMEOUT_SECONDS,
                retries=REQUEST_RETRIES,
            )
        )
    return session_cache["fetcher"]


def _get_dynamic_session(stack, session_cache):
    if "dynamic" not in session_cache:
        session_cache["dynamic"] = stack.enter_context(
            DynamicSession(
                headless=True,
                disable_resources=True,
                network_idle=True,
                timeout=BROWSER_TIMEOUT_MS,
                wait_selector=RESULT_WAIT_SELECTOR,
                retries=REQUEST_RETRIES,
                locale="en-US",
            )
        )
    return session_cache["dynamic"]


def _get_stealthy_session(stack, session_cache):
    if "stealthy" not in session_cache:
        session_cache["stealthy"] = stack.enter_context(
            StealthySession(
                headless=True,
                disable_resources=True,
                network_idle=True,
                timeout=max(BROWSER_TIMEOUT_MS, 60000),
                wait_selector=RESULT_WAIT_SELECTOR,
                retries=REQUEST_RETRIES,
                locale="en-US",
                block_webrtc=True,
                hide_canvas=True,
            )
        )
    return session_cache["stealthy"]


def _fetch_with_strategy(page_url, stack, session_cache):
    attempts = []

    for fetcher_name in _fetch_order():
        try:
            if fetcher_name == "fetcher":
                page = _get_static_session(stack, session_cache).get(page_url)
            elif fetcher_name == "dynamic":
                page = _get_dynamic_session(stack, session_cache).fetch(page_url)
            else:
                page = _get_stealthy_session(stack, session_cache).fetch(page_url)
        except Exception as exc:
            attempts.append(f"{fetcher_name}: {exc}")
            logger.warning("Scholar fetch via %s failed: %s", fetcher_name, exc)
            continue

        if _is_blocked_page(page):
            status = getattr(page, "status", "unknown")
            attempts.append(f"{fetcher_name}: blocked response ({status})")
            logger.warning(
                "Scholar fetch via %s returned a blocked page (status=%s)",
                fetcher_name,
                status,
            )
            continue

        logger.info("Fetched Scholar page with %s", fetcher_name)
        return page

    raise ScholarScrapeError(
        f"Failed to fetch Google Scholar page after trying {', '.join(_fetch_order())}: {'; '.join(attempts)}"
    )


def _has_next_page(page):
    return bool(page.css('button[aria-label="Next"], a[aria-label="Next"]'))


def _sleep_between_pages():
    if PAGE_DELAY_MAX_SECONDS <= 0:
        return

    sleep_time = random.uniform(PAGE_DELAY_MIN_SECONDS, PAGE_DELAY_MAX_SECONDS)
    logger.info("Sleeping %.2f seconds before next Scholar page", sleep_time)
    time.sleep(sleep_time)


def _fetch_ieee_metadata(url):
    try:
        page = Fetcher.get(
            url,
            impersonate=["chrome", "edge", "firefox"],
            stealthy_headers=True,
            timeout=IEEE_TIMEOUT_SECONDS,
            retries=REQUEST_RETRIES,
            headers={"Referer": "https://scholar.google.com/"},
        )
        if getattr(page, "status", None) != 200:
            logger.warning(
                "IEEE fetch returned non-200 status for %s: %s",
                url,
                getattr(page, "status", "unknown"),
            )
            return {"abstract": "", "citations": None}

        body_text = _page_text(page)
        abstract = _extract_longest_metadata_string(body_text, "abstract")
        citations = _extract_metadata_int(body_text, "citationCount")
        if citations is None:
            citations = _extract_metadata_int(body_text, "citationCountPaper")

        if not abstract:
            logger.warning("No IEEE abstract metadata found for %s", url)
        if citations is None:
            logger.warning("No IEEE citation metadata found for %s", url)

        return {"abstract": abstract, "citations": citations}
    except Exception as exc:
        logger.warning("Failed to fetch IEEE metadata for %s: %s", url, exc)
        return {"abstract": "", "citations": None}


def extract_paper_info(result):
    """
    Extract paper metadata from a single Google Scholar result node.
    """
    try:
        title_links = result.css("h3.gs_rt a")
        if not title_links:
            return None

        link = title_links[0]
        url = link.attrib.get("href", "")
        if "ieeexplore.ieee.org" not in url:
            return None

        title = _selector_text(link)
        if not title:
            return None

        ieee_id_match = re.search(r"/document/(\d+)", url)
        ieee_id = (
            ieee_id_match.group(1) if ieee_id_match else url.rstrip("/").split("/")[-1]
        )

        scholar_citations = 0
        footer_div = result.css("div.gs_fl")
        if footer_div:
            cited_match = re.search(r"Cited by (\d+)", _selector_text(footer_div[0]))
            if cited_match:
                scholar_citations = int(cited_match.group(1))

        snippet_div = result.css("div.gs_rs")
        snippet_abstract = _selector_text(snippet_div[0]) if snippet_div else ""
        ieee_metadata = _fetch_ieee_metadata(url)
        abstract = ieee_metadata["abstract"] or snippet_abstract
        citations = (
            ieee_metadata["citations"]
            if ieee_metadata["citations"] is not None
            else scholar_citations
        )

        return {
            "title": title,
            "url": url,
            "ieee_id": ieee_id,
            "citations": citations,
            "abstract": abstract,
        }
    except Exception as exc:
        logger.warning("Failed to extract Scholar result: %s", exc)
        return None


def parse_google_scholar(scholar_url, max_pages=MAX_PAGES):
    """
    Use SerpAPI to get IEEE paper links from Google Scholar.
    Avoids IP blocking issues with direct scraping.
    Then fetches full abstracts from each IEEE Xplore page using Scrapling.
    """
    if not SERPAPI_KEY:
        raise ScholarScrapeError("SERPAPI_KEY environment variable is not set")

    papers = []
    seen_ids = set()

    # Extract search query from the Scholar URL
    parsed = urlparse(scholar_url)
    params = parse_qs(parsed.query)
    query = params.get("q", [""])[0]

    logger.info("Using SerpAPI to search Scholar for: %s", query)

    for page_number in range(max_pages):
        search_params = {
            "engine": "google_scholar",
            "q": query,
            "api_key": SERPAPI_KEY,
            "num": RESULTS_PER_PAGE,
            "start": page_number * RESULTS_PER_PAGE,
        }

        try:
            search = GoogleScholarSearch(search_params)
            results = search.get_dict()
        except Exception as e:
            logger.error("SerpAPI request failed on page %d: %s", page_number + 1, str(e))
            break

        organic = results.get("organic_results", [])
        if not organic:
            logger.info("No more results after page %d", page_number + 1)
            break

        page_count = 0
        for result in organic:
            link = result.get("link", "")
            if "ieeexplore.ieee.org" not in link:
                continue

            ieee_id_match = re.search(r'/document/(\d+)', link)
            if not ieee_id_match:
                continue

            ieee_id = ieee_id_match.group(1)
            if ieee_id in seen_ids:
                continue

            seen_ids.add(ieee_id)

            # Use SerpAPI snippet as fallback abstract
            # Full abstract will be fetched from IEEE in scrape_and_collect
            cited_by = result.get("inline_links", {}).get("cited_by", {})
            citations = cited_by.get("total", 0) if isinstance(cited_by, dict) else 0

            papers.append({
                "title":     result.get("title", ""),
                "url":       link,
                "ieee_id":   ieee_id,
                "citations": citations,
                "abstract":  result.get("snippet", ""),  # fallback
            })
            page_count += 1

        logger.info("Collected %d IEEE papers from Scholar page %d", page_count, page_number + 1)

        # Check if there is a next page
        if not results.get("serpapi_pagination", {}).get("next"):
            logger.info("No more pages after page %d", page_number + 1)
            break

        time.sleep(1)

    logger.info("Total IEEE papers found from Scholar: %d", len(papers))
    return papers


def scrape_and_collect(scholar_url):
    """
    Main entry point.

    1. Uses SerpAPI to get IEEE paper links from Google Scholar
    2. Uses Scrapling Fetcher to get full abstracts from each IEEE Xplore page
    3. Falls back to SerpAPI snippet if IEEE fetch fails

    Returns paper dictionaries with keys:
        ieee_id, title, citations, abstract, url
    """
    logger.info("Starting scrape for URL: %s", scholar_url)

    # Step 1: Get paper list from SerpAPI
    papers = parse_google_scholar(scholar_url)

    if not papers:
        raise ScholarScrapeError("No IEEE papers were found in the Google Scholar results.")

    # Step 2: Fetch full abstracts from IEEE Xplore for each paper
    for i, paper in enumerate(papers):
        logger.info(
            "Fetching IEEE abstract %d/%d: %s",
            i + 1, len(papers), paper["title"][:50]
        )

        ieee_metadata = _fetch_ieee_metadata(paper["url"])

        # Use full abstract if available, otherwise keep SerpAPI snippet
        if ieee_metadata.get("abstract"):
            paper["abstract"] = ieee_metadata["abstract"]

        # Update citations with real count from IEEE if available
        if ieee_metadata.get("citations") is not None:
            paper["citations"] = ieee_metadata["citations"]

    complete_papers = [p for p in papers if p.get("abstract")]
    logger.info(
        "Scraping complete: %d/%d papers have abstracts",
        len(complete_papers), len(papers)
    )

    if not complete_papers:
        raise ScholarScrapeError(
            "Google Scholar returned papers, but none included abstract text."
        )

    return complete_papers