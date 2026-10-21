#!/usr/bin/env python3
"""Command-line plumbing for the tuzaizi-review Codex skill.

The agent reads evidence and writes structured analyses; these commands perform
reproducible retrieval, checkpoint validation and artifact export.
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
from zoneinfo import ZoneInfo

from retrieval import (Client, RetrievalError, resolve_gene, build_queries,
                       search_pubmed, search_europepmc, merge_records,
                       enrich_biorxiv, fetch_full_text)
from verification import verify_reference
from workflow import (create_run, load_run, update_run, load_jsonl, write_jsonl,
                      prepare_selection, select_gap, validate_run, export_review)
from orchestration import (enable as enable_skills, phase_plan, begin_phase,
                           finish_phase, completed_phase_errors)


def today():
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()


def date_window(*, last_years=None, years=None, start=None, end=None, as_of=None):
    """Explicit ranges win only through the parser's mutually exclusive inputs."""
    actual_today = today()
    cutoff = dt.date.fromisoformat(as_of) if as_of else actual_today
    if cutoff > actual_today:
        cutoff = actual_today
    if years:
        match = re.fullmatch(r"(\d{4})\s*[:–-]\s*(\d{4})", years)
        if not match:
            raise ValueError("--years must be YYYY:YYYY")
        low, high = map(int, match.groups())
        lower, upper = dt.date(low, 1, 1), min(dt.date(high, 12, 31), cutoff)
        mode = "explicit_years"
    elif start or end:
        if not start or not end:
            raise ValueError("Supply both --start and --end")
        lower, upper = dt.date.fromisoformat(start), min(dt.date.fromisoformat(end), cutoff)
        mode = "explicit_dates"
    else:
        n = 5 if last_years is None else last_years
        if n <= 0:
            raise ValueError("--last-years must be positive")
        target = cutoff.year - n
        lower = cutoff.replace(year=target, day=min(cutoff.day, calendar.monthrange(target, cutoff.month)[1]))
        upper, mode = cutoff, "rolling_years"
    if lower > upper:
        raise ValueError("Date range is empty or entirely in the future")
    return {"start": lower.isoformat(), "end": upper.isoformat(), "as_of": cutoff.isoformat(), "date_mode": mode}


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _client(run_dir, refresh=False):
    return Client(Path(run_dir) / ".cache", refresh=refresh)


def _error(exc):
    return exc.as_dict() if isinstance(exc, RetrievalError) else {"code": type(exc).__name__, "message": str(exc)}


def _logs(rows, *, purpose, gap_id=None):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for row in rows:
        row = dict(row)
        row.setdefault("query_id", "Q" + uuid.uuid4().hex[:16])
        row.setdefault("retrieved_at", now)
        row["purpose"] = purpose
        if gap_id:
            row["gap_id"] = gap_id
        yield row


def _do_search(run_dir, queries, start, end, *, limit=None, refresh=False, purpose="core", gap_id=None):
    client = _client(run_dir, refresh)
    records, logs = [], []
    for source, adapter in (("pubmed", search_pubmed), ("europepmc", search_europepmc)):
        if not queries.get(source):
            continue
        try:
            found, detail = adapter(queries[source], client, start, end, limit=limit)
            records.extend(found)
            logs.extend(detail)
        except Exception as exc:
            logs.append({"type": "summary", "source": source, "query": queries[source],
                         "reported_count": None, "retrieved_count": 0, "complete": False,
                         "errors": [_error(exc)]})
    for r in records:
        r["corpus_role"] = purpose
    existing = load_jsonl(Path(run_dir) / "papers.jsonl")
    merged = merge_records(existing, records)
    logrows = list(_logs(logs, purpose=purpose, gap_id=gap_id))
    logname = "search_log.jsonl" if purpose == "core" else "gap_searches.jsonl"
    oldlogs = load_jsonl(Path(run_dir) / logname)
    # Persist records first; a crash before the log commit is recoverable by replay.
    write_jsonl(Path(run_dir) / "papers.jsonl", merged)
    write_jsonl(Path(run_dir) / logname, oldlogs + logrows)
    summaries = [r for r in logrows if r.get("type") == "summary"]
    complete = bool(summaries) and all(r.get("complete") and not r.get("errors") for r in summaries)
    return {"records_in_run": len(merged), "new_records": len(merged) - len(existing),
            "coverage": "complete_pagination" if complete else "partial_or_failed",
            "searches": summaries or logrows}


def init_command(args):
    run_dir = Path(args.run_dir).expanduser().resolve()
    if (run_dir / "run.json").exists():
        raise ValueError("Run already exists; use status/search to resume or choose a new run directory")
    if args.word_min < 1 or args.word_max < args.word_min:
        raise ValueError("Invalid word range")
    params = date_window(last_years=args.last_years, years=args.years, start=args.start, end=args.end, as_of=args.as_of)
    concepts = [[v.strip() for v in concept.split("|") if v.strip()] for concept in args.concept]
    if any(not group for group in concepts):
        raise ValueError("Each --concept needs at least one nonempty synonym")
    gene = resolve_gene(args.gene, _client(run_dir, args.refresh), taxon=args.taxon)
    params.update({"gene": gene["symbol"], "requested_gene": args.gene, "topic": args.topic,
                   "concepts": concepts, "language": "en", "report_language": "zh-CN",
                   "review_type": "mechanistic_narrative", "word_range": [args.word_min, args.word_max],
                   "allow_background_refs": bool(args.allow_background_refs)})
    state = create_run(run_dir, params, gene)
    queries = build_queries(gene, concepts, params["start"], params["end"])
    # Keep a new run guarded even if bundle validation fails partway through init.
    state = update_run(run_dir, query_plan=queries, orchestration={"version": 1})
    state = enable_skills(run_dir)
    return {"run_dir": str(run_dir), "status": state["status"], "gene": gene, "parameters": params, "queries": queries}


def set_queries_command(args):
    state = load_run(args.run_dir)
    if state["status"] != "initialized":
        raise ValueError("Set the upstream search strategy before retrieval; changed scope needs a new run")
    queries = {"pubmed": args.pubmed_query.strip(), "europepmc": args.europepmc_query.strip()}
    if any(not query for query in queries.values()):
        raise ValueError("Both exact database queries must be nonempty")
    return update_run(args.run_dir, query_plan=queries)


def skills_command(args):
    if args.action == "enable":
        return enable_skills(args.run_dir)
    if args.action == "finish":
        if not args.invocation or not args.report:
            raise ValueError("skills finish requires --invocation and --report")
        return finish_phase(args.run_dir, args.invocation, args.report)
    if not args.phase:
        raise ValueError("skills plan/begin requires --phase")
    return (phase_plan if args.action == "plan" else begin_phase)(args.run_dir, args.phase)


def search_command(args):
    state = load_run(args.run_dir)
    if state["status"] not in ("initialized", "retrieved"):
        raise ValueError("Initial search cannot replace an analysis/selection. Use a new run for changed scope, or gap-search for a supplement.")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be positive; omitted means all paginated matches")
    prerequisites = completed_phase_errors(args.run_dir, "search")
    if prerequisites:
        raise ValueError("Complete the upstream search strategy before retrieval: " + "; ".join(prerequisites))
    p = state["parameters"]
    result = _do_search(args.run_dir, state["query_plan"], p["start"], p["end"], limit=args.limit, refresh=args.refresh)
    if args.enrich_preprints:
        rows = load_jsonl(Path(args.run_dir) / "papers.jsonl")
        updated, logs = enrich_biorxiv(rows, _client(args.run_dir, args.refresh))
        write_jsonl(Path(args.run_dir) / "papers.jsonl", updated)
        logpath = Path(args.run_dir) / "search_log.jsonl"
        write_jsonl(logpath, load_jsonl(logpath) + list(_logs(logs, purpose="preprint_enrichment")))
    update_run(args.run_dir, status="retrieved", coverage=result["coverage"],
               smoke_limit=args.limit, record_count=result["records_in_run"])
    return result


def gap_search_command(args):
    state = load_run(args.run_dir)
    if state["status"] == "initialized":
        raise ValueError("Retrieve the core corpus before gap checks")
    if not args.pubmed_query and not args.europepmc_query:
        raise ValueError("Provide a targeted --pubmed-query and/or --europepmc-query")
    p = state["parameters"]
    window = date_window(start=args.start, end=args.end or p["as_of"], as_of=p["as_of"])
    result = _do_search(args.run_dir, {"pubmed": args.pubmed_query, "europepmc": args.europepmc_query},
                        window["start"], window["end"], refresh=args.refresh,
                        purpose="gap_verification", gap_id=args.gap_id)
    # Supplementary searches do not unlock the topic-selection gate.
    update_run(args.run_dir, last_gap_search=args.gap_id)
    return result


def fulltext_command(args):
    load_run(args.run_dir)
    rows = load_jsonl(Path(args.run_dir) / "papers.jsonl")
    wanted = set(args.paper_id or [])
    known = {r["paper_id"] for r in rows}
    if wanted - known:
        raise ValueError("Unknown paper IDs: " + ", ".join(sorted(wanted - known)))
    if not args.all and not wanted:
        raise ValueError("Select --paper-id (repeatable) or --all")
    client = _client(args.run_dir, args.refresh)
    results = []
    for i, row in enumerate(rows):
        if args.all or row["paper_id"] in wanted:
            try:
                rows[i] = fetch_full_text(row, client, Path(args.run_dir).resolve() / "fulltext")
                last = next((p for p in reversed(rows[i].get("provenance", []))
                             if p.get("source") == "europepmc_full_text"), {})
                depth = rows[i]["source_depth"]
                outcome = "retrieved" if last.get("body_present") else "retained_full_text" if depth == "full_text" else "unavailable"
                entry = {"paper_id": row["paper_id"], "source_depth": depth,
                         "full_text_path": rows[i].get("full_text_path"), "outcome": outcome}
                if last.get("error"):
                    entry["error"] = last["error"]
                if last.get("reason"):
                    entry["reason"] = last["reason"]
                results.append(entry)
            except Exception as exc:
                results.append({"paper_id": row["paper_id"], "source_depth": row.get("source_depth"), "error": _error(exc)})
    write_jsonl(Path(args.run_dir) / "papers.jsonl", rows)
    path = Path(args.run_dir) / "fulltext_log.jsonl"
    write_jsonl(path, load_jsonl(path) + results)
    return {"results": results}


def verify_command(args):
    load_run(args.run_dir)
    rows = load_jsonl(Path(args.run_dir) / "papers.jsonl")
    wanted = set(args.paper_id or [])
    if not wanted and not getattr(args, "all", False):
        # Verify references used by analyses/manuscript, not every exploratory hit.
        for name in ("evidence.jsonl", "claims.jsonl"):
            for r in load_jsonl(Path(args.run_dir) / name):
                if r.get("paper_id"):
                    wanted.add(r["paper_id"])
                wanted.update(r.get("paper_ids", []))
        gaps_path = Path(args.run_dir) / "gaps.json"
        if gaps_path.exists():
            for gap in json.loads(gaps_path.read_text(encoding="utf-8")):
                wanted.update(gap.get("nearest_paper_ids", []))
        manuscript_path = Path(args.run_dir) / "manuscript.md"
        if manuscript_path.exists():
            wanted.update(re.findall(r"@([A-Za-z][A-Za-z0-9_-]*)", manuscript_path.read_text(encoding="utf-8")))
        if not wanted:
            raise ValueError("No analysis references found. Create evidence/claims, provide --paper-id, or explicitly use --all.")
    if wanted - {p["paper_id"] for p in rows}:
        raise ValueError("Unknown paper ID in --paper-id")
    # Notice checks must be current at manuscript verification, including on resume.
    client = _client(args.run_dir, refresh=not args.cached)
    previous_path = Path(args.run_dir) / "reference_audit.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {"records": []}
    audits = {r["paper_id"]: r for r in previous.get("records", [])}
    for row in rows:
        if not wanted or row["paper_id"] in wanted:
            checked = verify_reference(row, client)
            old = audits.get(row["paper_id"], {})
            # Retain a genuine correction review only while its exact notice set
            # and authoritative paper identity remain the same.
            def notice_key(r):
                return sorted(json.dumps(n, sort_keys=True, ensure_ascii=False) for n in r.get("notices", []))
            if (old.get("notice_reviewed") is True and old.get("status") == "verified"
                    and checked.get("status") == "verified"
                    and checked.get("notice_status") != "unknown"
                    and notice_key(old) == notice_key(checked)
                    and old.get("metadata", {}).get("doi") == checked.get("metadata", {}).get("doi")
                    and old.get("metadata", {}).get("pmid") == checked.get("metadata", {}).get("pmid")):
                checked["notice_reviewed"] = True
                if old.get("notice_review_note"):
                    checked["notice_review_note"] = old["notice_review_note"]
            audits[row["paper_id"]] = checked
    result = {"schema_version": 1, "records": list(audits.values())}
    _atomic_json(previous_path, result)
    counts = {s: sum(r["status"] == s for r in audits.values()) for s in ("verified", "mismatch", "unverified")}
    return {"audit_path": str(previous_path.resolve()), "counts": counts}


def export_command(args):
    # Try the current runtime. The orchestrator can rerun with the desktop bundled
    # interpreter, which provides python-docx; never silently omit requested Word.
    result = export_review(args.run_dir, docx=True)
    update_run(args.run_dir, status="complete", outputs=result)
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Resolve a gene, normalize dates and save the run")
    init.add_argument("--run-dir", required=True)
    init.add_argument("--gene", required=True)
    init.add_argument("--taxon", default="9606")
    init.add_argument("--topic", required=True)
    init.add_argument("--concept", action="append", required=True, help="Repeat AND groups; synonyms inside a group separated by |")
    group = init.add_mutually_exclusive_group()
    group.add_argument("--years")
    group.add_argument("--last-years", type=int)
    group.add_argument("--start")
    init.add_argument("--end")
    init.add_argument("--as-of", help="Reproducible cutoff, capped at today's Asia/Shanghai date")
    init.add_argument("--word-min", type=int, default=4000)
    init.add_argument("--word-max", type=int, default=6000)
    init.add_argument("--allow-background-refs", action="store_true", help="Only when user authorizes older review background references")
    init.add_argument("--refresh", action="store_true")
    init.set_defaults(handler=init_command)
    queries = sub.add_parser("set-queries", help="Use exact queries produced by the upstream search skill")
    queries.add_argument("--run-dir", required=True)
    queries.add_argument("--pubmed-query", required=True)
    queries.add_argument("--europepmc-query", required=True)
    queries.set_defaults(handler=set_queries_command)
    skills = sub.add_parser("skills", help="Dispatch pinned upstream skills and record actual agent work")
    skills.add_argument("action", choices=("enable", "plan", "begin", "finish"))
    skills.add_argument("--run-dir", required=True)
    skills.add_argument("--phase", choices=("search", "landscape", "gaps", "review", "claims"))
    skills.add_argument("--invocation")
    skills.add_argument("--report", help="Agent-written completion report JSON; loading alone is not completion")
    skills.set_defaults(handler=skills_command)
    for name in ("search", "gap-search", "fulltext", "status", "checkpoint", "select", "verify", "validate", "export"):
        c = sub.add_parser(name)
        c.add_argument("--run-dir", required=True)
        if name in ("search", "gap-search", "fulltext"):
            c.add_argument("--refresh", action="store_true")
        if name == "search":
            c.add_argument("--limit", type=int, help="Smoke-test cap, explicitly recorded as incomplete when truncating")
            c.add_argument("--enrich-preprints", action="store_true")
            c.set_defaults(handler=search_command)
        elif name == "gap-search":
            c.add_argument("--gap-id", required=True)
            c.add_argument("--pubmed-query")
            c.add_argument("--europepmc-query")
            c.add_argument("--start", default="1900-01-01")
            c.add_argument("--end")
            c.set_defaults(handler=gap_search_command)
        elif name == "fulltext":
            c.add_argument("--paper-id", action="append")
            c.add_argument("--all", action="store_true")
            c.set_defaults(handler=fulltext_command)
        elif name == "status":
            c.set_defaults(handler=lambda a: load_run(a.run_dir))
        elif name == "checkpoint":
            c.set_defaults(handler=lambda a: prepare_selection(a.run_dir))
        elif name == "select":
            c.add_argument("--gap", required=True)
            c.add_argument("--user-selection", required=True, help="Verbatim human selection; not permission to invent a choice")
            c.set_defaults(handler=lambda a: select_gap(a.run_dir, a.gap, a.user_selection))
        elif name == "verify":
            c.add_argument("--paper-id", action="append")
            c.add_argument("--all", action="store_true", help="Audit every retrieved hit; default audits references used by analyses/manuscript")
            c.add_argument("--cached", action="store_true", help="Use run cache for offline tests; fresh metadata is default")
            c.set_defaults(handler=verify_command)
        elif name == "validate":
            c.add_argument("--final", action="store_true")
            c.set_defaults(handler=lambda a: validate_run(a.run_dir, final=a.final))
        elif name == "export":
            c.set_defaults(handler=export_command)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init" and args.end and not args.start:
            raise ValueError("--end requires --start")
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get("ok") is False else 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": _error(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
