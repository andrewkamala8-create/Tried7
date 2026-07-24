import os
import time
import random
from urllib.parse import urlparse, quote_plus

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def build_session(retries=2):
    s = requests.Session()
    retry_cfg = Retry(
        total=retries,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_cfg)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def looks_like_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def search_web(query: str, timeout: int, retries: int, max_results: int = 5):
    session = build_session(retries)
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    resp = session.get(search_url, headers=headers(), timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    for result in soup.select(".result"):
        link_tag = result.select_one(".result__a")
        snippet_tag = result.select_one(".result__snippet")
        if not link_tag or not link_tag.get("href"):
            continue
        results.append({
            "title": link_tag.get_text(strip=True),
            "url": link_tag["href"],
            "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
        })
        if len(results) >= max_results:
            break
    return results


def scrape_page(url: str, timeout: int, retries: int, mode: str = "text"):
    session = build_session(retries)
    resp = session.get(url, headers=headers(), timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    result = {
        "url": url,
        "title": title,
        "status_code": resp.status_code,
    }

    if mode == "html":
        result["html"] = str(soup)[:20000]
    elif mode == "links":
        links, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href in seen or href.startswith("javascript:"):
                continue
            seen.add(href)
            links.append({"url": href, "text": a.get_text(strip=True)[:150]})
        result["links"] = links[:200]
        result["count"] = len(links)
    else:  # text (default)
        result["text"] = soup.get_text(separator="\n", strip=True)[:8000]

    return result


@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "usage": {
            "/scrape?url=<url-or-plain-words>&mode=text|html|links&timeout=&retries=":
                "scrapes a page. If the input isn't a real URL, it auto-searches and scrapes the top result.",
            "/search?q=<query>": "returns search results (title/url/snippet) without scraping",
        }
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})


@app.route("/search")
def search_route():
    q = request.args.get("q") or request.args.get("query")
    timeout = int(request.args.get("timeout", 20))
    retries = int(request.args.get("retries", 2))
    max_results = int(request.args.get("max_results", 5))

    if not q:
        return jsonify({"error": "missing q param, e.g. /search?q=yahoo finance"}), 400

    try:
        results = search_web(q, timeout=timeout, retries=retries, max_results=max_results)
        return jsonify({"query": q, "count": len(results), "results": results})
    except requests.exceptions.Timeout:
        return jsonify({"error": "search timed out", "detail": f"tried for {timeout}s, try &timeout=30"}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "search failed", "detail": str(e)}), 502


@app.route("/scrape")
def scrape_route():
    raw_input = request.args.get("url")
    mode = request.args.get("mode", "text")
    timeout = int(request.args.get("timeout", 20))
    retries = int(request.args.get("retries", 2))

    if not raw_input:
        return jsonify({"error": "missing url param, e.g. /scrape?url=https://example.com or /scrape?url=yahoo finance"}), 400

    try:
        if looks_like_url(raw_input):
            result = scrape_page(raw_input, timeout, retries, mode)
            result["mode"] = "direct_url"
            return jsonify(result)
        else:
            hits = search_web(raw_input, timeout, retries, max_results=1)
            if not hits:
                return jsonify({"error": "no search results found", "query": raw_input}), 404
            top = hits[0]
            result = scrape_page(top["url"], timeout, retries, mode)
            result["mode"] = "search_then_scrape"
            result["matched_query"] = raw_input
            result["search_title"] = top["title"]
            return jsonify(result)

    except requests.exceptions.Timeout:
        return jsonify({
            "error": "request timed out",
            "detail": f"tried for {timeout}s — target site may be slow. Try &timeout=30 or &timeout=45"
        }), 504
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": "connection failed", "detail": str(e)}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "request failed", "detail": str(e)}), 502
    except Exception as e:
        return jsonify({"error": "unexpected error", "detail": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
