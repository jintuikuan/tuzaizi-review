"""Pinned upstream-skill dispatch and agent execution attestations.

Loading instructions is not execution.  A completed event records an agent's
application report and hashes of its artifacts; it is not proof that scientific
claims are correct.  The evidence and reference validators remain separate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
from typing import Any
import uuid


SKILL_ROOT = Path(__file__).resolve().parents[1]
EVENTS = "orchestration_events.jsonl"
PHASES = {
    "search": {
        "skills": ["aipoch/biomedical-search-strategy-builder"],
        "inputs": [],
        "outputs": ["search_strategy.md"],
        "task": (
            "Apply the upstream search-strategy builder to the supplied gene, date window and topic. "
            "Verify controlled vocabulary where available, separate AND concepts from OR synonyms, "
            "and explain unverified mappings. Write the exact final PubMed and Europe PMC queries, "
            "synonym choices and filters in search_strategy.md. Use the host set-queries command to "
            "save those queries before retrieval. This upstream skill builds strategies; the host "
            "retrieval adapters execute them. Do not invent result counts."
        ),
    },
    "landscape": {
        "skills": ["aipoch/biomarker-landscape-scanner", "aipoch/disease-mechanism-evidence-map"],
        "inputs": ["papers.jsonl", "evidence.jsonl", "search_strategy.md", "search_log.jsonl"],
        "outputs": ["landscape.md"],
        "task": (
            "Apply both upstream landscape and mechanism-evidence skills to the retrieved corpus. "
            "Use their relevant reference modules, distinguish primary studies from reviews, "
            "preprints and adjacent evidence, and map context-specific mechanisms and evidence "
            "depth. Write landscape.md with source-linked directions, independent-study support "
            "and actual coverage limits. Maintain the host evidence.jsonl contract; do not force "
            "a fixed number of directions or treat repeated retellings as independent evidence."
        ),
    },
    "gaps": {
        "skills": [
            "aipoch/medical-research-gap-finder",
            "aipoch/medical-topic-saturation-and-whitespace-checker",
            "aipoch/contradictory-findings-resolver",
            "aipoch/method-gap-detector",
        ],
        "inputs": ["papers.jsonl", "evidence.jsonl", "landscape.md", "gap_searches.jsonl"],
        "outputs": ["gaps.md", "gaps.json"],
        "task": (
            "Apply all four upstream gap, saturation, conflict and method-gap skills. Compare "
            "nearest prior work and study contexts, reject generic pseudo-gaps, and perform actual "
            "targeted disconfirmation searches through the host gap-search command. Write the "
            "host gaps.json plus gaps.md with evidence, scores, assumptions and confidence. "
            "Separate low-confidence exploratory questions from priority opportunities and state "
            "unavailable-source limits. An upstream recommended direction is advisory: stop for "
            "the human choice and do not select a gap or draft a full review."
        ),
    },
    "review": {
        "skills": ["kdense/literature-review"],
        "inputs": ["papers.jsonl", "evidence.jsonl", "landscape.md", "gaps.json", "gaps.md"],
        "outputs": ["manuscript.md", "claims.jsonl"],
        "task": (
            "Invoke the upstream literature-review skill on the human-selected gap and existing "
            "source corpus. Read its relevant bundled resources, supplement evidence where needed, "
            "and write an English thematic narrative review within the configured word range. "
            "Preserve the selected focus, evidence boundaries and actual search window. Produce "
            "manuscript.md using stable [@paper_id] citations and claims.jsonl using the host "
            "claim/evidence contract. Do not silently use paid services, invent citations, or "
            "replace the human selection with the upstream skill's recommendation."
        ),
    },
    "claims": {
        "skills": ["aipoch/paper-to-claim-verifier"],
        "inputs": ["manuscript.md", "claims.jsonl", "evidence.jsonl", "papers.jsonl"],
        "outputs": ["claim_verification.md"],
        "task": (
            "Apply the upstream paper-to-claim verifier and its required reference modules to "
            "the exact manuscript, claims and saved evidence. Check source tracing, support, "
            "context transfer, citation drift and causal language separately from DOI/PMID "
            "identity verification. Write claim_verification.md with traceable verdicts and "
            "remaining issues. If manuscript, claims or evidence need correction, revise them "
            "and rerun the applicable review/claims invocations on the final versions before "
            "recording completion; a completion receipt must describe exactly what was audited."
        ),
    },
}
DISCOVERY = ("search", "landscape", "gaps")
WRITING = {"review", "claims"}
PREREQUISITES = {
    "search": (),
    "landscape": ("search",),
    "gaps": ("search", "landscape"),
    "review": DISCOVERY,
    "claims": DISCOVERY + ("review",),
}
REQUIRED_INPUTS = {
    "search": (),
    "landscape": ("papers.jsonl",),
    "gaps": ("papers.jsonl", "evidence.jsonl", "landscape.md"),
    "review": tuple(PHASES["review"]["inputs"]),
    "claims": tuple(PHASES["claims"]["inputs"]),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"Cannot read {path.name}: {exc}") from exc


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file(root: Path, value: Any, *, must_exist: bool = True) -> tuple[str, Path]:
    if not _nonempty(value) or "\\" in value:
        raise ValueError("Artifact paths must be nonempty relative POSIX paths")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative == PurePosixPath("."):
        raise ValueError(f"Path must stay inside its root: {value}")
    canonical = relative.as_posix()
    resolved = (root / canonical).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Path leaves its root: {value}")
    if must_exist and not resolved.is_file():
        raise ValueError(f"Missing artifact: {canonical}")
    return canonical, resolved


def _state(run_dir: str | Path) -> tuple[Path, dict]:
    root = Path(run_dir).expanduser().resolve()
    state = _json(root / "run.json")
    if not isinstance(state, dict):
        raise ValueError("run.json must contain an object")
    return root, state


def _enabled(state: dict) -> bool:
    config = state.get("orchestration")
    if config is None:
        return False
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Unsupported orchestration configuration; expected version 1")
    return True


def _bundle(skill_root: str | Path | None) -> tuple[Path, dict[str, dict], str]:
    root = Path(skill_root or SKILL_ROOT).expanduser().resolve()
    manifest = _json(root / "upstream" / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("skills"), list):
        raise ValueError("upstream/manifest.json requires schema_version=1 and a skills list")
    skills = {}
    for item in manifest["skills"]:
        if not isinstance(item, dict) or not re.fullmatch(r"(?:aipoch|kdense)/[a-z0-9-]+", str(item.get("id", ""))):
            raise ValueError("Invalid upstream skill ID in manifest")
        identity = item["id"]
        if identity in skills:
            raise ValueError(f"Duplicate upstream skill ID: {identity}")
        if not _nonempty(item.get("repo")) or not _nonempty(item.get("source_path")):
            raise ValueError(f"{identity}: missing repository or source_path provenance")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(item.get("commit", ""))):
            raise ValueError(f"{identity}: commit must be a pinned 40-character SHA")
        files = item.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError(f"{identity}: manifest needs pinned files")
        entrypoint, _ = _file(root, item.get("entrypoint"))
        if entrypoint not in files:
            raise ValueError(f"{identity}: entrypoint is not included in pinned files")
        for relative, expected in files.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"{identity}: invalid SHA-256 for {relative}")
            _, path = _file(root, relative)
            if _file_digest(path) != expected:
                raise ValueError(f"Upstream bundle hash mismatch: {relative}")
        skills[identity] = item
    required = {identity for spec in PHASES.values() for identity in spec["skills"]}
    missing = sorted(required.difference(skills))
    if missing:
        raise ValueError("Missing mandatory upstream skills: " + ", ".join(missing))
    licenses = manifest.get("licenses", {})
    if not isinstance(licenses, dict):
        raise ValueError("Manifest licenses must map relative paths to SHA-256 digests")
    for relative, expected in licenses.items():
        _, path = _file(root, relative)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or _file_digest(path) != expected:
            raise ValueError(f"Upstream license hash mismatch: {relative}")
    return root, skills, _digest(manifest)


def _scope(state: dict) -> dict:
    if not isinstance(state.get("parameters"), dict) or not isinstance(state.get("gene"), dict):
        raise ValueError("run.json requires parameters and gene objects")
    # Stage status, timestamps, counters and invocation logs are intentionally absent.
    return {"parameters": state["parameters"], "gene": state["gene"]}


def _selection(root: Path, state: dict) -> dict:
    if state.get("status") not in {"selected", "draft_ready", "complete"}:
        raise ValueError("Review and claims phases require an explicit human selection first")
    for key in ("selected_gap_id", "user_selection", "selected_at"):
        if not _nonempty(state.get(key)):
            raise ValueError(f"Human selection record is missing {key}")
    gaps = _json(root / "gaps.json")
    if not isinstance(gaps, list):
        raise ValueError("gaps.json must contain an array")
    selected = [gap for gap in gaps if isinstance(gap, dict) and gap.get("gap_id") == state["selected_gap_id"]]
    if len(selected) != 1:
        raise ValueError("Human-selected gap is missing or duplicated in gaps.json")
    return {"selected_gap_id": state["selected_gap_id"], "user_selection": state["user_selection"],
            "selected_at": state["selected_at"], "selected_gap": selected[0]}


def _inputs(root: Path, phase: str, *, required: bool = False) -> dict[str, str | None]:
    result = {}
    for value in PHASES[phase]["inputs"]:
        relative, path = _file(root, value, must_exist=required)
        if required and not path.read_bytes().strip():
            raise ValueError(f"Empty phase input: {relative}")
        result[relative] = _file_digest(path) if path.is_file() else None
    return result


def _events(root: Path) -> list[dict]:
    path = root / EVENTS
    if not path.exists():
        return []
    result = []
    number = 0
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("expected an object")
            result.append(item)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"Cannot read {EVENTS}, record {number}: {exc}") from exc
    return result


def _append(root: Path, item: dict) -> None:
    rows = _events(root)
    rows.append(item)
    path = root / EVENTS
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


def enable(run_dir: str | Path, *, skill_root: str | Path | None = None) -> dict:
    """Opt an unselected legacy run into mandatory upstream orchestration."""
    root, state = _state(run_dir)
    if _enabled(state):
        _bundle(skill_root)
        return state
    if state.get("status") not in {"initialized", "retrieved", "awaiting_selection"}:
        raise ValueError("Enable orchestration before selection; create a new run and reuse the corpus for an already selected/completed legacy run")
    _bundle(skill_root)
    state["orchestration"] = {"version": 1, "enabled_at": _now(), "events_file": EVENTS}
    _atomic_json(root / "run.json", state)
    return state


def _plan(run_dir: str | Path, phase: str, *, skill_root: str | Path | None = None) -> dict:
    if phase not in PHASES:
        raise ValueError(f"Unknown orchestration phase: {phase}")
    root, state = _state(run_dir)
    if not _enabled(state):
        raise ValueError("Run uses legacy mode; explicitly enable orchestration before dispatching phases")
    bundle_root, skills, bundle_hash = _bundle(skill_root)
    selection = _selection(root, state) if phase in WRITING else None
    spec = PHASES[phase]
    instructions = []
    for identity in spec["skills"]:
        item = skills[identity]
        instructions.append({"skill_id": identity, "entrypoint": str((bundle_root / item["entrypoint"]).resolve()),
                             "repo": item["repo"], "commit": item["commit"], "source_path": item["source_path"],
                             "resources": [str((bundle_root / value).resolve()) for value in sorted(item["files"])
                                           if value != item["entrypoint"]]})
    scope = _scope(state)
    return {"phase": phase, "run_id": state.get("run_id"), "task": spec["task"], "scope": scope, "scope_sha256": _digest(scope),
            "selection": selection, "selection_sha256": _digest(selection) if selection is not None else None,
            "skills": instructions, "skill_ids": list(spec["skills"]), "bundle_sha256": bundle_hash,
            "inputs": _inputs(root, phase), "expected_outputs": list(spec["outputs"]),
            "query_plan": state.get("query_plan") if phase == "search" else None,
            "execution_status": "planned", "attestation_only": True}


def phase_plan(run_dir: str | Path, phase: str, *, skill_root: str | Path | None = None) -> dict:
    """Return concrete upstream tasks and resources without claiming execution."""
    plan = _plan(run_dir, phase, skill_root=skill_root)
    root, state = _state(run_dir)
    if PREREQUISITES[phase]:
        rows = _events(root)
        prerequisites = []
        for previous in PREREQUISITES[phase]:
            previous_plan = _plan(root, previous, skill_root=skill_root)
            prerequisites.extend(_history_errors(root, state, previous_plan, rows))
        if prerequisites:
            raise ValueError("Upstream prerequisites incomplete: " + "; ".join(prerequisites))
    for value in REQUIRED_INPUTS[phase]:
        relative, path = _file(root, value)
        if not path.read_bytes().strip():
            raise ValueError(f"Empty phase input: {relative}")
    return plan


def begin_phase(run_dir: str | Path, phase: str, *, skill_root: str | Path | None = None) -> dict:
    """Load actual entrypoints and log only 'loaded', never 'executed'."""
    plan = phase_plan(run_dir, phase, skill_root=skill_root)
    root, _ = _state(run_dir)
    if phase == "claims":
        plan["inputs"] = _inputs(root, phase, required=True)
    loaded = [{**skill, "instructions": Path(skill["entrypoint"]).read_text(encoding="utf-8")}
              for skill in plan["skills"]]
    event = {key: plan[key] for key in ("phase", "run_id", "scope_sha256", "selection_sha256", "skill_ids",
                                       "bundle_sha256", "inputs", "expected_outputs", "query_plan")}
    event.update(event="loaded", invocation_id="I" + uuid.uuid4().hex, loaded_at=_now())
    _append(root, event)
    return {**plan, "skills": loaded, "invocation_id": event["invocation_id"], "execution_status": "loaded",
            "next_action": "Apply each upstream skill to the task, create the artifacts, then finish with an honest application report. Loading alone does not satisfy any phase gate."}


def _report(root: Path, report_path: str | Path, phase: str) -> tuple[str, dict, dict[str, str]]:
    if not isinstance(report_path, (str, Path)) or not str(report_path).strip():
        raise ValueError("Application report requires a path inside the run directory")
    report_file = Path(report_path).expanduser()
    if report_file.is_absolute():
        try:
            relative = report_file.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("Application report must be saved inside the run directory") from exc
    else:
        relative = report_file.as_posix()
    relative, path = _file(root, relative)
    report = _json(path)
    if not isinstance(report, dict) or not _nonempty(report.get("summary")):
        raise ValueError("Application report requires a nonempty summary")
    applications = report.get("applications")
    if not isinstance(applications, list):
        raise ValueError("Application report requires applications[]")
    applied = {}
    for application in applications:
        if not isinstance(application, dict) or not _nonempty(application.get("skill_id")):
            raise ValueError("Each application requires skill_id and a nonempty applied explanation")
        identity = application["skill_id"]
        if identity in applied or identity not in PHASES[phase]["skills"]:
            raise ValueError(f"Unexpected or duplicate application skill: {identity}")
        if not _nonempty(application.get("applied")):
            raise ValueError(f"{identity}: applied must explain how the upstream skill was used")
        applied[identity] = application["applied"]
    missing = set(PHASES[phase]["skills"]).difference(applied)
    if missing:
        raise ValueError("Missing upstream applications: " + ", ".join(sorted(missing)))
    outputs = report.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("Application report requires outputs[] relative to the run directory")
    hashes = {}
    for output in outputs:
        name, artifact = _file(root, output)
        if name in {"run.json", EVENTS}:
            raise ValueError("State and invocation logs cannot be phase outputs")
        if name in hashes:
            raise ValueError(f"Duplicate phase output: {name}")
        if not artifact.read_bytes().strip():
            raise ValueError(f"Empty phase output: {name}")
        hashes[name] = _file_digest(artifact)
    missing = set(PHASES[phase]["outputs"]).difference(hashes)
    if missing:
        raise ValueError("Missing required phase outputs: " + ", ".join(sorted(missing)))
    if phase in DISCOVERY:
        shared = {"papers.jsonl", "evidence.jsonl", "search_log.jsonl", "gap_searches.jsonl"}.intersection(hashes)
        if shared:
            raise ValueError("Shared corpus/evidence/logs must not be frozen as discovery outputs: " + ", ".join(sorted(shared)))
    return relative, report, hashes


def finish_phase(run_dir: str | Path, invocation_id: str, report_path: str | Path,
                 *, skill_root: str | Path | None = None) -> dict:
    """Record an agent attestation only after its explicit report and outputs exist."""
    root, state = _state(run_dir)
    if not _enabled(state):
        raise ValueError("Cannot finish an upstream invocation in legacy mode")
    rows = _events(root)
    started = [row for row in rows if row.get("event") == "loaded" and row.get("invocation_id") == invocation_id]
    if len(started) != 1:
        raise ValueError("Unknown or duplicated loaded invocation_id")
    loaded = started[0]
    phase = loaded.get("phase")
    plan = phase_plan(root, phase, skill_root=skill_root)
    for key in ("run_id", "scope_sha256", "selection_sha256", "skill_ids", "bundle_sha256", "expected_outputs"):
        if loaded.get(key) != plan[key]:
            raise ValueError(f"Invocation inputs changed ({key}); begin a new {phase} invocation")
    if phase == "claims" and loaded.get("inputs") != _inputs(root, phase, required=True):
        raise ValueError("Claims audit inputs changed; begin a new audit on the final manuscript, claims and evidence")
    relative, report, hashes = _report(root, report_path, phase)
    report_hash = _file_digest(root / relative)
    completed = [row for row in rows if row.get("event") == "completed" and row.get("invocation_id") == invocation_id]
    if completed:
        previous = completed[-1]
        if previous.get("report_sha256") == report_hash and previous.get("output_sha256") == hashes:
            return previous
        raise ValueError("Invocation already completed with different artifacts; begin a new invocation")
    event = {key: loaded[key] for key in ("phase", "run_id", "invocation_id", "scope_sha256", "selection_sha256", "skill_ids",
                                         "bundle_sha256", "expected_outputs")}
    event.update(event="completed", completed_at=_now(), attestation_only=True,
                 report_path=relative, report_sha256=report_hash, summary=report["summary"],
                 applications=report["applications"], output_sha256=hashes,
                 input_sha256=_inputs(root, phase),
                 query_plan_sha256=_digest(state.get("query_plan")) if phase == "search" else None,
                 query_plan=state.get("query_plan") if phase == "search" else None)
    _append(root, event)
    return event


def _receipt_errors(root: Path, state: dict, plan: dict, loaded: dict, receipt: dict) -> list[str]:
    phase = plan["phase"]
    errors = []
    for key in ("run_id", "scope_sha256", "selection_sha256", "skill_ids", "bundle_sha256", "expected_outputs"):
        if loaded.get(key) != plan[key] or receipt.get(key) != plan[key]:
            errors.append(f"{phase}: upstream receipt has stale or invalid {key}")
    if receipt.get("phase") != phase or receipt.get("invocation_id") != loaded.get("invocation_id"):
        errors.append(f"{phase}: completion does not match its loaded invocation")
    try:
        relative, _, hashes = _report(root, receipt.get("report_path", ""), phase)
        if _file_digest(root / relative) != receipt.get("report_sha256"):
            errors.append(f"{phase}: application report was modified after completion")
        if hashes != receipt.get("output_sha256"):
            errors.append(f"{phase}: phase outputs were modified after completion")
    except (OSError, ValueError) as exc:
        errors.append(f"{phase}: {exc}")
    if phase == "search" and receipt.get("query_plan_sha256") != _digest(state.get("query_plan")):
        errors.append("search: configured query_plan changed after the upstream strategy was completed")
    if phase == "claims":
        try:
            if receipt.get("input_sha256") != _inputs(root, phase, required=True):
                errors.append("claims: manuscript, claims or evidence changed after the upstream audit")
        except (OSError, ValueError) as exc:
            errors.append(f"claims: {exc}")
    return errors


def _history_errors(root: Path, state: dict, plan: dict, rows: list[dict]) -> list[str]:
    phase = plan["phase"]
    attempts = [row for row in rows if row.get("event") == "loaded" and row.get("phase") == phase]
    if not attempts:
        return [f"{phase}: missing mandatory upstream invocation"]
    loaded = attempts[-1]
    receipts = [row for row in rows if row.get("event") == "completed"
                and row.get("invocation_id") == loaded.get("invocation_id")]
    if not receipts:
        return [f"{phase}: latest upstream invocation is loaded, not completed"]
    return _receipt_errors(root, state, plan, loaded, receipts[-1])


def completed_phase_errors(run_dir: str | Path, phase: str, *, skill_root: str | Path | None = None) -> list[str]:
    """Check one current completed phase, preserving legacy-run behavior."""
    try:
        root, state = _state(run_dir)
        if not _enabled(state):
            return []
        plan = _plan(root, phase, skill_root=skill_root)
        return _history_errors(root, state, plan, _events(root))
    except (OSError, ValueError) as exc:
        return [str(exc)]


def phase_errors(run_dir: str | Path, final: bool = False, *, skill_root: str | Path | None = None) -> list[str]:
    """Check provenance gates, leaving scientific quality to existing validators."""
    try:
        root, state = _state(run_dir)
        if not _enabled(state):
            return []
        rows = _events(root)
        _bundle(skill_root)
    except (OSError, ValueError) as exc:
        return [str(exc)]
    errors = []
    for phase in PHASES if final else DISCOVERY:
        try:
            plan = _plan(root, phase, skill_root=skill_root)
        except (OSError, ValueError) as exc:
            errors.append(f"{phase}: {exc}")
            continue
        errors.extend(_history_errors(root, state, plan, rows))
    return errors
