import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verification import compare_metadata, verify_reference


XML = '''<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>
<Article><ArticleTitle>METTL3 promotes immune escape</ArticleTitle>
<Journal><JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue></Journal>
<ArticleDate DateType="Electronic"><Year>2022</Year></ArticleDate>
<AuthorList><Author><LastName>Wang</LastName><Initials>J</Initials></Author></AuthorList>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
</Article>{notice}</MedlineCitation><PubmedData><ArticleIdList>
<ArticleId IdType="doi">10.1000/real</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>'''


class Client:
    def __init__(self, fail=False, notice=""):
        self.fail = fail
        self.notice = notice

    def get_text(self, url, params=None):
        if self.fail:
            raise OSError("offline")
        return XML.format(notice=self.notice)

    def get_json(self, url, params=None):
        if self.fail:
            raise OSError("offline")
        if "crossref" in url:
            return {"message": {"title": ["METTL3 promotes immune escape"],
                    "author": [{"family": "Wang", "given": "J"}], "DOI": "10.1000/real",
                    "published-online": {"date-parts": [[2022, 12, 1]]}}}
        return {"resultList": {"result": [{"id": "123", "pmid": "123", "isRetracted": "N"}]}}


class ReferenceTests(unittest.TestCase):
    def paper(self, **changes):
        out = {"paper_id": "P1", "title": "METTL3 promotes immune escape.",
               "authors": ["Wang J"], "year": 2022, "doi": "10.1000/real", "pmid": "123"}
        out.update(changes)
        return out

    def test_identifiers_and_fields_match(self):
        r = verify_reference(self.paper(), Client())
        self.assertEqual(r["status"], "verified")
        self.assertEqual(r["notice_status"], "no_notice_found")
        self.assertEqual(r["metadata"]["pmid"], "123")

    def test_doi_for_other_work_is_not_rescued(self):
        r = verify_reference(self.paper(doi="10.1000/unrelated"), Client())
        self.assertEqual(r["status"], "mismatch")

    def test_network_failure_is_unverified_not_false(self):
        r = verify_reference(self.paper(), Client(fail=True))
        self.assertEqual(r["status"], "unverified")
        self.assertEqual(r["notice_status"], "unknown")
        self.assertTrue(r["errors"])

    def test_correct_pmid_does_not_rescue_unresolved_doi(self):
        class NoDoiClient(Client):
            def get_text(self, url, params=None):
                return super().get_text(url, params).replace('<ArticleId IdType="doi">10.1000/real</ArticleId>', '')

            def get_json(self, url, params=None):
                if "crossref" in url:
                    raise OSError("DOI not registered")
                return super().get_json(url, params)

        r = verify_reference(self.paper(doi="10.1000/typo"), NoDoiClient())
        self.assertEqual(r["status"], "unverified")

    def test_nearly_identical_title_does_not_silently_pass(self):
        p = self.paper(title="METTL14 promotes immune escape")
        r = verify_reference(p, Client())
        self.assertNotEqual(r["status"], "verified")

    def test_online_and_print_years_are_recorded(self):
        r = verify_reference(self.paper(doi="", year=2023), Client())
        self.assertEqual(r["status"], "verified")
        self.assertEqual(r["metadata"]["years"], [2022, 2023])

    def test_print_only_registry_can_match_retained_publication_date(self):
        p = self.paper(year=2023, dates={"pubmed_journal": "2024-04-18",
                                      "pubmed_article_electronic_0": "2023-09-25",
                                      "pubmed_history_received_0": "2022-10-11"})
        m = {"title": p["title"], "authors": p["authors"], "doi": p["doi"], "pmid": p["pmid"], "years": [2024]}
        self.assertEqual(compare_metadata(p, m)[0], "verified")
        m["years"] = [2022]
        self.assertEqual(compare_metadata(p, m)[0], "mismatch")

    def test_known_retraction_is_flagged(self):
        notice = '<CommentsCorrectionsList><CommentsCorrections RefType="RetractionIn"><PMID>456</PMID></CommentsCorrections></CommentsCorrectionsList>'
        r = verify_reference(self.paper(), Client(notice=notice))
        self.assertEqual(r["publication_status"], "retracted")
        self.assertEqual(r["notice_status"], "detected")

    def test_author_and_year_are_not_ignored(self):
        m = {"title": "METTL3 promotes immune escape", "authors": ["Li X"], "years": [2021], "doi": "10.1000/real"}
        status, checks = compare_metadata(self.paper(), m)
        self.assertEqual(status, "mismatch")
        self.assertEqual(checks["authors"], "mismatch")
        self.assertEqual(checks["year"], "mismatch")


if __name__ == "__main__":
    unittest.main()
