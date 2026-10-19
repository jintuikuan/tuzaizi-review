"""Reference identity checks. Claim support remains an evidence-reading task.

Python 3.10+, standard library only. No DOI-registry success is treated as
proof that a manuscript claim is supported.
"""
from __future__ import annotations

import datetime as dt
import difflib
import html
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET


def normalized_title(value):
    value = re.sub(r"<[^>]+>", "", html.unescape(value or ""))
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(c for c in value if c.isalnum())


def normalized_doi(value):
    value = urllib.parse.unquote((value or "").strip()).lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    return value.strip()


def surname(name):
    # Corpus and registry adapters store authors as surname + initials when possible.
    name = unicodedata.normalize("NFKD", name or "").casefold()
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", name.split(",")[0].split()[0]) if name.split() else ""


def _text(node):
    return "".join(node.itertext()).strip() if node is not None else ""


def _pubmed_metadata(xml, expected_pmid):
    root = ET.fromstring(xml)
    for item in root.findall(".//PubmedArticle"):
        citation = item.find("MedlineCitation")
        pmid = _text(citation.find("PMID"))
        if pmid != str(expected_pmid):
            continue
        article = citation.find("Article")
        authors = []
        for a in article.findall("AuthorList/Author"):
            family = _text(a.find("LastName"))
            authors.append((family + " " + _text(a.find("Initials"))).strip()
                           if family else _text(a.find("CollectiveName")))
        years = set()
        for y in article.findall(".//Year"):
            if (y.text or "").isdigit():
                years.add(int(y.text))
        for d in article.findall(".//MedlineDate"):
            years.update(int(x) for x in re.findall(r"\b(?:19|20)\d{2}\b", d.text or ""))
        doi = ""
        for a in item.findall("PubmedData/ArticleIdList/ArticleId"):
            if a.get("IdType") == "doi":
                doi = normalized_doi(a.text)
        if not doi:
            for a in article.findall("ELocationID"):
                if a.get("EIdType") == "doi":
                    doi = normalized_doi(a.text)
        notices = []
        status = "normal"
        for p in article.findall("PublicationTypeList/PublicationType"):
            label = _text(p)
            if label.lower() == "retracted publication":
                status = "retracted"
                notices.append({"type": "retraction", "label": label, "source": "pubmed"})
            elif "expression of concern" in label.lower():
                status = "expression_of_concern"
                notices.append({"type": "expression_of_concern", "label": label, "source": "pubmed"})
        for c in citation.findall("CommentsCorrectionsList/CommentsCorrections"):
            kind = c.get("RefType", "")
            if kind in ("RetractionIn", "ExpressionOfConcernIn", "ErratumIn", "UpdateIn"):
                typ = {"RetractionIn": "retraction", "ExpressionOfConcernIn": "expression_of_concern",
                       "ErratumIn": "correction", "UpdateIn": "update"}[kind]
                notices.append({"type": typ, "source": "pubmed", "pmid": _text(c.find("PMID")),
                                "label": _text(c.find("RefSource"))})
                if typ == "retraction":
                    status = "retracted"
                elif typ == "expression_of_concern" and status != "retracted":
                    status = "expression_of_concern"
                elif status == "normal":
                    status = "corrected"
        return {"title": _text(article.find("ArticleTitle")), "authors": authors,
                "years": sorted(years), "year": min(years) if years else None,
                "pmid": pmid, "doi": doi, "notices": notices, "publication_status": status,
                "registry": "pubmed"}
    raise ValueError("Requested PMID was not returned by PubMed")


def _crossref_metadata(message):
    titles = message.get("title", [])
    authors = [(a.get("family", "") + " " + a.get("given", "")).strip()
               or a.get("name", "") for a in message.get("author", [])]
    years = set()
    for key in ("published", "published-online", "published-print", "issued", "posted"):
        for parts in message.get(key, {}).get("date-parts", []):
            if parts and isinstance(parts[0], int):
                years.add(parts[0])
    notices = []
    status = "normal"
    # update-to identifies notices that this work issues, not notices issued about it.
    for notice in message.get("update-to", []):
        typ = str(notice.get("type", "")).lower()
        if typ:
            notices.append({"type": typ, "doi": notice.get("DOI", ""),
                            "source": "crossref", "direction": "this_work_updates_other"})
    # Some Crossref records carry publisher update relations. A dedicated query
    # below looks up notice records pointing at the original work.
    clean_title = re.sub(r"<[^>]+>", "", html.unescape(titles[0] if titles else ""))
    return {"title": clean_title, "authors": authors,
            "years": sorted(years), "year": min(years) if years else None,
            "doi": normalized_doi(message.get("DOI")), "pmid": "", "notices": notices,
            "publication_status": status, "registry": "crossref"}


def compare_metadata(paper, metadata):
    """Strict normalized identity comparison; fuzzy title variants need review."""
    checks = {}
    a, b = normalized_title(paper.get("title")), normalized_title(metadata.get("title"))
    if not a or not b:
        checks["title"] = "missing"
    elif a == b:
        checks["title"] = "match"
    else:
        checks["title"] = ("variant_needs_review" if difflib.SequenceMatcher(None, a, b).ratio() >= .90
                           else "mismatch")
    pa, ma = paper.get("authors", []), metadata.get("authors", [])
    checks["authors"] = ("match" if surname(pa[0]) == surname(ma[0]) else "mismatch") if pa and ma else "missing"
    year = paper.get("year")
    publication_years = {int(year)} if year else set()
    # An online-first article and its print issue can have different years.
    # Use actual retained publication dates, never a blanket +/-1 tolerance or
    # received/indexed/embargo-release years.
    for key, value in paper.get("dates", {}).items():
        publication_key = (key == "pubmed_journal" or key.startswith("pubmed_article_")
                           or key in {"europepmc_firstPublicationDate", "europepmc_electronicPublicationDate",
                                      "europepmc_printPublicationDate"})
        if publication_key and isinstance(value, str) and re.match(r"^\d{4}(?:-|$)", value):
            publication_years.add(int(value[:4]))
    checks["year"] = ("match" if publication_years.intersection(metadata.get("years", [])) else "mismatch") if publication_years and metadata.get("years") else "missing"
    for key in ("doi", "pmid"):
        val, other = str(paper.get(key) or ""), str(metadata.get(key) or "")
        if key == "doi":
            val, other = normalized_doi(val), normalized_doi(other)
        if val and other:
            checks[key] = "match" if val == other else "mismatch"
        elif val:
            checks[key] = "not_in_this_registry"
    if "mismatch" in checks.values():
        status = "mismatch"
    elif all(checks.get(k) == "match" for k in ("title", "authors", "year")):
        status = "verified"
    else:
        status = "unverified"
    return status, checks


def verify_reference(paper, client):
    """Check identities against authoritative metadata; retain coverage/errors."""
    result = {"paper_id": paper["paper_id"], "status": "unverified", "metadata": {},
              "checks": {}, "notices": [], "publication_status": "unknown",
              "notice_status": "unknown", "errors": [], "registry_results": [],
              "checked_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    doi, pmid = normalized_doi(paper.get("doi")), str(paper.get("pmid") or "")
    if not doi and not pmid:
        result["errors"].append("No DOI or PMID available")
        return result
    metadata_rows = []
    if pmid:
        try:
            xml = client.get_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                                  {"db": "pubmed", "id": pmid, "retmode": "xml"})
            metadata_rows.append(_pubmed_metadata(xml, pmid))
        except Exception as exc:
            result["errors"].append("PubMed: " + str(exc))
    if doi:
        try:
            data = client.get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""))
            metadata_rows.append(_crossref_metadata(data["message"]))
        except Exception as exc:
            result["errors"].append("Crossref: " + str(exc))
    statuses = []
    for m in metadata_rows:
        status, checks = compare_metadata(paper, m)
        statuses.append(status)
        result["registry_results"].append({"registry": m["registry"], "status": status,
                                           "checks": checks, "metadata": m})
        result["notices"].extend(m.get("notices", []))
        if not result["metadata"] or (status == "verified" and
                (not result.get("_metadata_verified") or m["registry"] == "pubmed")):
            result["metadata"], result["checks"] = dict(m), checks
            result["_metadata_verified"] = status == "verified"
        if m.get("publication_status") != "normal":
            result["publication_status"] = m["publication_status"]
        elif result["publication_status"] == "unknown":
            result["publication_status"] = "normal"
    # Conflicting identifiers must not be rescued by a different successful registry.
    if "mismatch" in statuses:
        result["status"] = "mismatch"
    elif "verified" in statuses:
        # Every supplied identifier must match authoritative metadata somewhere;
        # a correct PMID cannot rescue a typo DOI absent from both registries.
        matched_ids = all(not value or any(r["checks"].get(key) == "match"
                          for r in result["registry_results"])
                          for key, value in (("doi", doi), ("pmid", pmid)))
        result["status"] = "verified" if matched_ids else "unverified"
    if any(m["registry"] == "pubmed" for m in metadata_rows):
        result["notice_status"] = "detected" if result["notices"] else "no_notice_found"
    # Europe PMC exposes publisher/retraction links even for many DOI-only records.
    try:
        query = 'EXT_ID:' + pmid + ' AND SRC:MED' if pmid else 'DOI:"' + doi + '"'
        ep = client.get_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                             {"query": query, "format": "json", "resultType": "core", "pageSize": 10})
        matched = False
        for row in ep.get("resultList", {}).get("result", []):
            if not ((pmid and str(row.get("pmid", row.get("id", ""))) == pmid)
                    or (doi and normalized_doi(row.get("doi")) == doi)):
                continue
            matched = True
            if str(row.get("isRetracted", "N")).upper() == "Y":
                result["publication_status"] = "retracted"
                result["notices"].append({"type": "retraction", "source": "europepmc"})
            for n in row.get("commentCorrectionList", {}).get("commentCorrection", []):
                label = str(n.get("type", ""))
                if any(x in label.lower() for x in ("retract", "erratum", "correct", "concern")):
                    result["notices"].append({"type": label, "source": "europepmc", "details": n})
                    if "retract" in label.lower() and "in" in label.lower():
                        result["publication_status"] = "retracted"
                    elif "concern" in label.lower():
                        result["publication_status"] = "expression_of_concern"
                    elif result["publication_status"] not in ("retracted", "expression_of_concern"):
                        result["publication_status"] = "corrected"
        if matched:
            result["notice_status"] = "detected" if result["notices"] else "no_notice_found"
    except Exception as exc:
        result["errors"].append("Europe PMC notice check: " + str(exc))
    result["notice_coverage"] = "Known notices exposed by successfully queried PubMed/Europe PMC/Crossref metadata; not an exhaustive publisher audit."
    result.pop("_metadata_verified", None)
    return result
