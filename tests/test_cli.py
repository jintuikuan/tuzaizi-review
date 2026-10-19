import datetime as dt
import sys
import json
import tempfile
from argparse import Namespace
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research import date_window, parser
from research import verify_command
from research import fulltext_command
from workflow import create_run, write_jsonl


class DateAndInterfaceTests(unittest.TestCase):
    def test_rolling_five_years_is_not_six_calendar_years(self):
        with patch("research.today", return_value=dt.date(2026, 10, 19)):
            w = date_window(last_years=5)
        self.assertEqual((w["start"], w["end"]), ("2021-10-19", "2026-10-19"))

    def test_explicit_years_and_future_cutoff(self):
        with patch("research.today", return_value=dt.date(2026, 10, 19)):
            w = date_window(years="2021–2026")
        self.assertEqual((w["start"], w["end"]), ("2021-01-01", "2026-10-19"))

    def test_leap_day_and_future_as_of(self):
        with patch("research.today", return_value=dt.date(2024, 2, 29)):
            w = date_window(last_years=5, as_of="2030-01-01")
        self.assertEqual(w["start"], "2019-02-28")
        self.assertEqual(w["end"], "2024-02-29")

    def test_invalid_or_future_range_rejected(self):
        with patch("research.today", return_value=dt.date(2026, 10, 19)):
            for kw in ({"start": "2030-01-01", "end": "2031-01-01"},
                       {"start": "2020-01-01"}, {"last_years": 0}, {"years": "2026:2021"}):
                with self.assertRaises(ValueError):
                    date_window(**kw)

    def test_cli_requires_explicit_selection_record(self):
        args = parser().parse_args(["select", "--run-dir", "run", "--gap", "G2", "--user-selection", "用户选择第2个"])
        self.assertEqual(args.gap, "G2")
        self.assertEqual(args.user_selection, "用户选择第2个")

    def test_correction_review_survives_only_unchanged_notices(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            create_run(run, {"gene": "METTL3"}, {"symbol": "METTL3"})
            write_jsonl(run / "papers.jsonl", [{"paper_id": "P1", "doi": "10.1000/one"}])
            base = {"paper_id": "P1", "status": "verified", "metadata": {"doi": "10.1000/one", "pmid": "1"},
                    "notice_status": "detected", "notices": [{"type": "correction", "pmid": "2"}]}
            old = dict(base, notice_reviewed=True, notice_review_note="Reviewed the correction; quoted result unaffected.")
            path = run / "reference_audit.json"
            path.write_text(json.dumps({"records": [old]}))
            with patch("research.verify_reference", return_value=dict(base)):
                verify_command(Namespace(run_dir=str(run), paper_id=["P1"], cached=True))
            self.assertTrue(json.loads(path.read_text())["records"][0]["notice_reviewed"])
            changed = dict(base, notices=[{"type": "correction", "pmid": "3"}])
            with patch("research.verify_reference", return_value=changed):
                verify_command(Namespace(run_dir=str(run), paper_id=["P1"], cached=True))
            self.assertNotIn("notice_reviewed", json.loads(path.read_text())["records"][0])

    def test_verify_defaults_to_used_references_and_all_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            create_run(run, {"gene": "METTL3"}, {"symbol": "METTL3"})
            write_jsonl(run / "papers.jsonl", [{"paper_id": "P1"}, {"paper_id": "P2"}])
            write_jsonl(run / "evidence.jsonl", [{"evidence_id": "E1", "paper_id": "P1"}])
            def checked(paper, client):
                return {"paper_id": paper["paper_id"], "status": "unverified"}
            with patch("research.verify_reference", side_effect=checked) as checker:
                verify_command(Namespace(run_dir=str(run), paper_id=None, cached=True, all=False))
                self.assertEqual(checker.call_count, 1)
                self.assertEqual(checker.call_args.args[0]["paper_id"], "P1")
            with patch("research.verify_reference", side_effect=checked) as checker:
                verify_command(Namespace(run_dir=str(run), paper_id=None, cached=True, all=True))
                self.assertEqual(checker.call_count, 2)

    def test_fulltext_fallback_discloses_reason_in_log(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            create_run(run, {"gene": "METTL3"}, {"symbol": "METTL3"})
            paper = {"paper_id": "P1", "source_depth": "abstract", "provenance": []}
            write_jsonl(run / "papers.jsonl", [paper])
            fallback = dict(paper, provenance=[{"source": "europepmc_full_text", "status": "unavailable",
                                                "reason": "No unambiguous PMCID"}])
            with patch("research.fetch_full_text", return_value=fallback):
                result = fulltext_command(Namespace(run_dir=str(run), paper_id=["P1"], all=False, refresh=False))
            self.assertEqual(result["results"][0]["outcome"], "unavailable")
            self.assertEqual(result["results"][0]["reason"], "No unambiguous PMCID")
            self.assertEqual(result["results"][0]["source_depth"], "abstract")


if __name__ == "__main__":
    unittest.main()
