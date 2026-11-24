"""Policy-gated literature providers (spec §8.3, §12.1-12.3, App. A4 SearchResult).

Only providers named in the run's permissions are callable; a misspelled provider is denied,
never widened to "all sources" (E08). Redirects outside the provider's host allowlist fail.
Queries must carry public concepts only: long verbatim spans of the manuscript are refused.
Failures are recorded as provider_status=failed, never as success with an empty list.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Optional

from ..core.errors import SRError
from ..models.common import new_id, now_utc, sha256_bytes
from . import ingest

USER_AGENT = "scholarly-review/0.1 (research tool)"
TIMEOUT = 20
HOST_TIMEOUT = {"export.arxiv.org": 35}  # the arXiv API is often slow rather than down
PROVIDERS = {
    "openalex": {"hosts": {"api.openalex.org"}},
    "arxiv": {"hosts": {"export.arxiv.org", "arxiv.org"}},
}


class _AllowlistRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname
        if host not in self.hosts:
            raise SRError("policy_denied", f"redirect to non-allowlisted host {host} blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url: str, hosts: set[str], max_bytes: int) -> bytes:
    host = urllib.parse.urlparse(url).hostname
    if host not in hosts:
        raise SRError("policy_denied", f"host {host} is not allowlisted for this provider")
    opener = urllib.request.build_opener(_AllowlistRedirect(hosts))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=HOST_TIMEOUT.get(host, TIMEOUT)) as resp:
            data = resp.read(max_bytes + 1)
    except SRError:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = (e.headers.get("Retry-After") or "").strip() if e.headers else ""
            hint = f"; retry after {wait} s" if wait.isdigit() else ""
            raise SRError("provider_rate_limited", f"rate limited by {host}{hint}", retryable=True) from e
        raise SRError("provider_failed", f"HTTP {e.code} from {host}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise SRError("provider_timeout", f"network failure contacting {host}: {e}", retryable=True) from e
    if len(data) > max_bytes:
        raise SRError("resource_exhausted", f"response larger than the remaining download budget ({max_bytes} bytes)")
    return data


Fetcher = Callable[[str, set, int], bytes]


def check_disclosure(query: str, docmap: dict, title: Optional[str], disclosure: str) -> None:
    if disclosure == "none":
        raise SRError("policy_denied", "external search is disabled for this run (search_disclosure=none)")
    q = ingest.normalize(query).lower()
    if disclosure == "public_concepts_only":
        if title and len(title) > 12 and ingest.normalize(title).lower() in q:
            raise SRError("policy_denied", "query contains the manuscript title; use public technical concepts only")
        words = q.split()
        if len(words) >= 8:
            joined = " ".join(ingest.normalize(b["text"]).lower() for b in docmap["blocks"][:400])
            for i in range(len(words) - 7):
                if " ".join(words[i:i + 8]) in joined:
                    raise SRError("policy_denied", "query copies a distinctive manuscript span (8+ words); "
                                  "rephrase as public concepts")


def _openalex(query: str, limit: int, cutoff: Optional[str], fetch: Fetcher, max_bytes: int) -> dict:
    params = {"search": query, "per-page": str(min(limit, 25))}
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    raw = json.loads(fetch(url, PROVIDERS["openalex"]["hosts"], max_bytes))
    if not isinstance(raw, dict) or "results" not in raw:
        raise SRError("provider_failed", "unexpected OpenAlex response shape")
    records = []
    for w in raw["results"]:
        abstract = None
        inv = w.get("abstract_inverted_index")
        if isinstance(inv, dict):
            pos = sorted((p, word) for word, ps in inv.items() for p in ps)
            abstract = " ".join(word for _, word in pos)
        ids = {k: v for k, v in (w.get("ids") or {}).items() if v}
        records.append({
            "source_id": "openalex:" + str(w.get("id", "")).rsplit("/", 1)[-1],
            "title": w.get("title"),
            "authors": [a.get("author", {}).get("display_name") for a in w.get("authorships", [])][:20] or None,
            "first_public_date": w.get("publication_date"),
            "version_public_date": w.get("publication_date"),
            "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            "identifiers": ids, "links": [x for x in [w.get("doi"), w.get("id")] if x],
            "access_level": "abstract" if abstract else "metadata", "abstract": abstract,
        })
    total = (raw.get("meta") or {}).get("count")
    complete = total is not None and total <= len(records)
    return {"records": records, "complete": complete,
            "truncation": None if complete else f"returned {len(records)} of {total}",
            "next_cursor": None if complete else "page=2"}


ATOM = "{http://www.w3.org/2005/Atom}"


def _arxiv(query: str, limit: int, cutoff: Optional[str], fetch: Fetcher, max_bytes: int) -> dict:
    # arXiv treats space-separated words as OR; AND the terms so results stay on topic.
    stop = {"a", "an", "the", "to", "of", "for", "and", "or", "in", "on", "with", "without", "by", "via"}
    terms = [re.sub(r"[^\w\-]", "", t) for t in query.split() if t.lower() not in stop]
    expr = " AND ".join(f"all:{t}" for t in terms if t)
    params = {"search_query": expr, "start": "0", "max_results": str(min(limit, 25)),
              "sortBy": "relevance"}
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    root = ET.fromstring(fetch(url, PROVIDERS["arxiv"]["hosts"], max_bytes))
    records = []
    for e in root.findall(f"{ATOM}entry"):
        aid_url = (e.findtext(f"{ATOM}id") or "").strip()
        m = re.search(r"abs/([^v]+)(v\d+)?$", aid_url)
        if not m:
            continue
        base, ver = m.group(1), m.group(2)
        records.append({
            "source_id": "arxiv:" + base,
            "title": re.sub(r"\s+", " ", e.findtext(f"{ATOM}title") or "").strip() or None,
            "authors": [a.findtext(f"{ATOM}name") for a in e.findall(f"{ATOM}author")] or None,
            "first_public_date": (e.findtext(f"{ATOM}published") or "")[:10] or None,
            "version_public_date": (e.findtext(f"{ATOM}updated") or "")[:10] or None,
            "venue": "arXiv", "identifiers": {"arxiv": base, "version": ver},
            "links": [aid_url], "access_level": "abstract",
            "abstract": re.sub(r"\s+", " ", e.findtext(f"{ATOM}summary") or "").strip() or None,
        })
    total_txt = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    total = int(total_txt) if total_txt and total_txt.isdigit() else None
    complete = total is not None and total <= len(records)
    return {"records": records, "complete": complete,
            "truncation": None if complete else f"returned {len(records)} of {total if total is not None else 'unknown'}",
            "next_cursor": None if complete else f"start={len(records)}"}


SEARCHERS = {"openalex": _openalex, "arxiv": _arxiv}


def search(provider: str, query: str, limit: int, cutoff: Optional[str], fetch: Fetcher = http_get,
           max_bytes: int = 5_000_000) -> dict:
    """Return an App. A4 SearchResult dict. Provider errors become provider_status=failed."""
    retrieved = now_utc()
    try:
        res = SEARCHERS[provider](query, limit, cutoff, fetch, max_bytes)
    except SRError as e:
        if e.category == "policy_denied":
            raise
        return {"records": [], "provider_status": "failed", "error_reason": f"{e.category}: {e.message}",
                "complete": False, "truncation": None, "next_cursor": None, "queries": [query],
                "retrieved_at": retrieved}
    except (ET.ParseError, json.JSONDecodeError, ValueError) as e:
        return {"records": [], "provider_status": "failed", "error_reason": f"parse_failed: {e}",
                "complete": False, "truncation": None, "next_cursor": None, "queries": [query],
                "retrieved_at": retrieved}
    for r in res["records"]:
        r["after_cutoff"] = None if not cutoff or not r["first_public_date"] else r["first_public_date"] > cutoff
    return {"records": res["records"], "provider_status": "success" if res["complete"] else "partial",
            "error_reason": None, "complete": res["complete"], "truncation": res["truncation"],
            "next_cursor": res["next_cursor"], "queries": [query], "retrieved_at": retrieved}


def fetch_arxiv_pdf(arxiv_id: str, version: Optional[str], fetch: Fetcher, max_bytes: int) -> bytes:
    if not re.fullmatch(r"[0-9]{4}\.[0-9]{4,5}|[a-z\-]+(\.[A-Z]{2})?/[0-9]{7}", arxiv_id):
        raise SRError("invalid_input", f"not an arXiv identifier: {arxiv_id}")
    url = f"https://arxiv.org/pdf/{arxiv_id}{version or ''}"
    return fetch(url, PROVIDERS["arxiv"]["hosts"], max_bytes)


def new_search_id() -> str:
    return new_id("search")


def digest(data: bytes) -> str:
    return sha256_bytes(data)
