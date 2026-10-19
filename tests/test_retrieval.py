"""Deterministic contract and failure tests; no network or third-party packages."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location("retrieval", Path(__file__).resolve().parents[1] / "scripts" / "retrieval.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def article(pmid="1", doi="10.1000/one", date="2022", electronic="", title="A genuine study"):
    edate = ""
    if electronic:
        year, month, day = electronic.split("-")
        edate = f'<ArticleDate DateType="Electronic"><Year>{year}</Year><Month>{month}</Month><Day>{day}</Day></ArticleDate>'
    return f'''<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><ArticleTitle>{title}</ArticleTitle>
    <Journal><JournalIssue><PubDate><Year>{date}</Year></PubDate></JournalIssue></Journal>
    <Abstract><AbstractText Label="RESULTS">Result evidence.</AbstractText></Abstract>
    <AuthorList><Author><ForeName>Jane</ForeName><LastName>Doe</LastName></Author></AuthorList>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>{edate}</Article></MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="doi">{doi}</ArticleId></ArticleIdList></PubmedData></PubmedArticle>'''


class FakeClient:
    def __init__(self, json_fn=None, text_fn=None):
        self.json_fn, self.text_fn = json_fn, text_fn
        self.calls = []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        return self.json_fn(url, params)

    def get_text(self, url, params=None):
        self.calls.append((url, params))
        return self.text_fn(url, params)


class RetrievalTests(unittest.TestCase):
    def test_queries_keep_concept_or_groups_and_no_species_filter(self):
        gene = {"symbol": "METTL3", "official_name": "methyltransferase 3", "aliases": ["M6A", "MT-A70", "1234"]}
        queries = r.build_queries(gene, [["cancer", "tumor"], ["immune escape"]], "2021-01-01", "2025-12-31")
        self.assertIn('"METTL3"[Title/Abstract]', queries["pubmed"])
        self.assertIn('"MT-A70"[Title/Abstract] AND', queries["pubmed"])
        self.assertNotIn('"M6A"', queries["pubmed"])
        self.assertNotIn("Humans", queries["pubmed"])
        self.assertIn("FIRST_PDATE:[2021-01-01 TO 2025-12-31]", queries["europepmc"])

    def test_gene_ambiguous_alias_refused(self):
        def answer(url, params):
            if "esearch" in url:
                return {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}
            return {"result": {"1": {"name": "GENE1", "otheraliases": "ALIAS"}, "2": {"name": "GENE2", "otheraliases": "ALIAS"}}}
        with self.assertRaises(r.RetrievalError) as cm:
            r.resolve_gene("ALIAS", FakeClient(json_fn=answer))
        self.assertEqual(cm.exception.code, "ambiguous_gene")

    def test_gene_official_symbol_preferred_to_other_gene_alias(self):
        def answer(url, params):
            if "esearch" in url:
                return {"esearchresult": {"count": "2", "idlist": ["1", "2"]}}
            return {"result": {"1": {"name": "METTL3", "description": "methyltransferase 3", "otheraliases": "MT-A70"},
                               "2": {"name": "OTHER", "otheraliases": "METTL3"}}}
        gene = r.resolve_gene("METTL3", FakeClient(json_fn=answer))
        self.assertEqual(gene["gene_id"], "1")
        self.assertEqual(gene["aliases"], ["MT-A70"])

    def test_pubmed_xml_prefers_earlier_online_date(self):
        records = r._pubmed_records("<PubmedArticleSet>" + article(electronic="2021-12-20") + "</PubmedArticleSet>")
        self.assertEqual(records[0]["publication_date"], "2021-12-20")
        self.assertEqual(records[0]["dates"]["pubmed_journal"], "2022")
        self.assertEqual(records[0]["source_depth"], "abstract")
        self.assertEqual(records[0]["authors"], ["Doe J"])

    def test_parsed_pubmed_author_matches_registry_verification(self):
        verification_spec = importlib.util.spec_from_file_location("verification", Path(__file__).resolve().parents[1] / "scripts" / "verification.py")
        verification = importlib.util.module_from_spec(verification_spec)
        verification_spec.loader.exec_module(verification)
        xml = "<PubmedArticleSet>" + article() + "</PubmedArticleSet>"
        paper = r._pubmed_records(xml)[0]
        metadata = verification._pubmed_metadata(xml, "1")
        status, checks = verification.compare_metadata(paper, metadata)
        self.assertEqual(status, "verified")
        self.assertEqual(checks["authors"], "match")

    def test_ensembl_fallback_retains_ncbi_id_and_partial_alias_warning(self):
        def answer(url, params):
            if "esearch" in url:
                raise r.RetrievalError("network_error", "offline")
            if "xrefs/symbol" in url:
                return [{"id": "ENSG00000165819", "type": "gene"}]
            if "lookup/id" in url:
                return {"display_name": "METTL3", "species": "homo_sapiens", "object_type": "Gene", "description": "methyltransferase 3 [Source:HGNC Symbol;Acc:HGNC:17563]"}
            return [{"primary_id": "56339", "synonyms": ["MT-A70"]}]
        gene = r.resolve_gene("METTL3", FakeClient(json_fn=answer))
        self.assertEqual(gene["gene_id"], "56339")
        self.assertEqual(gene["ensembl_id"], "ENSG00000165819")
        self.assertEqual(gene["resolution_fallback"], "ensembl")
        self.assertIn("partial", gene["warnings"][0])

    def test_ensembl_ambiguous_fallback_refused(self):
        def answer(url, params):
            if "esearch" in url:
                raise r.RetrievalError("network_error", "offline")
            return [{"id": "ENSG1", "type": "gene"}, {"id": "ENSG2", "type": "gene"}]
        with self.assertRaises(r.RetrievalError) as cm:
            r.resolve_gene("ALIAS", FakeClient(json_fn=answer))
        self.assertEqual(cm.exception.code, "ambiguous_gene")

    def test_unknown_date_remains_unknown(self):
        rec = r._canonical(title="Undated")
        self.assertIsNone(rec["year"])
        self.assertEqual(rec["date_precision"], "unknown")
        self.assertTrue(r._in_window(rec, *r._window("2021", "2025")))

    def test_pubmed_limit_is_partial_even_with_successful_fetch(self):
        def answer(url, params):
            return {"esearchresult": {"count": "3", "idlist": ["1"] if params["retmax"] else []}}
        client = FakeClient(json_fn=answer, text_fn=lambda *args: "<PubmedArticleSet>" + article() + "</PubmedArticleSet>")
        records, logs = r.search_pubmed("test", client, "2021", "2025", limit=1)
        self.assertEqual(len(records), 1)
        self.assertFalse(logs[0]["complete"])
        self.assertTrue(logs[0]["limit_reached"])
        self.assertEqual(logs[0]["reported_count"], 3)
        self.assertEqual(logs[0]["retrieved_count"], 1)

    def test_pubmed_splits_at_ten_thousand(self):
        count_calls = []
        def answer(url, params):
            if not params["retmax"]:
                count_calls.append(params["term"])
                return {"esearchresult": {"count": "10000" if len(count_calls) == 1 else "5000"}}
            offset = 0 if '2022/01/01"[Date' in params["term"] else 5000
            return {"esearchresult": {"count": "5000", "idlist": [str(i) for i in range(offset + 1, offset + 5001)]}}
        client = FakeClient(json_fn=answer, text_fn=lambda url, p: p["id"])
        def parse_ids(text):
            return [r._canonical(pmid=ident, publication_date="2022") for ident in text.split(",")]
        with patch.object(r, "_pubmed_records", side_effect=parse_ids):
            records, logs = r.search_pubmed("test", client, "2022-01-01", "2022-01-02")
        self.assertEqual(len(count_calls), 3)
        self.assertEqual(len(logs[0]["date_slices"]), 2)
        self.assertEqual(logs[0]["reported_count"], 10000)
        self.assertTrue(logs[0]["complete"])
        self.assertEqual(len(records), 10000)

    def test_single_day_overflow_is_explicit_partial(self):
        def answer(url, params):
            return {"esearchresult": {"count": "10000", "idlist": ["1"]}}
        client = FakeClient(json_fn=answer, text_fn=lambda *args: "<PubmedArticleSet>" + article() + "</PubmedArticleSet>")
        _, logs = r.search_pubmed("test", client, "2022-01-01", "2022-01-01", limit=1)
        self.assertFalse(logs[0]["complete"])
        self.assertIn("pubmed_single_day_overflow", [e["code"] for e in logs[0]["errors"]])

    def test_epmc_cursor_pagination_and_future_filter(self):
        def answer(url, params):
            if params["cursorMark"] == "*":
                return {"hitCount": 2, "nextCursorMark": "next", "resultList": {"result": [{"id": "1", "source": "MED", "title": "Past", "firstPublicationDate": "2022-01-01"}]}}
            return {"hitCount": 2, "resultList": {"result": [{"id": "2", "source": "MED", "title": "Future", "firstPublicationDate": "2099-01-01"}]}}
        records, logs = r.search_europepmc("test", FakeClient(json_fn=answer), "2021", "2025")
        self.assertEqual(len(records), 1)
        self.assertTrue(logs[0]["complete"])
        self.assertEqual(logs[0]["retrieved_count"], 2)
        self.assertEqual(logs[0]["excluded_outside_window"], 1)

    def test_epmc_stalled_cursor_is_partial(self):
        client = FakeClient(json_fn=lambda *args: {"hitCount": 4, "nextCursorMark": "*", "resultList": {"result": [{"id": "1", "source": "MED"}]}})
        _, logs = r.search_europepmc("test", client, "2021", "2025")
        self.assertFalse(logs[0]["complete"])
        self.assertEqual(logs[0]["errors"][0]["code"], "pagination_stalled")

    def test_merge_identifiers_stable_and_core_preferred(self):
        existing = r._canonical(pmid="1", title="X", authors=["Jane Doe"], corpus_role="core")
        incoming = r._canonical(pmid="1", title="X", authors=["Doe J"], doi="10.1/abc", corpus_role="gap_verification")
        merged = r.merge_records([existing], [incoming])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["paper_id"], existing["paper_id"])
        self.assertEqual(merged[0]["corpus_role"], "core")
        self.assertEqual(merged[0]["doi"], "10.1/abc")
        self.assertEqual(merged[0]["authors"], ["Doe J"])

    def test_distinct_dois_same_title_not_merged_explicit_links_grouped(self):
        preprint = r._canonical(title="Same title", doi="10.1101/2022.123", is_preprint=True, related_dois=["10.1/published"])
        published = r._canonical(title="Same title", doi="10.1/published")
        merged = r.merge_records([preprint], [published])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["work_family_id"], merged[1]["work_family_id"])
        self.assertIn("10.1101/2022.123", merged[1]["related_dois"])

    def test_biorxiv_versions_explicit_not_invented_published_record(self):
        preprint = r._canonical(doi="10.1101/2022.123", is_preprint=True)
        client = FakeClient(json_fn=lambda *args: {"collection": [{"doi": preprint["doi"], "version": "1", "date": "2022-01-02", "published": "10.1/final"},
                                                                  {"doi": preprint["doi"], "version": "2", "date": "2022-02-02", "published": "10.1/final"}]})
        records, logs = r.enrich_biorxiv([preprint], client)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["preprint_versions"]), 2)
        self.assertEqual(records[0]["related_dois"], ["10.1/final"])
        self.assertTrue(logs[0]["complete"])

    def test_full_text_requires_body_and_saves_plain_text(self):
        record = r._canonical(title="Article", pmcid="PMC123", abstract="Abstract")
        client = FakeClient(text_fn=lambda *args: "<article><front><article-title>Title</article-title></front><body><sec><title>Results</title><p>Actual <italic>article</italic> evidence.</p></sec></body></article>")
        with tempfile.TemporaryDirectory() as directory:
            enriched = r.fetch_full_text(record, client, directory)
            self.assertEqual(enriched["source_depth"], "full_text")
            plain = Path(enriched["full_text_path"]).read_text()
            self.assertIn("Actual article evidence.", plain)
            self.assertNotIn("<", plain)
            self.assertTrue(Path(enriched["full_text_xml_path"]).exists())
        no_body = FakeClient(text_fn=lambda *args: "<article><front><abstract>Only abstract</abstract></front></article>")
        with tempfile.TemporaryDirectory() as directory:
            enriched = r.fetch_full_text(record, no_body, directory)
            self.assertEqual(enriched["source_depth"], "abstract")
            self.assertNotIn("full_text_path", enriched)

    def test_transport_cache_and_redacted_errors(self):
        class Response(io.BytesIO):
            class Headers:
                def get_content_charset(self):
                    return "utf-8"
            headers = Headers()
        with tempfile.TemporaryDirectory() as directory:
            client = r.Client(directory)
            with patch.object(r.request, "urlopen", return_value=Response(b'{"ok": true}')) as fetch:
                self.assertTrue(client.get_json("https://example.test/data")["ok"])
                self.assertTrue(client.get_json("https://example.test/data")["ok"])
                self.assertEqual(fetch.call_count, 1)
        details = r.RetrievalError("x", "error", url="https://example.test/?api_key=secret&term=test").as_dict()
        self.assertNotIn("secret", details["url"])

    def test_http_retry_and_structured_failure(self):
        class Response(io.BytesIO):
            class Headers:
                def get_content_charset(self):
                    return "utf-8"
            headers = Headers()
        transient = HTTPError("https://example.test", 429, "Rate limited", {}, None)
        with tempfile.TemporaryDirectory() as directory:
            client = r.Client(directory)
            with patch.object(r.request, "urlopen", side_effect=[transient, Response(b"ok")]) as fetch, patch.object(r.time, "sleep"):
                self.assertEqual(client.get_text("https://example.test"), "ok")
                self.assertEqual(fetch.call_count, 2)
        permanent = HTTPError("https://example.test", 403, "Forbidden", {}, None)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(r.request, "urlopen", side_effect=permanent):
                with self.assertRaises(r.RetrievalError) as cm:
                    r.Client(directory).get_text("https://example.test")
                self.assertEqual(cm.exception.status, 403)
                self.assertFalse(cm.exception.retryable)


if __name__ == "__main__":
    unittest.main()
