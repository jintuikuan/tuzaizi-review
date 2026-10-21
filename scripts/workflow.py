"""Run state, evidence gates, and deterministic review exports.

This module deliberately performs no literature discovery or scientific scoring.
Those decisions and their source excerpts are supplied by the research agent.
"""
from __future__ import annotations

import csv
import importlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from orchestration import phase_errors


STATUSES = {"initialized", "retrieved", "awaiting_selection", "selected", "draft_ready", "complete"}
SUPPORT = {"supported", "partial", "unsupported", "unassessable"}
DEPTHS = {"abstract", "full_text"}
CITATION = re.compile(r"\[([^\]\n]*@[A-Za-z][A-Za-z0-9_-]*[^\]\n]*)\]")
PAPER_CITE = re.compile(r"@([A-Za-z][A-Za-z0-9_-]*)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {path.name}: {exc}") from exc


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_jsonl(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    # JSONL records are LF-delimited. Unicode paragraph/line separators are legal
    # inside JSON strings and must not become record boundaries.
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{path.name}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name}:{number}: expected an object")
        rows.append(row)
    return rows


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    path = Path(path)
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("JSONL rows must be objects")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


def create_run(run_dir: str | Path, parameters: dict, gene: dict) -> dict:
    run_dir = Path(run_dir)
    if (run_dir / "run.json").exists():
        raise ValueError("Run already exists; resume it instead of overwriting run.json")
    parameters = dict(parameters)
    parameters.setdefault("language", "en")
    parameters.setdefault("word_range", [4000, 6000])
    timestamp = _now()
    state = {"schema_version": 1, "run_id": uuid.uuid4().hex, "status": "initialized",
             "parameters": parameters, "gene": dict(gene), "selected_gap_id": None,
             "created_at": timestamp, "updated_at": timestamp}
    _save(run_dir / "run.json", state)
    return state


def load_run(run_dir: str | Path) -> dict:
    state = _json(Path(run_dir) / "run.json")
    if not isinstance(state, dict):
        raise ValueError("run.json must contain an object")
    return state


def update_run(run_dir: str | Path, **changes: Any) -> dict:
    state = load_run(run_dir)
    if "selected_gap_id" in changes or "user_selection" in changes:
        raise ValueError("Use select_gap to record an explicit human selection")
    status = changes.get("status", state.get("status"))
    if status not in STATUSES:
        raise ValueError(f"Unknown run status: {status}")
    if status in {"awaiting_selection", "selected"} and status != state.get("status"):
        raise ValueError("Use prepare_selection or select_gap for gated state transitions")
    if status in {"draft_ready", "complete"} and state.get("status") not in {"selected", "draft_ready", "complete"}:
        raise ValueError("A human-selected gap is required before drafting or completing a review")
    if status == "complete":
        report = validate_run(run_dir, final=True)
        if not report["ok"]:
            raise ValueError("Cannot complete run: " + "; ".join(report["errors"]))
    state.update(changes)
    state["updated_at"] = _now()
    _save(Path(run_dir) / "run.json", state)
    return state


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize(value: str) -> str:
    return " ".join(value.split()).strip()


def _doi(value: Any) -> str:
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value or "").strip(), flags=re.I).lower()


def _title(value: Any) -> str:
    return re.sub(r"[^\w]+", "", str(value or "").casefold())


def _index(rows: list[dict], key: str, errors: list[str]) -> dict[str, dict]:
    result = {}
    for row in rows:
        identity = row.get(key)
        if not _present(identity):
            errors.append(f"Missing {key}")
        elif identity in result:
            errors.append(f"Duplicate {key}: {identity}")
        else:
            result[identity] = row
    return result


def _text_path(run_dir: Path, paper: dict) -> Path | None:
    value = paper.get("full_text_path")
    if not _present(value):
        return None
    path = Path(value)
    return path if path.is_absolute() else run_dir / path


def _evidence_check(run_dir: Path, row: dict, papers: dict, errors: list[str]) -> None:
    identity = row.get("evidence_id", "unknown evidence")
    for key in ("evidence_id", "paper_id", "claim", "locator", "quote"):
        if not _present(row.get(key)):
            errors.append(f"{identity}: missing nonempty {key}")
    if row.get("source_depth") not in DEPTHS:
        errors.append(f"{identity}: invalid source_depth")
    if row.get("support") not in SUPPORT:
        errors.append(f"{identity}: invalid support status")
    paper = papers.get(row.get("paper_id"))
    if paper is None:
        errors.append(f"{identity}: unknown paper_id {row.get('paper_id')}")
        return
    if paper.get("source_depth") == "metadata":
        errors.append(f"{identity}: metadata-only paper cannot supply usable evidence")
    text = ""
    if row.get("source_depth") == "abstract":
        text = paper.get("abstract") or ""
    elif row.get("source_depth") == "full_text":
        path = _text_path(run_dir, paper)
        if path is None or not path.is_file():
            errors.append(f"{identity}: full_text evidence requires a saved full_text_path")
        else:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"{identity}: cannot read saved full text: {exc}")
    quote = row.get("quote")
    if _present(quote) and _normalize(quote) not in _normalize(text):
        errors.append(f"{identity}: quote not found in saved {row.get('source_depth')} source")


def _gap_check(gap: dict, evidence: dict, papers: dict, searches: dict,
               gene_terms: list[str], errors: list[str]) -> None:
    identity = gap.get("gap_id", "unknown gap")
    for field in ("gap_id", "title", "question", "missing_evidence", "validation_idea"):
        if not _present(gap.get(field)):
            errors.append(f"{identity}: missing nonempty {field}")
    for field, mapping in (("evidence_ids", evidence), ("nearest_paper_ids", papers)):
        values = gap.get(field)
        if not isinstance(values, list) or not values:
            errors.append(f"{identity}: {field} must be a nonempty list")
            continue
        for value in values:
            if not isinstance(value, str) or value not in mapping:
                errors.append(f"{identity}: unknown {field} entry {value}")
    for eid in gap.get("evidence_ids", []) if isinstance(gap.get("evidence_ids"), list) else []:
        if isinstance(eid, str) and eid in evidence and evidence[eid].get("support") not in {"supported", "partial"}:
            errors.append(f"{identity}: gap evidence {eid} is unsupported or unassessable")
    for field in ("novelty", "feasibility"):
        rating = gap.get(field)
        if not isinstance(rating, dict):
            errors.append(f"{identity}: {field} must contain score and rationale")
            continue
        score = rating.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 1 <= score <= 5:
            errors.append(f"{identity}: {field}.score must be between 1 and 5")
        if not _present(rating.get("rationale")):
            errors.append(f"{identity}: missing {field}.rationale")
        if field == "feasibility" and not rating.get("resource_assumptions"):
            errors.append(f"{identity}: missing feasibility.resource_assumptions")
    if gap.get("confidence") not in {"high", "medium", "low"}:
        errors.append(f"{identity}: invalid confidence")
    check = gap.get("disconfirmation")
    if not isinstance(check, dict) or not _present(check.get("conclusion")):
        errors.append(f"{identity}: missing disconfirmation conclusion")
        check = check if isinstance(check, dict) else {}
    queries = check.get("query_ids")
    if not isinstance(queries, list) or not queries:
        errors.append(f"{identity}: disconfirmation requires actual targeted query_ids")
        return
    for qid in queries:
        search = searches.get(qid) if isinstance(qid, str) else None
        if search is None:
            errors.append(f"{identity}: unknown disconfirmation query {qid}")
            continue
        if search.get("complete") is not True or search.get("errors"):
            errors.append(f"{identity}: disconfirmation query {qid} is incomplete or failed")
        reported, retrieved = search.get("reported_count"), search.get("retrieved_count")
        if (not isinstance(reported, int) or isinstance(reported, bool) or reported < 0 or
                not isinstance(retrieved, int) or isinstance(retrieved, bool) or retrieved != reported):
            errors.append(f"{identity}: disconfirmation query {qid} counts do not demonstrate complete retrieval")
        query = search.get("query")
        if not _present(query) or not _present(search.get("source")):
            errors.append(f"{identity}: query {qid} lacks source or exact query")
        elif gene_terms and not any(term.casefold() in query.casefold() for term in gene_terms):
            errors.append(f"{identity}: query {qid} does not contain the selected gene")


def _read_collection(run_dir: Path, name: str, errors: list[str], jsonl: bool = True) -> list[dict]:
    path = run_dir / name
    if not path.exists():
        return []
    try:
        rows = load_jsonl(path) if jsonl else _json(path)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{name} must contain a list of objects")
        return rows
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(str(exc))
        return []


def _core_retrieval_check(run_dir: Path, state: dict, errors: list[str]) -> None:
    """A provisional/capped corpus cannot graduate through a separate gap search."""
    if state.get("coverage") != "complete_pagination":
        errors.append("Core retrieval coverage must be complete_pagination before selection or final review")
    if state.get("smoke_limit") is not None:
        errors.append("Core retrieval smoke_limit must be None; rerun the core search without a cap")
    logs = _read_collection(run_dir, "search_log.jsonl", errors)
    latest = {}
    for row in logs:
        if row.get("purpose") == "core" and row.get("type") == "summary" and _present(row.get("source")):
            latest[row["source"]] = row

    def issues(row: dict, expected: str | None = None) -> list[str]:
        found = []
        source = row.get("source", "unknown source")
        if row.get("complete") is not True or row.get("errors") != []:
            found.append(f"Core {source} summary is incomplete or failed")
        reported, retrieved = row.get("reported_count"), row.get("retrieved_count")
        if (not isinstance(reported, int) or isinstance(reported, bool) or reported < 0 or
                not isinstance(retrieved, int) or isinstance(retrieved, bool) or retrieved != reported):
            found.append(f"Core {source} counts do not demonstrate complete retrieval")
        if row.get("limit") is not None or row.get("limit_reached") is True:
            found.append(f"Core {source} summary is capped; rerun without a limit")
        if not _present(row.get("query")):
            found.append(f"Core {source} summary lacks the actual query")
        elif expected is not None and _normalize(expected) not in _normalize(row["query"]):
            found.append(f"Core {source} actual query does not include the configured query_plan query")
        return found

    plan = state.get("query_plan")
    if plan is not None and not isinstance(plan, dict):
        errors.append("query_plan must be a mapping of sources to exact core queries")
        return
    configured = {source: query for source, query in (plan or {}).items() if _present(query)}
    if plan and len(configured) != len(plan):
        errors.append("query_plan contains an empty or invalid source query")
    if configured:
        for source, expected in configured.items():
            if source not in latest:
                errors.append(f"Missing purpose=core summary for configured source {source}")
            else:
                errors.extend(issues(latest[source], expected))
    elif not latest:
        errors.append("At least one complete purpose=core summary is required in search_log.jsonl")
    elif not any(not issues(row) for row in latest.values()):
        errors.append("At least one complete uncapped purpose=core summary is required")
        for row in latest.values():
            errors.extend(issues(row))


def _cited_ids(text: str) -> list[str]:
    result = []
    for match in CITATION.finditer(text):
        for identity in PAPER_CITE.findall(match.group(1)):
            if identity not in result:
                result.append(identity)
    return result


def _literal_identifiers(text: str) -> tuple[set[str], set[str]]:
    dois = set()
    for value in re.findall(r"10\.\d{4,9}/[^\s<>\"{}]+", text, flags=re.I):
        value = value.rstrip(".,;:")
        while value.endswith(")") and value.count(")") > value.count("("):
            value = value[:-1]
        value = value.rstrip("]")
        dois.add(_doi(value))
    pmids = set(re.findall(r"\bPMID\s*:?\s*(\d+)\b", text, flags=re.I))
    pmids.update(re.findall(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", text, flags=re.I))
    return dois, pmids


def _validate_final(run_dir: Path, state: dict, papers: dict, evidence: dict,
                    gaps: dict, errors: list[str], warnings: list[str]) -> None:
    selected = state.get("selected_gap_id")
    if state.get("status") not in {"selected", "draft_ready", "complete"} or selected not in gaps:
        errors.append("A valid gap must be explicitly selected before final review validation")
    if not _present(state.get("user_selection")):
        errors.append("Missing recorded human user_selection")
    manuscript_path = run_dir / "manuscript.md"
    if not manuscript_path.is_file():
        errors.append("Missing manuscript.md")
        return
    manuscript = manuscript_path.read_text(encoding="utf-8")
    if not manuscript.strip():
        errors.append("manuscript.md is empty")
    cited = _cited_ids(manuscript)
    if not cited:
        errors.append("manuscript.md must use stable [@P...] source citations")
    literal_dois, literal_pmids = _literal_identifiers(manuscript)
    cited_dois = {_doi(papers[pid].get("doi")) for pid in cited if pid in papers and papers[pid].get("doi")}
    cited_pmids = {str(papers[pid].get("pmid")) for pid in cited if pid in papers and papers[pid].get("pmid")}
    for doi in sorted(literal_dois - cited_dois):
        errors.append(f"Manuscript DOI {doi} does not match a stable cited paper")
    for pmid in sorted(literal_pmids - cited_pmids):
        errors.append(f"Manuscript PMID {pmid} does not match a stable cited paper")
    parameters = state.get("parameters", {})
    bounds = parameters.get("word_range", [4000, 6000])
    if isinstance(bounds, list) and len(bounds) == 2 and all(isinstance(v, int) for v in bounds):
        words = len(re.findall(r"\b[A-Za-z]+(?:['’-][A-Za-z]+)*\b", CITATION.sub("", manuscript)))
        if not bounds[0] <= words <= bounds[1]:
            errors.append(f"Manuscript has {words} English words; expected {bounds[0]}–{bounds[1]}")
    claims = _read_collection(run_dir, "claims.jsonl", errors)
    _index(claims, "claim_id", errors)
    if not claims:
        errors.append("Missing nonempty claims.jsonl")
    claim_papers = set()
    manuscript_claim_text = _normalize(CITATION.sub("", manuscript)).casefold()
    for claim in claims:
        identity = claim.get("claim_id", "unknown claim")
        if not _present(claim.get("text")):
            errors.append(f"{identity}: missing claim text")
        elif _normalize(claim["text"]).casefold() not in manuscript_claim_text:
            errors.append(f"{identity}: claim text is absent from manuscript.md")
        required = claim.get("required_depth", "abstract")
        if required not in DEPTHS:
            errors.append(f"{identity}: invalid required_depth")
        eids, pids = claim.get("evidence_ids"), claim.get("paper_ids")
        if not isinstance(eids, list) or not eids or not isinstance(pids, list) or not pids:
            errors.append(f"{identity}: nonempty paper_ids and evidence_ids are required")
            continue
        linked = set()
        for eid in eids:
            row = evidence.get(eid) if isinstance(eid, str) else None
            if row is None:
                errors.append(f"{identity}: unknown evidence {eid}")
                continue
            pid = row.get("paper_id")
            linked.add(pid)
            if pid not in pids:
                errors.append(f"{identity}: evidence {eid} paper missing from paper_ids")
            if row.get("support") != "supported":
                errors.append(f"{identity}: evidence {eid} is not supported")
            if required == "full_text" and row.get("source_depth") != "full_text":
                errors.append(f"{identity}: evidence {eid} is abstract-only but claim requires full_text")
        for pid in pids:
            if not isinstance(pid, str) or pid not in papers:
                errors.append(f"{identity}: unknown paper {pid}")
            elif pid not in linked:
                errors.append(f"{identity}: paper {pid} lacks linked evidence")
            else:
                claim_papers.add(pid)
    audit_path = run_dir / "reference_audit.json"
    audit_rows = []
    if not audit_path.is_file():
        errors.append("Missing reference_audit.json")
    else:
        try:
            audit = _json(audit_path)
            audit_rows = audit.get("records") if isinstance(audit, dict) else None
            if not isinstance(audit_rows, list) or any(not isinstance(row, dict) for row in audit_rows):
                raise ValueError("reference_audit.json requires records list")
        except ValueError as exc:
            errors.append(str(exc))
            audit_rows = []
    audits = _index(audit_rows, "paper_id", errors)
    for pid in cited:
        paper = papers.get(pid)
        if paper is None:
            errors.append(f"Manuscript cites unknown paper {pid}")
            continue
        if pid not in claim_papers:
            errors.append(f"Cited paper {pid} lacks a supported claim/evidence link")
        record = audits.get(pid, {})
        if record.get("status") != "verified":
            errors.append(f"Cited paper {pid} does not have a verified reference audit")
        publication_status = record.get("publication_status")
        if publication_status in {"retracted", "expression_of_concern"}:
            errors.append(f"Cited paper {pid} cannot support final claims: publication_status={publication_status}")
        if publication_status == "corrected" and record.get("notice_reviewed") is not True:
            errors.append(f"Cited paper {pid} has a correction requiring notice_reviewed=true")
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        for field in ("doi", "pmid", "title"):
            value = record.get(field) or metadata.get(field)
            expected = paper.get(field)
            if value and expected:
                normalize = _doi if field == "doi" else _title if field == "title" else lambda v: str(v).strip()
                if normalize(value) != normalize(expected):
                    errors.append(f"Cited paper {pid} has an audit {field} mismatch")
        if not paper.get("doi") and not paper.get("pmid"):
            warnings.append(f"{pid} has neither DOI nor PMID; audit must identify its authoritative source")
        if not parameters.get("allow_background_refs", False):
            try:
                start, end = date.fromisoformat(parameters["start"]), date.fromisoformat(parameters["end"])
            except (ValueError, TypeError, KeyError):
                errors.append("Final review requires valid parameters.start and parameters.end dates")
                start = end = None
            try:
                publication_date = date.fromisoformat(str(paper.get("publication_date") or ""))
            except ValueError:
                review = paper.get("date_scope_review")
                if not (isinstance(review, dict) and review.get("eligible") is True and _present(review.get("rationale"))):
                    errors.append(f"Cited paper {pid} has an unknown or partial publication_date; explicit date_scope_review is required")
                else:
                    warnings.append(f"{pid}: date eligibility relies on recorded manual date_scope_review")
            else:
                if start and not start <= publication_date <= end:
                    errors.append(f"Cited paper {pid} is outside the review time window; allow_background_refs is false")
    uncited = sorted(claim_papers.difference(cited))
    if uncited:
        warnings.append("Claims contain uncited papers: " + ", ".join(uncited))


def validate_run(run_dir: str | Path, final: bool = False) -> dict:
    run_dir = Path(run_dir)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        state = load_run(run_dir)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": []}
    if state.get("schema_version") != 1:
        errors.append("Unsupported run schema_version")
    if state.get("status") not in STATUSES:
        errors.append("Invalid run status")
    if not isinstance(state.get("parameters"), dict) or not isinstance(state.get("gene"), dict):
        errors.append("Run parameters and gene must be objects")
        return {"ok": False, "errors": errors, "warnings": warnings}
    papers = _index(_read_collection(run_dir, "papers.jsonl", errors), "paper_id", errors)
    evidence = _index(_read_collection(run_dir, "evidence.jsonl", errors), "evidence_id", errors)
    gaps = _index(_read_collection(run_dir, "gaps.json", errors, jsonl=False), "gap_id", errors)
    searches = _index(_read_collection(run_dir, "gap_searches.jsonl", errors), "query_id", errors)
    if state.get("status") != "initialized" and not papers:
        errors.append("No paper corpus is available")
    for pid, paper in papers.items():
        if not _present(paper.get("title")):
            errors.append(f"{pid}: missing title")
        if paper.get("source_depth") not in DEPTHS | {"metadata"}:
            errors.append(f"{pid}: invalid source_depth")
        if paper.get("source_depth") == "full_text":
            path = _text_path(run_dir, paper)
            if path is None or not path.is_file():
                errors.append(f"{pid}: full_text source_depth requires a saved full_text_path")
    for row in evidence.values():
        _evidence_check(run_dir, row, papers, errors)
    gene = state.get("gene") if isinstance(state.get("gene"), dict) else {}
    parameters = state.get("parameters") if isinstance(state.get("parameters"), dict) else {}
    gene_terms = [term for term in (gene.get("symbol"), gene.get("official_symbol"), parameters.get("gene")) if _present(term)]
    for gap in gaps.values():
        _gap_check(gap, evidence, papers, searches, gene_terms, errors)
        if state.get("orchestration") and gap.get("confidence") == "low":
            errors.append(f"{gap.get('gap_id')}: low-confidence leads belong in the exploratory report, not selectable gaps.json")
    if state.get("status") in {"awaiting_selection", "selected", "draft_ready", "complete"} or final:
        _core_retrieval_check(run_dir, state, errors)
        if not evidence:
            errors.append("No evidence records are available")
        if not gaps:
            errors.append("No credible candidate gaps are available")
        for name in ("landscape.md", "gaps.md"):
            path = run_dir / name
            if not path.is_file() or not path.read_text(encoding="utf-8").strip():
                errors.append(f"Missing nonempty {name}")
    if final:
        _validate_final(run_dir, state, papers, evidence, gaps, errors, warnings)
    if final or state.get("status") in {"awaiting_selection", "selected", "draft_ready", "complete"}:
        errors.extend(phase_errors(run_dir, final=final))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def prepare_selection(run_dir: str | Path) -> dict:
    run_dir = Path(run_dir)
    state = load_run(run_dir)
    if state.get("status") not in {"initialized", "retrieved", "awaiting_selection"}:
        raise ValueError("Selection preparation is only allowed before a gap has been selected")
    report = validate_run(run_dir)
    errors = list(report["errors"])
    errors.extend(phase_errors(run_dir))
    _core_retrieval_check(run_dir, state, errors)
    for name in ("papers.jsonl", "evidence.jsonl", "gaps.json", "gap_searches.jsonl", "landscape.md", "gaps.md"):
        path = run_dir / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            errors.append(f"Missing nonempty {name}")
    if not _read_collection(run_dir, "gaps.json", errors, jsonl=False):
        errors.append("No credible candidate gaps are available")
    if errors:
        raise ValueError("Cannot prepare selection: " + "; ".join(dict.fromkeys(errors)))
    state.update(status="awaiting_selection", selected_gap_id=None, updated_at=_now())
    _save(run_dir / "run.json", state)
    return state


def select_gap(run_dir: str | Path, gap_id: str, user_selection: str) -> dict:
    run_dir = Path(run_dir)
    state = load_run(run_dir)
    if state.get("status") != "awaiting_selection":
        raise ValueError("select_gap requires status awaiting_selection")
    if not _present(user_selection):
        raise ValueError("Explicit nonempty human selection text is required")
    report = validate_run(run_dir)
    if not report["ok"]:
        raise ValueError("Cannot select gap: " + "; ".join(report["errors"]))
    gaps = _json(run_dir / "gaps.json")
    if gap_id not in {row.get("gap_id") for row in gaps}:
        raise ValueError(f"Unknown gap_id: {gap_id}")
    state.update(status="selected", selected_gap_id=gap_id, user_selection=user_selection.strip(),
                 selected_at=_now(), updated_at=_now())
    _save(run_dir / "run.json", state)
    return state


def _reference(paper: dict, number: int) -> str:
    authors = paper.get("authors") or []
    if isinstance(authors, list):
        authors = ", ".join(str(a.get("name") or " ".join(str(a.get(k) or "") for k in ("given", "family")).strip())
                            if isinstance(a, dict) else str(a) for a in authors)
    venue = str(paper.get("journal") or "")
    if paper.get("volume"):
        venue += " " + str(paper["volume"])
    if paper.get("issue"):
        venue += "(" + str(paper["issue"]) + ")"
    if paper.get("pages"):
        venue += ":" + str(paper["pages"])
    pieces = [str(authors).strip(), str(paper.get("title") or "").rstrip("."), venue.strip(), str(paper.get("year") or "")]
    text = ". ".join(piece for piece in pieces if piece) + "."
    if paper.get("is_preprint"):
        text += " [Preprint]."
    if paper.get("doi"):
        text += " https://doi.org/" + _doi(paper["doi"])
    if paper.get("pmid"):
        text += " PMID: " + str(paper["pmid"])
    return f"{number}. {text}"


def _bibtex(papers: list[dict]) -> str:
    def escaped(value: Any) -> str:
        return str(value).replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")
    entries = []
    for paper in papers:
        values = {"title": paper.get("title"), "year": paper.get("year"), "doi": _doi(paper.get("doi")),
                  "pmid": paper.get("pmid"), "journal": paper.get("journal"), "volume": paper.get("volume"),
                  "number": paper.get("issue"), "pages": paper.get("pages")}
        authors = paper.get("authors")
        if isinstance(authors, list):
            authors = " and ".join(str(a.get("name") or " ".join(str(a.get(k) or "") for k in ("given", "family")).strip())
                                    if isinstance(a, dict) else str(a) for a in authors)
        if authors:
            values["author"] = authors
        if paper.get("is_preprint"):
            values["note"] = "Preprint"
        fields = ",\n".join(f"  {key} = {{{escaped(value)}}}" for key, value in values.items() if value)
        entries.append(f"@article{{{paper['paper_id']},\n{fields}\n}}")
    return "\n\n".join(entries) + "\n"


def _render_docx(markdown: str, path: Path, module: Any) -> None:
    document = module.Document()
    normal = document.styles["Normal"]
    normal.font.name = "Times New Roman"
    try:
        normal.font.size = module.shared.Pt(11)
    except AttributeError:
        pass
    lines = markdown.splitlines()
    paragraph: list[str] = []
    def flush() -> None:
        if paragraph:
            document.add_paragraph(" ".join(paragraph))
            paragraph.clear()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            flush()
        elif line.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            flush()
            cells = lambda value: [cell.strip() for cell in value.strip().strip("|").split("|")]
            headers = cells(line)
            table = document.add_table(rows=1, cols=len(headers))
            table.style = "Table Grid"
            for cell, value in zip(table.rows[0].cells, headers):
                cell.text = value
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                row = table.add_row().cells
                for cell, value in zip(row, cells(lines[i])):
                    cell.text = value
                i += 1
            continue
        elif re.match(r"^#{1,6}\s", line):
            flush()
            heading = re.match(r"^(#{1,6})\s+(.*)", line)
            document.add_heading(heading.group(2), level=min(len(heading.group(1)), 4))
        elif re.match(r"^[-*+]\s+", line):
            flush()
            document.add_paragraph(re.sub(r"^[-*+]\s+", "", line), style="List Bullet")
        elif re.match(r"^\d+\.\s+", line):
            flush()
            # Keep literal numbering so Word and Markdown references match exactly.
            document.add_paragraph(line)
        else:
            paragraph.append(line)
        i += 1
    flush()
    document.save(str(path))


def export_review(run_dir: str | Path, docx: bool = True) -> dict[str, str]:
    run_dir = Path(run_dir)
    report = validate_run(run_dir, final=True)
    if not report["ok"]:
        raise ValueError("Review export refused: " + "; ".join(report["errors"]))
    docx_module = None
    if docx:
        try:
            docx_module = importlib.import_module("docx")
        except ImportError as exc:
            raise ValueError("Word export requires python-docx; use the bundled document runtime or install python-docx, or request Markdown-only export") from exc
    manuscript = (run_dir / "manuscript.md").read_text(encoding="utf-8")
    papers = {paper["paper_id"]: paper for paper in load_jsonl(run_dir / "papers.jsonl")}
    cited = _cited_ids(manuscript)
    numbers = {pid: i + 1 for i, pid in enumerate(cited)}
    def replace_citation(match: re.Match) -> str:
        return "[" + ", ".join(str(numbers[pid]) for pid in PAPER_CITE.findall(match.group(1))) + "]"
    body = CITATION.sub(replace_citation, manuscript).rstrip()
    references = "\n".join(_reference(papers[pid], numbers[pid]) for pid in cited)
    markdown = body + "\n\n## References\n\n" + references + "\n"
    outputs = {"markdown": str(run_dir / "review.md"), "evidence_csv": str(run_dir / "evidence.csv"),
               "references_bib": str(run_dir / "references.bib")}
    if docx_module:
        temporary_docx = run_dir / "review.tmp.docx"
        _render_docx(markdown, temporary_docx, docx_module)
        temporary_docx.replace(run_dir / "review.docx")
        outputs["docx"] = str(run_dir / "review.docx")
    (run_dir / "review.md").write_text(markdown, encoding="utf-8")
    (run_dir / "references.bib").write_text(_bibtex([papers[pid] for pid in cited]), encoding="utf-8")
    rows = load_jsonl(run_dir / "evidence.jsonl")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (run_dir / "evidence.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                             for key, value in row.items()})
    state = load_run(run_dir)
    state.update(status="complete", outputs=outputs, updated_at=_now())
    _save(run_dir / "run.json", state)
    return outputs
