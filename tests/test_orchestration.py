"""Offline provenance gates for actual upstream-skill orchestration."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import orchestration as o


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.skill = base / "portable-skill"
        self.run = base / "research-run"
        self.run.mkdir()
        self.state = {"schema_version": 1, "run_id": "test-run", "status": "retrieved",
                      "parameters": {"gene": "METTL3", "topic": "immune escape", "start": "2021-01-01",
                                     "end": "2026-10-21", "word_range": [4000, 6000]},
                      "gene": {"symbol": "METTL3", "gene_id": "56339"},
                      "query_plan": {"pubmed": "METTL3 immune escape", "europepmc": "METTL3 immune escape"},
                      "selected_gap_id": None, "created_at": "old timestamp"}
        self.save_state()
        self.write("papers.jsonl", '{"paper_id":"P1","abstract":"A real fixture excerpt."}\n')
        self.write("evidence.jsonl", '{"evidence_id":"E1","paper_id":"P1"}\n')
        self.write("gaps.json", json.dumps([{"gap_id": "G1", "question": "Which context?"}]))
        self.write("landscape.md", "# Initial landscape\n")
        self.manifest = {"schema_version": 1, "skills": []}
        for identity in dict.fromkeys(s for phase in o.PHASES.values() for s in phase["skills"]):
            entrypoint = f"upstream/{identity}/SKILL.md"
            reference = f"upstream/{identity}/references/rules.md"
            files = {}
            for relative, text in ((entrypoint, f"# Actual fixture instructions for {identity}\n"),
                                   (reference, "Apply the source depth rules.\n")):
                path = self.skill / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                files[relative] = hashlib.sha256(text.encode()).hexdigest()
            self.manifest["skills"].append({"id": identity, "entrypoint": entrypoint,
                                            "repo": "https://github.com/example/fixture", "commit": "a" * 40,
                                            "source_path": identity + "/SKILL.md", "files": files})
        self.save_manifest()

    def tearDown(self):
        self.temporary.cleanup()

    def save_state(self):
        (self.run / "run.json").write_text(json.dumps(self.state), encoding="utf-8")

    def refresh_state(self):
        self.state = json.loads((self.run / "run.json").read_text(encoding="utf-8"))

    def save_manifest(self):
        (self.skill / "upstream" / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def write(self, relative, text):
        path = self.run / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def enable(self):
        result = o.enable(self.run, skill_root=self.skill)
        self.refresh_state()
        return result

    def choose(self):
        self.refresh_state()
        self.state.update(status="selected", selected_gap_id="G1", user_selection="I choose G1", selected_at="2026-10-21T00:00:00Z")
        self.save_state()

    def begin(self, phase):
        return o.begin_phase(self.run, phase, skill_root=self.skill)

    def report(self, begin, *, applications=None, outputs=None):
        phase = begin["phase"]
        path = self.run / "reports" / (begin["invocation_id"] + ".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"summary": f"Applied the {phase} upstream instructions to the supplied evidence.",
                                    "applications": applications if applications is not None else [
                                        {"skill_id": skill, "applied": "Used its workflow and reference rules on the named inputs."}
                                        for skill in o.PHASES[phase]["skills"]],
                                    "outputs": outputs if outputs is not None else o.PHASES[phase]["outputs"]}), encoding="utf-8")
        return path

    def complete(self, phase):
        begin = self.begin(phase)
        for output in o.PHASES[phase]["outputs"]:
            if output == "gaps.json":
                continue
            self.write(output, f"# Applied {phase}\nThe supported statement.\n")
        report = self.report(begin)
        return o.finish_phase(self.run, begin["invocation_id"], report, skill_root=self.skill)

    def discovery(self):
        self.enable()
        for phase in o.DISCOVERY:
            self.complete(phase)

    def test_legacy_runs_are_compatible_until_explicitly_enabled(self):
        self.assertEqual(o.phase_errors(self.run, final=True, skill_root=self.skill / "absent"), [])
        self.assertEqual(o.completed_phase_errors(self.run, "search", skill_root=self.skill / "absent"), [])
        original = dict(self.state)
        self.enable()
        self.assertEqual(self.state["status"], original["status"])
        self.assertEqual(self.state["selected_gap_id"], original["selected_gap_id"])
        self.assertEqual(self.state["orchestration"]["version"], 1)
        again = o.enable(self.run, skill_root=self.skill)
        self.assertEqual(again, self.state)

    def test_legacy_selected_runs_cannot_retroactively_claim_orchestration(self):
        self.choose()
        with self.assertRaisesRegex(ValueError, "before selection"):
            o.enable(self.run, skill_root=self.skill)
        self.refresh_state()
        self.assertNotIn("orchestration", self.state)

    def test_guarded_run_recovers_after_upstream_bundle_repair(self):
        self.enable()
        entry = self.skill / self.manifest["skills"][0]["entrypoint"]
        original = entry.read_text()
        entry.write_text(original + "corrupted resource")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            o.enable(self.run, skill_root=self.skill)
        entry.write_text(original)
        self.assertEqual(o.enable(self.run, skill_root=self.skill)["orchestration"]["version"], 1)
        self.assertEqual(self.begin("search")["execution_status"], "loaded")

    def test_shared_evidence_cannot_be_frozen_as_discovery_output(self):
        self.enable()
        self.complete("search")
        begin = self.begin("landscape")
        report = self.report(begin, outputs=["landscape.md", "evidence.jsonl"])
        with self.assertRaisesRegex(ValueError, "Shared corpus/evidence/logs"):
            o.finish_phase(self.run, begin["invocation_id"], report, skill_root=self.skill)

    def test_plan_and_load_expose_actual_resources_but_do_not_complete(self):
        self.enable()
        self.complete("search")
        previous_events = (self.run / o.EVENTS).read_text()
        plan = o.phase_plan(self.run, "landscape", skill_root=self.skill)
        self.assertEqual(plan["expected_outputs"], ["landscape.md"])
        self.assertEqual(len(plan["skills"]), 2)
        for item in plan["skills"]:
            self.assertTrue(Path(item["entrypoint"]).is_file())
            self.assertTrue(Path(item["resources"][0]).is_file())
        self.assertEqual((self.run / o.EVENTS).read_text(), previous_events)
        loaded = self.begin("landscape")
        self.assertIn("Actual fixture instructions", loaded["skills"][0]["instructions"])
        self.assertEqual(loaded["execution_status"], "loaded")
        errors = o.phase_errors(self.run, skill_root=self.skill)
        self.assertTrue(any("landscape: latest upstream invocation is loaded, not completed" in error for error in errors))
        events = [json.loads(line) for line in (self.run / o.EVENTS).read_text().splitlines()]
        self.assertEqual([event["event"] for event in events], ["loaded", "completed", "loaded"])

    def test_landscape_and_gaps_cannot_start_before_previous_phases_complete(self):
        self.enable()
        for phase in ("landscape", "gaps"):
            with self.assertRaisesRegex(ValueError, "search: missing"):
                o.phase_plan(self.run, phase, skill_root=self.skill)
        self.begin("search")
        with self.assertRaisesRegex(ValueError, "search: latest upstream invocation is loaded, not completed"):
            self.begin("landscape")
        self.complete("search")
        with self.assertRaisesRegex(ValueError, "landscape: missing"):
            self.begin("gaps")
        self.begin("landscape")
        with self.assertRaisesRegex(ValueError, "landscape: latest upstream invocation is loaded, not completed"):
            o.phase_plan(self.run, "gaps", skill_root=self.skill)
        self.complete("landscape")
        self.assertEqual(self.begin("gaps")["execution_status"], "loaded")

    def test_phase_inputs_are_required_only_when_available_before_that_stage(self):
        self.enable()
        self.complete("search")
        (self.run / "papers.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "Missing artifact: papers.jsonl"):
            self.begin("landscape")
        self.write("papers.jsonl", " \n")
        with self.assertRaisesRegex(ValueError, "Empty phase input: papers.jsonl"):
            o.phase_plan(self.run, "landscape", skill_root=self.skill)
        self.write("papers.jsonl", '{"paper_id":"P1"}\n')
        (self.run / "evidence.jsonl").unlink()
        # Evidence is produced while applying the landscape skills.
        self.assertEqual(self.begin("landscape")["inputs"]["evidence.jsonl"], None)
        self.complete("landscape")
        with self.assertRaisesRegex(ValueError, "Missing artifact: evidence.jsonl"):
            self.begin("gaps")
        self.write("evidence.jsonl", '{"evidence_id":"E1"}\n')
        # Targeted gap searches are performed during the gaps phase.
        self.assertFalse((self.run / "gap_searches.jsonl").exists())
        self.assertEqual(o.phase_plan(self.run, "gaps", skill_root=self.skill)["phase"], "gaps")

    def test_review_requires_its_full_input_set_before_loading(self):
        self.discovery()
        self.choose()
        (self.run / "evidence.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "Missing artifact: evidence.jsonl"):
            self.begin("review")
        self.write("evidence.jsonl", "\n")
        with self.assertRaisesRegex(ValueError, "Empty phase input: evidence.jsonl"):
            o.phase_plan(self.run, "review", skill_root=self.skill)

    def test_completed_phase_api_distinguishes_load_completion_and_stale_outputs(self):
        self.enable()
        self.assertTrue(any("missing mandatory" in e for e in o.completed_phase_errors(self.run, "search", skill_root=self.skill)))
        self.begin("search")
        self.assertTrue(any("loaded, not completed" in e for e in o.completed_phase_errors(self.run, "search", skill_root=self.skill)))
        self.complete("search")
        self.assertEqual(o.completed_phase_errors(self.run, "search", skill_root=self.skill), [])
        self.write("search_strategy.md", "A different strategy after completion")
        self.assertTrue(any("outputs were modified" in e for e in o.completed_phase_errors(self.run, "search", skill_root=self.skill)))

    def test_review_and_claims_cannot_begin_before_recorded_human_selection(self):
        self.enable()
        for phase in ("review", "claims"):
            with self.assertRaisesRegex(ValueError, "human selection"):
                self.begin(phase)
        self.state.update(status="selected", selected_gap_id="G1")
        self.save_state()
        with self.assertRaisesRegex(ValueError, "user_selection"):
            self.begin("review")

    def test_missing_skill_application_or_required_output_does_not_complete(self):
        self.enable()
        self.complete("search")
        begin = self.begin("landscape")
        report = self.report(begin, applications=[{"skill_id": begin["skill_ids"][0], "applied": "Applied one skill"}])
        with self.assertRaisesRegex(ValueError, "Missing upstream applications"):
            o.finish_phase(self.run, begin["invocation_id"], report, skill_root=self.skill)
        report = self.report(begin, outputs=["papers.jsonl"])
        with self.assertRaisesRegex(ValueError, "Missing required phase outputs"):
            o.finish_phase(self.run, begin["invocation_id"], report, skill_root=self.skill)
        self.assertTrue(any("loaded, not completed" in e for e in o.phase_errors(self.run, skill_root=self.skill)))

    def test_writing_dispatch_requires_completed_discovery_and_review(self):
        self.enable()
        self.choose()
        with self.assertRaisesRegex(ValueError, "prerequisites incomplete"):
            self.begin("review")
        for phase in o.DISCOVERY:
            self.complete(phase)
        with self.assertRaisesRegex(ValueError, "review: missing"):
            self.begin("claims")
        self.complete("review")
        self.assertEqual(self.begin("claims")["execution_status"], "loaded")

    def test_real_bundle_hash_tamper_is_rejected(self):
        self.enable()
        target = self.skill / self.manifest["skills"][0]["entrypoint"]
        target.write_text("Changed upstream instructions", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bundle hash mismatch"):
            self.begin("search")
        self.assertTrue(any("bundle hash mismatch" in e for e in o.phase_errors(self.run, skill_root=self.skill)))

    def test_missing_mandatory_skill_or_pinned_reference_is_rejected(self):
        missing = self.manifest["skills"].pop()
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "Missing mandatory upstream skills"):
            self.enable()
        self.manifest["skills"].append(missing)
        self.save_manifest()
        resource = next(value for value in missing["files"] if value.endswith("rules.md"))
        (self.skill / resource).unlink()
        with self.assertRaisesRegex(ValueError, "Missing artifact"):
            self.enable()

    def test_declared_upstream_license_hash_is_checked(self):
        license_path = "upstream/LICENSE.fixture"
        content = "Fixture license notice\n"
        (self.skill / license_path).write_text(content, encoding="utf-8")
        self.manifest["licenses"] = {license_path: hashlib.sha256(content.encode()).hexdigest()}
        self.save_manifest()
        self.enable()
        (self.skill / license_path).write_text("Modified notice", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "license hash mismatch"):
            self.begin("search")

    def test_search_accepts_query_edits_during_work_and_binds_final_queries(self):
        self.enable()
        begin = self.begin("search")
        self.state["query_plan"]["pubmed"] = "Final MeSH-aware query"
        self.save_state()
        self.write("search_strategy.md", "The final documented strategy")
        receipt = o.finish_phase(self.run, begin["invocation_id"], self.report(begin), skill_root=self.skill)
        self.assertEqual(receipt["query_plan"]["pubmed"], "Final MeSH-aware query")
        self.complete("landscape")
        self.complete("gaps")
        self.assertEqual(o.phase_errors(self.run, skill_root=self.skill), [])
        self.state["query_plan"]["pubmed"] = "Changed after execution"
        self.save_state()
        self.assertTrue(any("query_plan changed" in e for e in o.phase_errors(self.run, skill_root=self.skill)))

    def test_output_and_report_tamper_invalidate_completion(self):
        self.discovery()
        self.assertEqual(o.phase_errors(self.run, skill_root=self.skill), [])
        self.write("landscape.md", "Changed after completing landscape")
        self.assertTrue(any("landscape: phase outputs were modified" in e for e in o.phase_errors(self.run, skill_root=self.skill)))
        receipt = self.complete("landscape")
        path = self.run / receipt["report_path"]
        report = json.loads(path.read_text())
        report["summary"] = "Different execution assertion"
        path.write_text(json.dumps(report), encoding="utf-8")
        self.assertTrue(any("application report was modified" in e for e in o.phase_errors(self.run, skill_root=self.skill)))

    def test_later_evidence_supplement_does_not_invalidate_discovery(self):
        self.discovery()
        self.write("papers.jsonl", '{"paper_id":"P1"}\n{"paper_id":"P2"}\n')
        self.write("evidence.jsonl", '{"evidence_id":"E1"}\n{"evidence_id":"E2"}\n')
        self.refresh_state()
        self.state.update(status="awaiting_selection", updated_at="new timestamp", record_count=2)
        self.save_state()
        self.assertEqual(o.phase_errors(self.run, skill_root=self.skill), [])

    def test_selection_change_invalidates_review_without_hashing_whole_state(self):
        self.discovery()
        self.choose()
        self.complete("review")
        self.complete("claims")
        self.assertEqual(o.phase_errors(self.run, final=True, skill_root=self.skill), [])
        self.refresh_state()
        self.state.update(status="complete", updated_at="irrelevant timestamp")
        self.save_state()
        self.assertEqual(o.phase_errors(self.run, final=True, skill_root=self.skill), [])
        self.state["user_selection"] = "A different selection instruction"
        self.save_state()
        self.assertTrue(any("selection_sha256" in e for e in o.phase_errors(self.run, final=True, skill_root=self.skill)))

    def test_claims_bind_exact_audited_manuscript_claims_and_evidence(self):
        self.discovery()
        self.choose()
        self.complete("review")
        begin = self.begin("claims")
        self.write("evidence.jsonl", '{"evidence_id":"E1","changed":true}\n')
        self.write("claim_verification.md", "Checked source support")
        with self.assertRaisesRegex(ValueError, "audit inputs changed"):
            o.finish_phase(self.run, begin["invocation_id"], self.report(begin), skill_root=self.skill)
        self.complete("claims")
        self.assertEqual(o.phase_errors(self.run, final=True, skill_root=self.skill), [])
        self.write("claims.jsonl", '{"claim_id":"changed"}\n')
        errors = o.phase_errors(self.run, final=True, skill_root=self.skill)
        self.assertTrue(any("review: phase outputs were modified" in e for e in errors))
        self.assertTrue(any("changed after the upstream audit" in e for e in errors))

    def test_latest_loaded_attempt_cannot_be_mistaken_for_completed_execution(self):
        self.discovery()
        self.begin("gaps")
        self.assertTrue(any("gaps: latest upstream invocation is loaded, not completed" in e
                            for e in o.phase_errors(self.run, skill_root=self.skill)))

    def test_receipts_cannot_be_reused_as_execution_for_another_run(self):
        self.discovery()
        self.refresh_state()
        self.state["run_id"] = "another-run-with-the-same-topic"
        self.save_state()
        self.assertTrue(any("invalid run_id" in e for e in o.phase_errors(self.run, skill_root=self.skill)))

    def test_report_paths_cannot_escape_the_run_or_bundle(self):
        self.enable()
        begin = self.begin("search")
        self.write("search_strategy.md", "Final strategy")
        report = self.report(begin, outputs=["../outside.md", "search_strategy.md"])
        with self.assertRaisesRegex(ValueError, "inside its root"):
            o.finish_phase(self.run, begin["invocation_id"], report, skill_root=self.skill)
        reference = self.manifest["skills"][0]
        reference["entrypoint"] = "../outside.md"
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "inside its root"):
            self.begin("search")

    def test_finish_is_idempotent_for_identical_report_and_artifacts(self):
        self.enable()
        receipt = self.complete("search")
        repeated = o.finish_phase(self.run, receipt["invocation_id"], receipt["report_path"], skill_root=self.skill)
        self.assertEqual(repeated, receipt)
        events = [json.loads(line) for line in (self.run / o.EVENTS).read_text().splitlines()]
        self.assertEqual(sum(event["event"] == "completed" for event in events), 1)


if __name__ == "__main__":
    unittest.main()
