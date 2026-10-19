#!/usr/bin/env python3
"""Auditable biomedical retrieval adapters; Python 3.10+, standard library only.

Network responses are cached by URL (including query), never by inferred findings.
Search functions return (canonical_records, coverage_logs). Unknown dates remain
available for later screening; retrieval completeness describes API pagination,
not the recall of the search strategy or availability of article full texts.
"""
from __future__ import annotations

import calendar
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib import error, parse, request
import xml.etree.ElementTree as ET

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
TODAY = lambda: dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


class RetrievalError(RuntimeError):
    """Recoverable transport, parsing, ambiguity or coverage error."""
    def __init__(self, code, message, *, url=None, status=None, retryable=False):
        super().__init__(message)
        self.code, self.url, self.status, self.retryable = code, url, status, retryable

    def as_dict(self):
        # Query URLs can contain a configured API key. Keep secrets out of logs.
        url = self.url
        if url:
            parts = parse.urlsplit(url)
            params = [(k, "[REDACTED]" if k.lower() in {"api_key", "key", "token"} else v)
                      for k, v in parse.parse_qsl(parts.query)]
            url = parse.urlunsplit(parts._replace(query=parse.urlencode(params)))
        return {"code": self.code, "message": str(self), "url": url,
                "status": self.status, "retryable": self.retryable}


class Client:
    """Small persistent HTTP cache, explicit errors, bounded retries and pacing.

    Optional NCBI_API_KEY and NCBI_EMAIL environment settings are read only for
    NCBI requests. Anonymous NCBI requests are spaced below 3 requests/second.
    Cache responses have no expiry: use refresh=True for a fresh evidence run.
    """
    def __init__(self, cache_dir, refresh=False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self._last_request = {}
        self.max_attempts = 4
        self.timeout = 45

    def _get(self, url, params=None):
        params = dict(params or {})
        if parse.urlsplit(url).hostname == "eutils.ncbi.nlm.nih.gov":
            params.setdefault("tool", "gene-research-review")
            if os.environ.get("NCBI_EMAIL"):
                params.setdefault("email", os.environ["NCBI_EMAIL"])
            if os.environ.get("NCBI_API_KEY"):
                params.setdefault("api_key", os.environ["NCBI_API_KEY"])
        if params:
            url += ("&" if "?" in url else "?") + parse.urlencode(sorted(params.items()), doseq=True)
        cache_path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".response")
        if cache_path.exists() and not self.refresh:
            return cache_path.read_text(encoding="utf-8")
        host = parse.urlsplit(url).hostname or ""
        interval = 0.12 if "api_key" in params else (0.36 if host == "eutils.ncbi.nlm.nih.gov" else 0.12)
        for attempt in range(self.max_attempts):
            delay = interval - (time.monotonic() - self._last_request.get(host, 0))
            if delay > 0:
                time.sleep(delay)
            try:
                self._last_request[host] = time.monotonic()
                req = request.Request(url, headers={"User-Agent": "gene-research-review/1.0 (research literature retrieval)",
                                                    "Accept": "application/json, application/xml, text/plain"})
                with request.urlopen(req, timeout=self.timeout) as response:
                    text = response.read().decode(response.headers.get_content_charset() or "utf-8")
                # Atomic replace permits interrupted or concurrent runs safely.
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                                 delete=False, prefix=".response-") as handle:
                    handle.write(text)
                    temporary = handle.name
                os.replace(temporary, cache_path)
                return text
            except error.HTTPError as exc:
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                failure = RetrievalError("http_error", f"HTTP {exc.code}", url=url,
                                         status=exc.code, retryable=retryable)
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                wait = min(float(retry_after), 60) if retry_after.isdigit() else min(2 ** attempt, 16)
                exc.close()
            except (error.URLError, TimeoutError, OSError, UnicodeError) as exc:
                failure = RetrievalError("network_error", type(exc).__name__, url=url, retryable=True)
                wait = min(2 ** attempt, 16)
            if not failure.retryable or attempt + 1 == self.max_attempts:
                raise failure
            time.sleep(wait)
        raise AssertionError("unreachable")

    def get_text(self, url, params=None):
        return self._get(url, params)

    def get_json(self, url, params=None):
        text = self._get(url, params)
        try:
            result = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise RetrievalError("invalid_json", "Response is not valid JSON", url=url) from exc
        if isinstance(result, dict) and result.get("error"):
            raise RetrievalError("api_error", str(result["error"]), url=url)
        return result


def _text(node):
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _dedupe(values):
    return list(dict.fromkeys(v for v in values if v not in (None, "")))


def _doi(value):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value or "").strip(), flags=re.I).lower().rstrip(" .;")


def _date(value):
    """Normalize a genuine source date to ISO precision; never fill missing parts."""
    if not value:
        return ""
    value = str(value).strip()
    match = re.match(r"^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?", value)
    if not match:
        return ""
    year, month, day = match.groups()
    try:
        if month:
            if not 1 <= int(month) <= 12:
                return ""
            if day:
                dt.date(int(year), int(month), int(day))
        else:
            if not 1 <= int(year) <= 9999:
                return ""
    except ValueError:
        return ""
    return year + (f"-{int(month):02}" if month else "") + (f"-{int(day):02}" if day else "")


def _date_bounds(value):
    value = _date(value)
    if not value:
        return None
    pieces = list(map(int, value.split("-")))
    year = pieces[0]
    if len(pieces) == 1:
        return dt.date(year, 1, 1), dt.date(year, 12, 31)
    month = pieces[1]
    if len(pieces) == 2:
        return dt.date(year, month, 1), dt.date(year, month, calendar.monthrange(year, month)[1])
    exact = dt.date(*pieces)
    return exact, exact


def _xml_date(node):
    if node is None:
        return ""
    year, month, day = node.findtext("Year"), node.findtext("Month"), node.findtext("Day")
    if not year:
        return _date(node.findtext("MedlineDate", ""))
    if month and not month.isdigit():
        names = {name.lower(): i for i, name in enumerate(calendar.month_abbr) if name}
        month = str(names.get(month[:3].lower(), ""))
    return _date(year + ("-" + month if month else "") + ("-" + day if month and day else ""))


def _window(start, end):
    lower, upper = _date_bounds(start), _date_bounds(end)
    if not lower or not upper or lower[0] > upper[1]:
        raise ValueError("start/end must be valid ordered ISO dates")
    return lower[0], min(upper[1], TODAY())


def _in_window(record, lower, upper):
    bounds = _date_bounds(record.get("publication_date"))
    return bounds is None or (bounds[0] <= upper and bounds[1] >= lower)


def _canonical(**fields):
    record = {"paper_id": "", "title": "", "authors": [], "year": None, "doi": "", "pmid": "", "pmcid": "",
              "publication_date": "", "date_precision": "unknown", "dates": {}, "abstract": "",
              "source_depth": "metadata", "is_preprint": False, "publication_types": [],
              "related_dois": [], "sources": [], "urls": [], "provenance": []}
    record.update(fields)
    record["doi"] = _doi(record["doi"])
    record["publication_date"] = _date(record["publication_date"])
    date = record["publication_date"]
    record["year"] = int(date[:4]) if date else record.get("year")
    record["date_precision"] = {4: "year", 7: "month", 10: "day"}.get(len(date), "unknown")
    if record["abstract"] and record["source_depth"] != "full_text":
        record["source_depth"] = "abstract"
    identity = ("doi:" + record["doi"] if record["doi"] else
                "pmid:" + record["pmid"] if record["pmid"] else
                "pmcid:" + record["pmcid"] if record["pmcid"] else
                "metadata:" + "|".join([record["title"].casefold(), str(record["year"]), ";".join(record["authors"])]))
    record["paper_id"] = "P" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    return record


def _resolve_ncbi_gene(symbol, client, taxon="9606"):
    """Resolve an official symbol or unique alias; ambiguity raises RetrievalError."""
    symbol, taxon = str(symbol).strip(), str(taxon).strip()
    if not symbol or not re.fullmatch(r"[\w .+-]{1,100}", symbol) or not taxon.isdigit():
        raise ValueError("Invalid gene symbol or NCBI taxonomy ID")
    query = f'"{symbol}"[Gene Name] AND txid{taxon}[Organism:exp]'
    result = client.get_json(EUTILS + "esearch.fcgi", {"db": "gene", "term": query, "retmode": "json", "retmax": 100})
    search = result.get("esearchresult", {})
    ids = search.get("idlist", [])
    if not ids:
        raise RetrievalError("gene_not_found", f"No NCBI Gene record for {symbol} in taxon {taxon}")
    if int(search.get("count", len(ids))) > len(ids):
        raise RetrievalError("ambiguous_gene", "More gene matches than inspected; supply an official symbol")
    data = client.get_json(EUTILS + "esummary.fcgi", {"db": "gene", "id": ",".join(ids), "retmode": "json"}).get("result", {})
    candidates = []
    for ident in ids:
        item = data.get(str(ident), {})
        if str(item.get("organism", {}).get("taxid", taxon)) != taxon:
            continue
        official = item.get("nomenclaturesymbol") or item.get("name", "")
        aliases = _dedupe([part.strip() for part in item.get("otheraliases", "").split(",")])
        if symbol.casefold() in {official.casefold(), item.get("name", "").casefold(), *(a.casefold() for a in aliases)}:
            candidates.append((ident, official, aliases, item))
    exact = [candidate for candidate in candidates if candidate[1].casefold() == symbol.casefold()]
    if exact:
        candidates = exact
    if len(candidates) != 1:
        summary = ", ".join(f"{candidate[1]} (Gene ID {candidate[0]})" for candidate in candidates)
        raise RetrievalError("ambiguous_gene" if candidates else "gene_match_unverified",
                             "Supply an official symbol; matches: " + (summary or "none verified"))
    ident, official, aliases, item = candidates[0]
    resolved = {"symbol": official, "gene_id": str(ident), "taxon": taxon,
                "official_name": item.get("nomenclaturename") or item.get("description", ""),
                "aliases": aliases, "source": f"https://www.ncbi.nlm.nih.gov/gene/{ident}"}
    for ref in item.get("genomicinfo", []):
        if str(ref.get("ensemblgeneid", "")).startswith("ENS"):
            resolved["ensembl_id"] = ref["ensemblgeneid"]
    return resolved


def resolve_gene(symbol, client, taxon="9606"):
    """Resolve from NCBI; use unambiguous Ensembl xrefs only on outage/not-found.

    An ambiguous NCBI match never triggers fallback. Ensembl fallback must supply
    one Ensembl gene, the requested species and exactly one EntrezGene crossref.
    """
    try:
        return _resolve_ncbi_gene(symbol, client, taxon)
    except RetrievalError as primary_error:
        if primary_error.code not in {"gene_not_found", "network_error", "http_error"}:
            raise
    species_by_taxon = {"9606": "homo_sapiens", "10090": "mus_musculus", "10116": "rattus_norvegicus"}
    species = species_by_taxon.get(str(taxon))
    base = "https://rest.ensembl.org/"
    if species is None:
        rows = client.get_json(base + "info/species", {"content-type": "application/json"}).get("species", [])
        matches = _dedupe(row.get("name") for row in rows if str(row.get("taxon_id")) == str(taxon))
        if len(matches) != 1:
            raise RetrievalError("ambiguous_gene", "Ensembl could not uniquely map the requested taxonomy ID")
        species = matches[0]
    found = client.get_json(base + "xrefs/symbol/" + species + "/" + parse.quote(str(symbol), safe=""),
                            {"object_type": "gene", "content-type": "application/json"})
    ids = _dedupe(item.get("id") for item in found if item.get("type", "gene") == "gene")
    if len(ids) != 1:
        raise RetrievalError("ambiguous_gene" if ids else "gene_not_found",
                             "Ensembl fallback requires exactly one gene; found " + str(len(ids)))
    ensembl_id = ids[0]
    item = client.get_json(base + "lookup/id/" + ensembl_id, {"content-type": "application/json"})
    if item.get("species") != species or item.get("object_type") != "Gene":
        raise RetrievalError("gene_match_unverified", "Ensembl record does not match the requested species/gene type")
    xrefs = client.get_json(base + "xrefs/id/" + ensembl_id, {"external_db": "EntrezGene", "content-type": "application/json"})
    entrez = _dedupe(str(ref.get("primary_id")) for ref in xrefs if str(ref.get("primary_id", "")).isdigit())
    if len(entrez) != 1:
        raise RetrievalError("ambiguous_gene", "Ensembl fallback requires exactly one NCBI Gene cross-reference")
    aliases = _dedupe(alias for ref in xrefs for alias in ref.get("synonyms", []) if isinstance(alias, str))
    official = item.get("display_name", "")
    if not official:
        raise RetrievalError("gene_match_unverified", "Ensembl supplied no official gene display name")
    if str(symbol).casefold() != official.casefold():
        aliases = _dedupe(aliases + [str(symbol)])
    return {"symbol": official, "gene_id": entrez[0], "taxon": str(taxon), "ensembl_id": ensembl_id,
            "official_name": re.sub(r"\s*\[Source:.*$", "", item.get("description", "")), "aliases": aliases,
            "source": "https://rest.ensembl.org/lookup/id/" + ensembl_id,
            "warnings": ["NCBI Gene resolution unavailable or not found; Ensembl cross-reference fallback used. Alias coverage is partial."],
            "resolution_fallback": "ensembl"}


def build_queries(gene, concepts: list[list[str]], start: str, end: str):
    """Build sensitive gene + AND-of-OR concept queries, without species filtering.

    Short/generic aliases are omitted. Remaining compact aliases require a
    gene/protein context guard. Verified official symbol/name remain unguarded.
    """
    lower, upper = _window(start, end)
    if upper < lower:
        raise ValueError("The requested interval is entirely in the future")
    def quote(value):
        return '"' + str(value).replace('"', " ").replace("\n", " ").strip() + '"'
    official = _dedupe([gene["symbol"], gene.get("official_name", "")])
    aliases = [str(a).strip() for a in gene.get("aliases", [])]
    generic = {"m6a", "rna", "dna", "protein", "gene", "cancer", "tumor", "methyltransferase"}
    aliases = _dedupe(a for a in aliases if len(a) >= 4 and any(c.isalpha() for c in a)
                      and a.casefold() not in generic and a.casefold() not in {v.casefold() for v in official})
    pubterms = [quote(term) + "[Title/Abstract]" for term in official]
    epmcterms = ["TITLE_ABS:" + quote(term) for term in official]
    for alias in aliases:
        pubterms.append(f'({quote(alias)}[Title/Abstract] AND (gene[Title/Abstract] OR protein[Title/Abstract] OR methyltransferase[Title/Abstract]))')
        epmcterms.append(f'(TITLE_ABS:{quote(alias)} AND (TITLE_ABS:gene OR TITLE_ABS:protein OR TITLE_ABS:methyltransferase))')
    pubgroups, epmcgroups = ["(" + " OR ".join(pubterms) + ")"], ["(" + " OR ".join(epmcterms) + ")"]
    for group in concepts:
        clean = _dedupe(str(term).strip() for term in group if str(term).strip())
        if not clean:
            raise ValueError("Each concept group must contain a search term")
        pubgroups.append("(" + " OR ".join(quote(term) + "[Title/Abstract]" for term in clean) + ")")
        epmcgroups.append("(" + " OR ".join("TITLE_ABS:" + quote(term) for term in clean) + ")")
    pubgroups.append(f'("{lower:%Y/%m/%d}"[Date - Publication] : "{upper:%Y/%m/%d}"[Date - Publication])')
    epmcgroups.append(f"FIRST_PDATE:[{lower.isoformat()} TO {upper.isoformat()}]")
    return {"pubmed": " AND ".join(pubgroups), "europepmc": " AND ".join(epmcgroups)}


def _pubmed_records(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise RetrievalError("invalid_xml", "PubMed returned invalid XML") from exc
    if root.tag == "ERROR" or root.find("ERROR") is not None:
        raise RetrievalError("api_error", _text(root.find("ERROR")) or _text(root))
    records = []
    for node in root.findall(".//PubmedArticle"):
        article = node.find("./MedlineCitation/Article")
        if article is None:
            continue
        pmid = node.findtext("./MedlineCitation/PMID", "")
        ids = {item.get("IdType", ""): _text(item) for item in node.findall("./PubmedData/ArticleIdList/ArticleId")}
        dates = {}
        journal_date = _xml_date(article.find("./Journal/JournalIssue/PubDate"))
        if journal_date:
            dates["pubmed_journal"] = journal_date
        for index, item in enumerate(article.findall("ArticleDate")):
            date = _xml_date(item)
            if date:
                dates[f"pubmed_article_{item.get('DateType', 'unknown').lower()}_{index}"] = date
        for index, item in enumerate(node.findall("./PubmedData/History/PubMedPubDate")):
            date = _xml_date(item)
            if date:
                dates[f"pubmed_history_{item.get('PubStatus', 'unknown')}_{index}"] = date
        public = [value for key, value in dates.items() if any(t in key for t in ("journal", "electronic", "epublish", "ppublish"))]
        # Indexing timestamps are a fallback only when publication dates are missing.
        fallback = [value for key, value in dates.items() if "history_pubmed_" in key]
        publication = min(public or fallback or [""], key=lambda value: _date_bounds(value)[0] if value else dt.date.max)
        abstract = "\n".join((part.get("Label", "") + ": " if part.get("Label") else "") + _text(part)
                             for part in article.findall("./Abstract/AbstractText"))
        authors = []
        for author in article.findall("./AuthorList/Author"):
            family = author.findtext("LastName", "")
            initials = author.findtext("Initials", "")
            if not initials and author.findtext("ForeName"):
                initials = "".join(part[0] for part in re.findall(r"[A-Za-z]+", author.findtext("ForeName", "")))
            name = " ".join(filter(None, [family, initials])) if family else author.findtext("CollectiveName", "")
            if name:
                authors.append(name)
        types = [_text(item) for item in article.findall("./PublicationTypeList/PublicationType")]
        doi = ids.get("doi", "")
        if not doi:
            doi = next((_text(item) for item in article.findall("ELocationID") if item.get("EIdType") == "doi"), "")
        records.append(_canonical(title=_text(article.find("ArticleTitle")), authors=authors, doi=doi, pmid=pmid,
                                  pmcid=ids.get("pmc", ""), publication_date=publication, dates=dates, abstract=abstract,
                                  publication_types=types, is_preprint="Preprint" in types or _doi(doi).startswith("10.1101/"),
                                  sources=["pubmed"], urls=[f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"],
                                  provenance=[{"source": "pubmed", "record_id": pmid, "content": "MEDLINE metadata/abstract; not article full text"}]))
    return records


def search_pubmed(query, client, start, end, limit=None):
    """Retrieve all ESearch IDs using adaptive date splits at PubMed's 10k cap."""
    lower, upper = _window(start, end)
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        raise ValueError("limit must be a positive integer or None")
    log = {"source": "pubmed", "query": query, "reported_count": 0, "retrieved_count": 0,
           "complete": False, "type": "summary", "errors": [], "date_slices": [], "limit": limit,
           "start": lower.isoformat(), "end": upper.isoformat(), "excluded_outside_window": 0}
    if upper < lower:
        log["errors"].append({"code": "future_window", "message": "Interval is entirely in the future"})
        return [], [log]
    ids, seen = [], set()
    limited = False
    def collect(lo, hi, top=False):
        nonlocal limited
        if limit is not None and len(ids) >= limit:
            limited = True
            return
        scoped = f'({query}) AND ("{lo:%Y/%m/%d}"[Date - Publication] : "{hi:%Y/%m/%d}"[Date - Publication])'
        params = {"db": "pubmed", "term": scoped, "retmode": "json", "retmax": 0}
        data = client.get_json(EUTILS + "esearch.fcgi", params).get("esearchresult", {})
        if data.get("errorlist"):
            raise RetrievalError("query_error", json.dumps(data["errorlist"]))
        count = int(data.get("count", 0))
        if top:
            log["reported_count"] = count
        if count >= 10000 and lo < hi:
            middle = lo + (hi - lo) // 2
            collect(lo, middle)
            collect(middle + dt.timedelta(days=1), hi)
            return
        if count >= 10000:
            log["errors"].append({"code": "pubmed_single_day_overflow", "date": lo.isoformat(), "count": count,
                                  "message": "PubMed exposes at most 9,999 records for this single-day query; coverage is partial"})
        take = min(count, 9999)
        if limit is not None:
            if take > limit - len(ids):
                limited = True
            take = min(take, limit - len(ids))
        log["date_slices"].append({"start": lo.isoformat(), "end": hi.isoformat(), "count": count, "requested": take})
        if take:
            params["retmax"] = take
            page = client.get_json(EUTILS + "esearch.fcgi", params).get("esearchresult", {})
            page_ids = page.get("idlist", [])
            if len(page_ids) < take:
                log["errors"].append({"code": "short_id_page", "requested": take, "received": len(page_ids)})
            for ident in page_ids:
                if ident not in seen:
                    ids.append(str(ident))
                    seen.add(ident)
    try:
        collect(lower, upper, top=True)
    except RetrievalError as exc:
        log["errors"].append(exc.as_dict())
    records = []
    fetched_ids = set()
    for offset in range(0, len(ids), 200):
        batch = ids[offset:offset + 200]
        try:
            xml = client.get_text(EUTILS + "efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"})
            parsed = _pubmed_records(xml)
            fetched_ids.update(record["pmid"] for record in parsed)
            records.extend(record for record in parsed if _in_window(record, lower, upper))
            log["excluded_outside_window"] += sum(not _in_window(record, lower, upper) for record in parsed)
        except RetrievalError as exc:
            log["errors"].append(exc.as_dict())
    if not limited and len(ids) < log["reported_count"] and not log["errors"]:
        log["errors"].append({"code": "coverage_count_mismatch", "reported": log["reported_count"], "unique_ids": len(ids),
                              "message": "Unique retrieved IDs do not cover the reported count; the live index may have changed"})
    missing = set(ids) - fetched_ids
    if missing:
        log["errors"].append({"code": "missing_fetched_records", "count": len(missing), "pmids": sorted(missing)})
    log.update(retrieved_count=len(fetched_ids), included_count=len(records),
               complete=not log["errors"] and not limited,
               limit_reached=limited, unknown_date_count=sum(not r["publication_date"] for r in records))
    return merge_records([], records), [log]


def _epmc_record(item):
    source, ident = str(item.get("source", "")), str(item.get("id", ""))
    dates = {"europepmc_" + key: value for key in ("firstPublicationDate", "electronicPublicationDate", "printPublicationDate", "firstIndexDate")
             if (value := _date(item.get(key)))}
    public = [v for k, v in dates.items() if "Index" not in k]
    date = min(public, key=lambda value: _date_bounds(value)[0]) if public else _date(item.get("pubYear"))
    authors = [author.get("fullName", "") for author in item.get("authorList", {}).get("author", [])]
    if not authors and item.get("authorString"):
        authors = [part.strip() for part in item["authorString"].split(",")]
    abstract = item.get("abstractText", "")
    # AbstractText can contain JATS/HTML tags; preserve textual content only.
    if "<" in abstract:
        try:
            abstract = _text(ET.fromstring("<abstract>" + abstract + "</abstract>"))
        except ET.ParseError:
            abstract = re.sub(r"<[^>]*>", " ", abstract)
    types = item.get("pubTypeList", {}).get("pubType", [])
    return _canonical(title=item.get("title", ""), authors=_dedupe(authors), doi=item.get("doi", ""),
                      pmid=str(item.get("pmid", "") or (ident if source == "MED" else "")),
                      pmcid=item.get("pmcid", ""), publication_date=date, dates=dates, abstract=abstract,
                      is_preprint=source == "PPR" or any("preprint" in t.lower() for t in types),
                      publication_types=types, sources=["europepmc"],
                      urls=[f"https://europepmc.org/article/{source}/{ident}"],
                      provenance=[{"source": "europepmc", "record_id": ident, "source_database": source}])


def search_europepmc(query, client, start, end, limit=None):
    """Cursor-paginate Europe PMC core results, including indexed preprints."""
    lower, upper = _window(start, end)
    if limit is not None and (not isinstance(limit, int) or limit <= 0):
        raise ValueError("limit must be a positive integer or None")
    scoped = f"({query}) AND FIRST_PDATE:[{lower.isoformat()} TO {upper.isoformat()}]"
    log = {"source": "europepmc", "query": scoped, "reported_count": 0, "retrieved_count": 0,
           "complete": False, "type": "summary", "errors": [], "limit": limit, "pages": 0, "excluded_outside_window": 0}
    records, cursor, cursors = [], "*", set()
    if upper < lower:
        log["errors"].append({"code": "future_window", "message": "Interval is entirely in the future"})
        return [], [log]
    exhausted = False
    try:
        while True:
            size = min(1000, limit - log["retrieved_count"]) if limit else 1000
            if size <= 0:
                break
            response = client.get_json(EPMC + "search", {"query": scoped, "format": "json", "resultType": "core",
                                                          "pageSize": size, "cursorMark": cursor})
            log["reported_count"] = int(response.get("hitCount", 0))
            items = response.get("resultList", {}).get("result", [])
            log["pages"] += 1
            for item in items:
                record = _epmc_record(item)
                if _in_window(record, lower, upper):
                    records.append(record)
                else:
                    log["excluded_outside_window"] += 1
            log["retrieved_count"] += len(items)
            if log["retrieved_count"] >= log["reported_count"]:
                exhausted = True
                break
            next_cursor = response.get("nextCursorMark")
            if not items or not next_cursor or next_cursor == cursor or next_cursor in cursors:
                log["errors"].append({"code": "pagination_stalled", "message": "API cursor ended before all reported hits were retrieved"})
                break
            cursors.add(cursor)
            cursor = next_cursor
    except RetrievalError as exc:
        log["errors"].append(exc.as_dict())
    log.update(complete=exhausted and not log["errors"], included_count=len(records),
               limit_reached=bool(limit and log["retrieved_count"] >= limit and not exhausted),
               unknown_date_count=sum(not record["publication_date"] for record in records))
    return merge_records([], records), [log]


def merge_records(existing, incoming):
    """Deduplicate actual identifier matches; distinct DOIs never merge by title.

    Explicit preprint/publication DOI relationships survive as separate records.
    Existing paper_ids remain stable even after metadata enrichment.
    """
    merged = [copy.deepcopy(record) for record in existing]
    indexes = {key: {} for key in ("doi", "pmid", "pmcid", "paper_id")}
    def index_record(record, position):
        for key, index in indexes.items():
            value = _doi(record.get(key)) if key == "doi" else record.get(key)
            if value:
                bucket = index.setdefault(value, [])
                if position not in bucket:
                    bucket.append(position)
    for position, record in enumerate(merged):
        index_record(record, position)
    for original in incoming:
        record = copy.deepcopy(original)
        record["doi"] = _doi(record.get("doi"))
        if not record.get("paper_id"):
            record = _canonical(**record)
        match = None
        candidates = []
        for key, index in indexes.items():
            candidates.extend(index.get(record.get(key), []))
        for position in dict.fromkeys(candidates):
            old = merged[position]
            old_doi, new_doi = _doi(old.get("doi")), record["doi"]
            if old_doi and new_doi and old_doi != new_doi:
                continue
            match = old
            break
        if match is None:
            merged.append(record)
            index_record(record, len(merged) - 1)
            continue
        for key in ("sources", "urls", "publication_types", "related_dois"):
            match[key] = _dedupe(match.get(key, []) + record.get(key, []))
        for entry in record.get("provenance", []):
            if entry not in match.setdefault("provenance", []):
                match["provenance"].append(entry)
        match.setdefault("dates", {}).update(record.get("dates", {}))
        if record.get("corpus_role") == "core" or (not match.get("corpus_role") and record.get("corpus_role")):
            match["corpus_role"] = record["corpus_role"]
        for key in ("title", "doi", "pmid", "pmcid", "full_text_path"):
            if not match.get(key) and record.get(key):
                match[key] = record[key]
        if len(record.get("abstract", "")) > len(match.get("abstract", "")):
            match["abstract"] = record["abstract"]
        if record.get("authors") and len(record["authors"]) >= len(match.get("authors", [])):
            match["authors"] = record["authors"]
        old_date, new_date = match.get("publication_date", ""), record.get("publication_date", "")
        if new_date and (not old_date or _date_bounds(new_date)[0] < _date_bounds(old_date)[0] or
                         (old_date[:4] == new_date[:4] and len(new_date) > len(old_date))):
            match["publication_date"] = new_date
            match["year"] = int(new_date[:4])
            match["date_precision"] = record.get("date_precision", "unknown")
        depth = {"metadata": 0, "abstract": 1, "full_text": 2}
        match["source_depth"] = max([match.get("source_depth", "metadata"), record.get("source_depth", "metadata")], key=depth.get)
        match["is_preprint"] = bool(match.get("is_preprint") or record.get("is_preprint"))
        index_record(match, position)
    # Group only explicitly linked DOI identities, never superficial title similarity.
    by_doi = {record["doi"]: record for record in merged if record.get("doi")}
    parent = {record["paper_id"]: record["paper_id"] for record in merged}
    def find(ident):
        while parent[ident] != ident:
            parent[ident] = parent[parent[ident]]
            ident = parent[ident]
        return ident
    for record in merged:
        for related in record.get("related_dois", []):
            other = by_doi.get(_doi(related))
            if other and other is not record:
                if record.get("doi"):
                    other["related_dois"] = _dedupe(other.get("related_dois", []) + [record["doi"]])
                parent[find(record["paper_id"])] = find(other["paper_id"])
    groups = {}
    for record in merged:
        groups.setdefault(find(record["paper_id"]), []).append(record)
    for members in groups.values():
        if len(members) > 1:
            group = "W" + hashlib.sha256("|".join(sorted(item["paper_id"] for item in members)).encode()).hexdigest()[:16]
            for item in members:
                item["work_family_id"] = group

    return merged


def enrich_biorxiv(records, client):
    """Return (records, logs) with explicit bioRxiv/medRxiv DOI/version links.

    A discovered published DOI is a relationship, not evidence of having read or
    retrieved the published article. Failed optional enrichments stay partial.
    """
    enriched, logs = copy.deepcopy(records), []
    for record in enriched:
        doi = _doi(record.get("doi"))
        if not doi.startswith("10.1101/"):
            continue
        log = {"source": "biorxiv", "query": doi, "reported_count": 0, "retrieved_count": 0, "complete": False, "errors": []}
        for server in ("biorxiv", "medrxiv"):
            try:
                data = client.get_json(f"https://api.biorxiv.org/details/{server}/{parse.quote(doi, safe='/')}")
                items = [item for item in data.get("collection", []) if _doi(item.get("doi")) == doi]
                if not items:
                    continue
                record["is_preprint"] = True
                versions = []
                for item in items:
                    published = _doi(item.get("published"))
                    if published and published not in {"na", "n/a", "none"} and published.startswith("10."):
                        record["related_dois"] = _dedupe(record.get("related_dois", []) + [published])
                    versions.append({"version": str(item.get("version", "")), "date": _date(item.get("date")),
                                     "published_doi": published if published.startswith("10.") else "", "server": server})
                record["preprint_versions"] = versions
                record["provenance"].append({"source": server, "doi": doi, "relationship": "explicit publisher API", "versions": versions})
                log.update(source=server, reported_count=len(items), retrieved_count=len(items), complete=True)
                break
            except RetrievalError as exc:
                # Wrong-server not-found should permit trying the other server.
                if exc.status != 404:
                    log["errors"].append(exc.as_dict())
        if not log["complete"] and not log["errors"]:
            log["errors"].append({"code": "preprint_not_found", "message": "No matching bioRxiv/medRxiv DOI metadata"})
        logs.append(log)
    return merge_records([], enriched), logs


def fetch_full_text(record, client, output_dir):
    """Return an updated record after retrieving genuine OA article XML.

    Uses Europe PMC fullTextXML; subscription pages and MEDLINE are not full
    text. A structured failure is appended to provenance, retaining prior depth.
    """
    result = copy.deepcopy(record)
    pmcid = str(result.get("pmcid", "")).upper()
    if not pmcid and (result.get("doi") or result.get("pmid")):
        query = "DOI:" + result["doi"] if result.get("doi") else "EXT_ID:" + result["pmid"] + " AND SRC:MED"
        try:
            data = client.get_json(EPMC + "search", {"query": query, "format": "json", "pageSize": 10})
            exact = [item for item in data.get("resultList", {}).get("result", []) if
                     (result.get("doi") and _doi(item.get("doi")) == _doi(result["doi"])) or
                     (result.get("pmid") and str(item.get("pmid") or item.get("id")) == result["pmid"])]
            pmcids = _dedupe(item.get("pmcid", "") for item in exact)
            if len(pmcids) == 1:
                pmcid = pmcids[0]
        except RetrievalError as exc:
            result.setdefault("provenance", []).append({"source": "europepmc_full_text", "error": exc.as_dict()})
            return result
    if not re.fullmatch(r"PMC\d+", pmcid):
        result.setdefault("provenance", []).append({"source": "europepmc_full_text", "status": "unavailable", "reason": "No unambiguous PMCID"})
        return result
    url = EPMC + pmcid + "/fullTextXML"
    try:
        xml = client.get_text(url)
        root = ET.fromstring(xml)
        article = root if root.tag.rsplit("}", 1)[-1] == "article" else root.find("article")
        if article is None or article.find("body") is None or not _text(article.find("body")):
            raise RetrievalError("not_full_text", "Response contains no article body", url=url)
        # Preserve OA licensing exactly as the source presents it; do not infer reuse rights.
        licenses = [_text(item) for item in article.findall(".//license")]
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", result.get("paper_id") or pmcid)
        xml_path = output / (safe_id + ".xml")
        xml_path.write_text(xml, encoding="utf-8")
        path = output / (safe_id + ".txt")
        # JATS inline markup must preserve word boundaries; block boundaries
        # become whitespace so quoted evidence is readable in the plain text.
        blocks = {"p", "title", "article-title", "abstract", "sec", "table", "table-wrap", "tr", "ref", "list-item"}
        def plain_text(node):
            chunks = [node.text or ""]
            for child in node:
                if child.tag.rsplit("}", 1)[-1] in blocks:
                    chunks.extend(["\n", plain_text(child), "\n"])
                else:
                    chunks.append(plain_text(child))
                chunks.append(child.tail or "")
            return "".join(chunks)
        plain = "\n".join(" ".join(line.split()) for line in plain_text(article).splitlines())
        path.write_text(plain.strip() + "\n", encoding="utf-8")
        result.update(pmcid=pmcid, full_text_path=str(path.resolve()), full_text_xml_path=str(xml_path.resolve()), source_depth="full_text")
        result["urls"] = _dedupe(result.get("urls", []) + [url])
        result.setdefault("provenance", []).append({"source": "europepmc_full_text", "url": url, "format": "JATS XML",
                                                   "license_text": licenses, "body_present": True})
    except ET.ParseError:
        result.setdefault("provenance", []).append({"source": "europepmc_full_text", "error": {"code": "invalid_xml", "message": "Invalid full-text XML"}})
    except RetrievalError as exc:
        result.setdefault("provenance", []).append({"source": "europepmc_full_text", "error": exc.as_dict()})
    return result
