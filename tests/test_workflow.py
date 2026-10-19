"""Evidence and human-selection gates, with no live services."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import workflow as w


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        w.create_run(self.root, {"gene": "METTL3", "topic": "tumor immune escape", "start": "2021-01-01",
                                "end": "2026-10-19", "as_of": "2026-10-19", "concepts": [],
                                "word_range": [1, 100]}, {"symbol": "METTL3"})
        self.paper = {"paper_id": "P0001", "title": "A METTL3 mechanism", "authors": ["Researcher A"],
                      "year": 2024, "doi": "10.1000/example", "pmid": "1234", "pmcid": None,
                      "publication_date": "2024-04-01", "abstract": "METTL3 regulates immune escape in a model.",
                      "source_depth": "abstract", "is_preprint": False, "related_dois": [], "sources": [], "provenance": []}
        self.evidence = {"evidence_id": "E001", "paper_id": "P0001", "direction": "mechanism", "claim": "METTL3 regulates escape",
                         "source_depth": "abstract", "locator": "Abstract, sentence 1",
                         "quote": "METTL3 regulates immune escape in a model.", "mechanism": "", "pathway": "",
                         "cancer_type": "", "cell_type": "", "organism": "", "experiment_type": "", "support": "supported"}
        self.gap = {"gap_id": "G1", "title": "Context dependence", "question": "Which contexts explain dependence?",
                    "evidence_ids": ["E001"], "nearest_paper_ids": ["P0001"], "missing_evidence": "Cross-context replication",
                    "validation_idea": "Compare independently measured contexts", "novelty": {"score": 3, "rationale": "Bounded search"},
                    "feasibility": {"score": 3, "rationale": "Existing assays", "resource_assumptions": "Access to models"},
                    "confidence": "low", "disconfirmation": {"query_ids": ["Q1"], "conclusion": "No direct report found in searched sources"}}
        self.search = {"query_id": "Q1", "source": "pubmed", "query": "METTL3 context immune escape",
                       "reported_count": 0, "retrieved_count": 0, "complete": True, "errors": []}
        self.core_search = {"query_id": "CORE1", "source": "pubmed", "query": "METTL3 tumor immune escape",
                            "type": "summary", "purpose": "core", "reported_count": 1, "retrieved_count": 1,
                            "complete": True, "errors": [], "limit": None, "limit_reached": False}
        w.update_run(self.root, coverage="complete_pagination", smoke_limit=None)
        self.save_inputs()

    def tearDown(self):
        self.temporary.cleanup()

    def save_inputs(self):
        w.write_jsonl(self.root / "papers.jsonl", [self.paper])
        w.write_jsonl(self.root / "evidence.jsonl", [self.evidence])
        w.write_jsonl(self.root / "gap_searches.jsonl", [self.search])
        w.write_jsonl(self.root / "search_log.jsonl", [self.core_search])
        (self.root / "gaps.json").write_text(json.dumps([self.gap]), encoding="utf-8")
        (self.root / "landscape.md").write_text("# Landscape\nEvidence map.", encoding="utf-8")
        (self.root / "gaps.md").write_text("# Gaps\nCandidate G1.", encoding="utf-8")

    def select(self):
        w.prepare_selection(self.root)
        w.select_gap(self.root, "G1", "I choose G1")

    def draft(self):
        (self.root / "manuscript.md").write_text("# Review\n\nMETTL3 regulates immune escape [@P0001].\n", encoding="utf-8")
        w.write_jsonl(self.root / "claims.jsonl", [{"claim_id": "C1", "text": "METTL3 regulates immune escape",
                                                   "paper_ids": ["P0001"], "evidence_ids": ["E001"]}])
        (self.root / "reference_audit.json").write_text(json.dumps({"records": [
            {"paper_id": "P0001", "status": "verified", "doi": "10.1000/example", "pmid": "1234"}]}), encoding="utf-8")

    def test_gate_and_resume_require_human_selection(self):
        state = w.prepare_selection(self.root)
        self.assertEqual(state["status"], "awaiting_selection")
        self.assertEqual(w.load_run(self.root)["selected_gap_id"], None)
        self.draft()
        with self.assertRaisesRegex(ValueError, "export refused"):
            w.export_review(self.root, docx=False)
        with self.assertRaisesRegex(ValueError, "human selection"):
            w.select_gap(self.root, "G1", " ")
        state = w.select_gap(self.root, "G1", "Select G1")
        self.assertEqual(state["selected_gap_id"], "G1")
        self.assertEqual(state["user_selection"], "Select G1")
        self.assertTrue(w.validate_run(self.root, final=True)["ok"])

    def test_update_cannot_bypass_selection(self):
        with self.assertRaises(ValueError):
            w.update_run(self.root, status="selected")
        with self.assertRaises(ValueError):
            w.update_run(self.root, selected_gap_id="G1")
        with self.assertRaises(ValueError):
            w.update_run(self.root, status="complete")

    def test_jsonl_round_trip_preserves_unicode_line_separators_in_strings(self):
        rows = [{"paper_id": "P1", "abstract": "Line\u2028separator\u2029paragraph\u0085next"},
                {"paper_id": "P2", "abstract": "A second record"}]
        path = self.root / "unicode.jsonl"
        w.write_jsonl(path, rows)
        self.assertIn("\u2028", path.read_text(encoding="utf-8"))
        self.assertIn("\u2029", path.read_text(encoding="utf-8"))
        self.assertIn("\u0085", path.read_text(encoding="utf-8"))
        self.assertEqual(w.load_jsonl(path), rows)

    def test_missing_or_incomplete_disconfirmation_blocks_gate(self):
        self.search["complete"] = False
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            w.prepare_selection(self.root)
        self.search["complete"] = True
        self.search["reported_count"] = 2
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, "counts"):
            w.prepare_selection(self.root)

    def test_quote_and_depth_are_checked_against_saved_source(self):
        self.evidence["quote"] = "An invented mechanism"
        self.save_inputs()
        self.assertFalse(w.validate_run(self.root)["ok"])
        self.evidence["source_depth"] = "full_text"
        self.evidence["quote"] = self.paper["abstract"]
        self.save_inputs()
        self.assertTrue(any("full_text_path" in e for e in w.validate_run(self.root)["errors"]))
        (self.root / "paper.txt").write_text("Results\n" + self.evidence["quote"], encoding="utf-8")
        self.paper["full_text_path"] = "paper.txt"
        self.paper["source_depth"] = "full_text"
        self.save_inputs()
        self.assertTrue(w.validate_run(self.root)["ok"])

    def test_abstract_cannot_support_full_text_claim(self):
        self.select()
        self.draft()
        claim = w.load_jsonl(self.root / "claims.jsonl")[0]
        claim["required_depth"] = "full_text"
        w.write_jsonl(self.root / "claims.jsonl", [claim])
        self.assertTrue(any("abstract-only" in e for e in w.validate_run(self.root, final=True)["errors"]))

    def test_doi_mismatch_or_unsupported_claim_blocks_final(self):
        self.select()
        self.draft()
        (self.root / "reference_audit.json").write_text(json.dumps({"records": [
            {"paper_id": "P0001", "status": "verified", "metadata": {"doi": "10.1000/wrong"}}]}), encoding="utf-8")
        self.assertTrue(any("doi mismatch" in e for e in w.validate_run(self.root, final=True)["errors"]))
        self.draft()
        self.evidence["support"] = "partial"
        self.save_inputs()
        self.assertTrue(any("not supported" in e for e in w.validate_run(self.root, final=True)["errors"]))

    def test_markdown_export_uses_stable_numbered_references(self):
        self.select()
        self.draft()
        paths = w.export_review(self.root, docx=False)
        markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
        self.assertIn("immune escape [1]", markdown)
        self.assertIn("1. Researcher A. A METTL3 mechanism", markdown)
        self.assertNotIn("@P0001", markdown)
        self.assertIn("@article{P0001", Path(paths["references_bib"]).read_text())
        self.assertEqual(w.load_run(self.root)["status"], "complete")
        self.assertTrue(Path(paths["evidence_csv"]).is_file())

    def test_publication_notices_block_unsafe_claim_support(self):
        self.select()
        self.draft()
        record = {"paper_id": "P0001", "status": "verified", "publication_status": "retracted"}
        (self.root / "reference_audit.json").write_text(json.dumps({"records": [record]}), encoding="utf-8")
        self.assertTrue(any("retracted" in e for e in w.validate_run(self.root, final=True)["errors"]))
        record["publication_status"] = "expression_of_concern"
        (self.root / "reference_audit.json").write_text(json.dumps({"records": [record]}), encoding="utf-8")
        self.assertFalse(w.validate_run(self.root, final=True)["ok"])
        record["publication_status"] = "corrected"
        (self.root / "reference_audit.json").write_text(json.dumps({"records": [record]}), encoding="utf-8")
        self.assertTrue(any("notice_reviewed" in e for e in w.validate_run(self.root, final=True)["errors"]))
        record["notice_reviewed"] = True
        (self.root / "reference_audit.json").write_text(json.dumps({"records": [record]}), encoding="utf-8")
        self.assertTrue(w.validate_run(self.root, final=True)["ok"])

    def test_historical_gap_verification_cannot_silently_enter_review(self):
        self.select()
        self.draft()
        self.paper["publication_date"] = "2010-01-01"
        self.paper["corpus_role"] = "gap_verification"
        self.save_inputs()
        self.assertTrue(any("outside the review" in e for e in w.validate_run(self.root, final=True)["errors"]))
        state = w.load_run(self.root)
        parameters = dict(state["parameters"], allow_background_refs=True)
        w.update_run(self.root, parameters=parameters)
        self.assertTrue(w.validate_run(self.root, final=True)["ok"])

    def test_partial_date_needs_a_recorded_review(self):
        self.select()
        self.draft()
        self.paper["publication_date"] = "2024"
        self.save_inputs()
        self.assertTrue(any("date_scope_review" in e for e in w.validate_run(self.root, final=True)["errors"]))
        self.paper["date_scope_review"] = {"eligible": True, "rationale": "The entire indexed year is inside the specified window"}
        self.save_inputs()
        report = w.validate_run(self.root, final=True)
        self.assertTrue(report["ok"])
        self.assertTrue(any("manual date_scope_review" in warning for warning in report["warnings"]))

    def test_literal_doi_and_pmid_must_match_cited_corpus(self):
        self.select()
        self.draft()
        with (self.root / "manuscript.md").open("a", encoding="utf-8") as handle:
            handle.write("\nPMID: 9999; https://doi.org/10.1000/invented\n")
        report = w.validate_run(self.root, final=True)
        self.assertTrue(any("PMID 9999" in e for e in report["errors"]))
        self.assertTrue(any("DOI 10.1000/invented" in e for e in report["errors"]))

    def test_metadata_only_uncited_papers_can_coexist_with_supported_corpus(self):
        metadata = dict(self.paper, paper_id="P0002", title="A discovery-only record", source_depth="metadata", abstract="")
        w.write_jsonl(self.root / "papers.jsonl", [self.paper, metadata])
        self.select()
        self.draft()
        self.assertTrue(w.validate_run(self.root, final=True)["ok"])

    def test_metadata_only_papers_cannot_supply_usable_evidence(self):
        self.paper["source_depth"] = "metadata"
        self.save_inputs()
        report = w.validate_run(self.root)
        self.assertTrue(any("metadata-only paper cannot" in error for error in report["errors"]))
        self.evidence["source_depth"] = "metadata"
        self.save_inputs()
        self.assertTrue(any("invalid source_depth" in error for error in w.validate_run(self.root)["errors"]))

    def test_claim_coverage_must_refer_to_text_actually_in_manuscript(self):
        self.select()
        self.draft()
        claims = w.load_jsonl(self.root / "claims.jsonl")
        claims[0]["text"] = "A different conclusion that the manuscript never states"
        w.write_jsonl(self.root / "claims.jsonl", claims)
        self.assertTrue(any("claim text is absent" in error for error in w.validate_run(self.root, final=True)["errors"]))

    def test_capped_core_cannot_graduate_until_full_query_rerun(self):
        self.core_search.update(reported_count=10, retrieved_count=1, complete=False, limit=1, limit_reached=True)
        self.save_inputs()
        w.update_run(self.root, status="retrieved", coverage="partial_or_failed", smoke_limit=1)
        # A complete, independent zero-hit gap search cannot repair core coverage.
        with self.assertRaisesRegex(ValueError, "Core retrieval"):
            w.prepare_selection(self.root)
        # Merely changing the state label cannot hide the capped source summary.
        w.update_run(self.root, coverage="complete_pagination", smoke_limit=None)
        with self.assertRaisesRegex(ValueError, "capped"):
            w.prepare_selection(self.root)
        full = dict(self.core_search, query_id="CORE2", retrieved_count=10, complete=True, limit=None, limit_reached=False)
        w.write_jsonl(self.root / "search_log.jsonl", [self.core_search, full])
        self.assertEqual(w.prepare_selection(self.root)["status"], "awaiting_selection")
        w.select_gap(self.root, "G1", "Choose G1 after the complete search")
        self.draft()
        self.assertTrue(w.validate_run(self.root, final=True)["ok"])
        # The most recent attempt controls; an older success cannot hide a new failure.
        w.write_jsonl(self.root / "search_log.jsonl", [self.core_search, full, self.core_search])
        self.assertFalse(w.validate_run(self.root, final=True)["ok"])

    def test_core_summary_is_required_even_without_a_query_plan(self):
        w.write_jsonl(self.root / "search_log.jsonl", [dict(self.core_search, purpose="gap_verification")])
        with self.assertRaisesRegex(ValueError, "purpose=core"):
            w.prepare_selection(self.root)

    def test_each_configured_source_requires_matching_complete_core_query(self):
        pubmed = "METTL3 AND immune escape"
        epmc = "TITLE_ABS:METTL3 AND TITLE_ABS:immune"
        w.update_run(self.root, query_plan={"pubmed": pubmed, "europepmc": epmc})
        p = dict(self.core_search, query=pubmed)
        e = dict(self.core_search, query_id="CORE2", source="europepmc",
                 query=f"({epmc}) AND FIRST_PDATE:[2021-01-01 TO 2026-10-19]")
        w.write_jsonl(self.root / "search_log.jsonl", [p])
        with self.assertRaisesRegex(ValueError, "configured source europepmc"):
            w.prepare_selection(self.root)
        w.write_jsonl(self.root / "search_log.jsonl", [p, dict(e, query="unrelated query")])
        with self.assertRaisesRegex(ValueError, "query_plan query"):
            w.prepare_selection(self.root)
        w.write_jsonl(self.root / "search_log.jsonl", [p, e])
        self.assertEqual(w.prepare_selection(self.root)["status"], "awaiting_selection")

    def test_docx_dependency_error_is_explicit(self):
        self.select()
        self.draft()
        with mock.patch.object(w.importlib, "import_module", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(ValueError, "python-docx"):
                w.export_review(self.root)
        self.assertFalse((self.root / "review.md").exists())

    @unittest.skipUnless(importlib.util.find_spec("docx"), "python-docx unavailable in this runtime")
    def test_docx_and_markdown_reference_consistency(self):
        self.select()
        self.draft()
        paths = w.export_review(self.root)
        from docx import Document
        text = "\n".join(p.text for p in Document(paths["docx"]).paragraphs)
        self.assertIn("immune escape [1]", text)
        self.assertIn("1. Researcher A. A METTL3 mechanism", text)
        self.assertIn("PMID: 1234", text)


if __name__ == "__main__":
    unittest.main()
