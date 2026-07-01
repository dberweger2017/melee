#!/usr/bin/env python3
"""Batch-probe one decomp mutator and write an advisory Markdown report.

This is intentionally report-only. It applies at most one candidate per
function in a round, builds all touched objects together, scores the modified
functions, writes patches/results, and restores the original sources.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from tqdm.auto import tqdm
except Exception:  # pragma: no cover - optional dependency
    tqdm = None

try:
    from tools.decomp_candidates import (
        DEFAULT_OBJDIFF,
        DEFAULT_OBJDIFF_CLI,
        DEFAULT_REPORT,
        Candidate,
        DiffScore,
        MUTATOR_LANE_HELP_TEXT,
        UnitInfo,
        find_function_span,
        find_unit_for_source,
        generate_candidates,
        load_json,
        normalize_rel_path,
        parse_mutator_filter,
        path_relative_to_project,
        project_path,
        replace_function_body,
        run_objdiff,
        run_ninja,
        score_diff,
        unified_patch,
    )
except ModuleNotFoundError:  # Allows `python tools/decomp_batch_probe.py`.
    from decomp_candidates import (  # type: ignore[no-redef]
        DEFAULT_OBJDIFF,
        DEFAULT_OBJDIFF_CLI,
        DEFAULT_REPORT,
        Candidate,
        DiffScore,
        MUTATOR_LANE_HELP_TEXT,
        UnitInfo,
        find_function_span,
        find_unit_for_source,
        generate_candidates,
        load_json,
        normalize_rel_path,
        parse_mutator_filter,
        path_relative_to_project,
        project_path,
        replace_function_body,
        run_objdiff,
        run_ninja,
        score_diff,
        unified_patch,
    )


@dataclass(frozen=True)
class TargetFunction:
    source_path: str
    unit_name: str
    function: str
    match_percent: float | None
    size: int | None
    virtual_address: str


@dataclass
class ProbeCandidate:
    target: TargetFunction
    unit: UnitInfo
    candidate: Candidate
    source_path: Path
    source_rel: str


@dataclass
class ProbeResult:
    round_no: int
    source_path: str
    function: str
    unit_name: str
    candidate_id: int | None
    mutator: str
    description: str
    status: str
    baseline_match_percent: float | None
    candidate_match_percent: float | None = None
    delta: float | None = None
    instruction_diff_score: float | None = None
    patch_path: str = ""
    error: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_pct(value: float | None) -> str:
    return "" if value is None else f"{value:.5f}"


def fmt_delta(value: float | None) -> str:
    return "" if value is None else f"{value:+.5f}"


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_address(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"0x{int(value):08X}"
    except (TypeError, ValueError):
        return str(value)


def iter_unmatched_targets(report: dict[str, Any]) -> Iterable[TargetFunction]:
    for unit in report.get("units", []):
        source_path = normalize_rel_path(unit.get("metadata", {}).get("source_path", ""))
        unit_name = unit.get("name", "")
        if not source_path:
            continue
        for function in unit.get("functions") or []:
            fuzzy = function.get("fuzzy_match_percent")
            match_percent = float(fuzzy) if fuzzy is not None else None
            if match_percent is not None and match_percent >= 100.0:
                continue
            yield TargetFunction(
                source_path=source_path,
                unit_name=unit_name,
                function=function.get("name", ""),
                match_percent=match_percent,
                size=parse_int(function.get("size")),
                virtual_address=format_address(
                    function.get("metadata", {}).get("virtual_address", "")
                ),
            )


def split_csv(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    values = {part.strip() for part in raw.split(",") if part.strip()}
    return values or None


def select_targets(
    targets: Iterable[TargetFunction],
    sources: set[str] | None,
    functions: set[str] | None,
    min_match: float | None,
    sort_mode: str,
    limit: int | None,
    start_index: int,
) -> list[TargetFunction]:
    selected: list[TargetFunction] = []
    normalized_sources = {normalize_rel_path(source) for source in sources or set()}
    for target in targets:
        if normalized_sources and normalize_rel_path(target.source_path) not in normalized_sources:
            continue
        if functions and target.function not in functions:
            continue
        if min_match is not None:
            if target.match_percent is None or target.match_percent < min_match:
                continue
        selected.append(target)

    if sort_mode == "best-first":
        selected.sort(
            key=lambda target: (
                target.match_percent is None,
                -(target.match_percent if target.match_percent is not None else -1.0),
                target.source_path,
                target.function,
            )
        )
    elif sort_mode == "worst-first":
        selected.sort(
            key=lambda target: (
                target.match_percent is None,
                target.match_percent if target.match_percent is not None else 101.0,
                target.source_path,
                target.function,
            )
        )

    if start_index < 0:
        raise ValueError("--start-index must be non-negative")
    if start_index:
        selected = selected[start_index:]
    if limit is not None:
        selected = selected[:limit]
    return selected


def safe_run_name(source_rel: str, function: str) -> str:
    raw = f"{source_rel}__{function}"
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in raw)
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe[-180:]


def run_logged_ninja(project: Path, targets: list[str]) -> subprocess.CompletedProcess[str]:
    unique_targets = sorted(set(targets))
    if not unique_targets:
        return subprocess.CompletedProcess(["ninja"], 0, "", "")
    return subprocess.run(
        ["ninja", *unique_targets],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def collect_candidates(
    project: Path,
    args: argparse.Namespace,
    targets: list[TargetFunction],
) -> tuple[dict[tuple[str, str], list[ProbeCandidate]], dict[str, str]]:
    unit_cache: dict[str, UnitInfo] = {}
    source_cache: dict[str, str] = {}
    by_target: dict[tuple[str, str], list[ProbeCandidate]] = {}
    errors: dict[str, str] = {}

    iterator: Iterable[TargetFunction] = targets
    if tqdm is not None and not args.no_progress_bar:
        iterator = tqdm(targets, unit="fn", desc="generate")

    for target in iterator:
        source_path = project_path(project, target.source_path).resolve()
        source_rel = path_relative_to_project(project, source_path)
        try:
            unit = unit_cache.get(source_rel)
            if unit is None:
                unit = find_unit_for_source(args.objdiff, source_rel)
                unit_cache[source_rel] = unit
            source_text = source_cache.get(source_rel)
            if source_text is None:
                source_text = source_path.read_text(encoding="utf-8")
                source_cache[source_rel] = source_text
            candidates = generate_candidates(
                source_text,
                source_rel,
                [target.function],
                args.max_candidates_per_function,
                args.mutator_tier,
                args.mutator_filter,
            )
            by_target[(source_rel, target.function)] = [
                ProbeCandidate(target, unit, candidate, source_path, source_rel)
                for candidate in candidates
            ]
        except Exception as exc:
            errors[f"{target.source_path}:{target.function}"] = str(exc)

    return by_target, errors


def compose_round_sources(
    original_sources: dict[str, str],
    round_candidates: list[ProbeCandidate],
) -> tuple[dict[str, str], list[ProbeResult]]:
    by_source: dict[str, list[ProbeCandidate]] = {}
    for probe in round_candidates:
        by_source.setdefault(probe.source_rel, []).append(probe)

    composed: dict[str, str] = {}
    skipped: list[ProbeResult] = []
    for source_rel, probes in by_source.items():
        text = original_sources[source_rel]
        # Edit functions from bottom to top so earlier spans do not move.
        span_items = []
        for probe in probes:
            try:
                original_span = find_function_span(original_sources[source_rel], probe.target.function)
                candidate_span = find_function_span(probe.candidate.source_text, probe.target.function)
                candidate_body = probe.candidate.source_text[
                    candidate_span.body_open + 1 : candidate_span.body_close
                ]
                span_items.append((original_span.body_open, original_span, candidate_body, probe))
            except Exception as exc:
                skipped.append(
                    ProbeResult(
                        round_no=0,
                        source_path=source_rel,
                        function=probe.target.function,
                        unit_name=probe.unit.name,
                        candidate_id=probe.candidate.candidate_id,
                        mutator=probe.candidate.mutator,
                        description=probe.candidate.description,
                        status="compose_failed",
                        baseline_match_percent=probe.target.match_percent,
                        error=str(exc),
                    )
                )
        for _, span, candidate_body, _probe in sorted(span_items, reverse=True):
            text = replace_function_body(text, span, candidate_body)
        composed[source_rel] = text
    return composed, skipped


def baseline_scores(
    project: Path,
    objdiff_cli: Path,
    probes: list[ProbeCandidate],
) -> dict[tuple[str, str], DiffScore]:
    scores: dict[tuple[str, str], DiffScore] = {}
    for probe in probes:
        key = (probe.source_rel, probe.target.function)
        if key in scores:
            continue
        scores[key] = score_diff(
            run_objdiff(project, objdiff_cli, probe.unit.name, probe.target.function),
            probe.target.function,
        )
    return scores


def run_round(
    project: Path,
    args: argparse.Namespace,
    round_no: int,
    by_target: dict[tuple[str, str], list[ProbeCandidate]],
    original_sources: dict[str, str],
    output_dir: Path,
) -> list[ProbeResult]:
    round_dir = output_dir / f"round-{round_no:03d}"
    patch_dir = round_dir / "patches"
    diff_dir = round_dir / "diffs"
    patch_dir.mkdir(parents=True, exist_ok=True)
    diff_dir.mkdir(parents=True, exist_ok=True)

    round_candidates = [
        candidates[round_no - 1]
        for candidates in by_target.values()
        if len(candidates) >= round_no
    ]
    if not round_candidates:
        return []

    composed_sources, skipped = compose_round_sources(original_sources, round_candidates)
    for skipped_result in skipped:
        skipped_result.round_no = round_no

    touched_units = [probe.unit.base_path for probe in round_candidates]
    source_paths = {
        source_rel: project_path(project, source_rel).resolve()
        for source_rel in composed_sources
    }

    results: list[ProbeResult] = list(skipped)
    try:
        # Make sure the baseline objects are current before scoring.
        baseline_build = run_logged_ninja(project, touched_units)
        (round_dir / "baseline-build.stdout.log").write_text(
            baseline_build.stdout, encoding="utf-8"
        )
        (round_dir / "baseline-build.stderr.log").write_text(
            baseline_build.stderr, encoding="utf-8"
        )
        if baseline_build.returncode != 0:
            error = baseline_build.stderr.strip() or baseline_build.stdout.strip()
            for probe in round_candidates:
                results.append(
                    ProbeResult(
                        round_no=round_no,
                        source_path=probe.source_rel,
                        function=probe.target.function,
                        unit_name=probe.unit.name,
                        candidate_id=probe.candidate.candidate_id,
                        mutator=probe.candidate.mutator,
                        description=probe.candidate.description,
                        status="baseline_build_failed",
                        baseline_match_percent=probe.target.match_percent,
                        error=error,
                    )
                )
            return results

        baseline = baseline_scores(project, args.objdiff_cli, round_candidates)

        for source_rel, new_text in composed_sources.items():
            source_paths[source_rel].write_text(new_text, encoding="utf-8")

        build = run_logged_ninja(project, touched_units)
        (round_dir / "build.stdout.log").write_text(build.stdout, encoding="utf-8")
        (round_dir / "build.stderr.log").write_text(build.stderr, encoding="utf-8")
        if build.returncode != 0:
            error = build.stderr.strip() or build.stdout.strip()
            for probe in round_candidates:
                results.append(
                    ProbeResult(
                        round_no=round_no,
                        source_path=probe.source_rel,
                        function=probe.target.function,
                        unit_name=probe.unit.name,
                        candidate_id=probe.candidate.candidate_id,
                        mutator=probe.candidate.mutator,
                        description=probe.candidate.description,
                        status="batch_compile_failed",
                        baseline_match_percent=baseline.get(
                            (probe.source_rel, probe.target.function),
                            DiffScore(None, None, 0, 0),
                        ).match_percent,
                        error=error,
                    )
                )
            return results

        for probe in round_candidates:
            key = (probe.source_rel, probe.target.function)
            base_score = baseline.get(key)
            patch_name = (
                f"{safe_run_name(probe.source_rel, probe.target.function)}"
                f"__round-{round_no:03d}__cand-{probe.candidate.candidate_id:06d}.patch"
            )
            patch_path = patch_dir / patch_name
            patch_text = unified_patch(
                probe.source_rel,
                original_sources[probe.source_rel],
                composed_sources[probe.source_rel],
            )
            # Also write the single-function candidate patch for manual review.
            patch_path.write_text(probe.candidate.patch_text, encoding="utf-8")
            combined_patch_path = patch_dir / patch_name.replace(".patch", ".combined.patch")
            combined_patch_path.write_text(patch_text, encoding="utf-8")
            try:
                score = score_diff(
                    run_objdiff(
                        project,
                        args.objdiff_cli,
                        probe.unit.name,
                        probe.target.function,
                    ),
                    probe.target.function,
                )
                diff_path = diff_dir / patch_name.replace(".patch", ".diff.json")
                write_json(diff_path, {"score": asdict(score)})
                baseline_match = base_score.match_percent if base_score else None
                delta = (
                    score.match_percent - baseline_match
                    if score.match_percent is not None and baseline_match is not None
                    else None
                )
                status = "improved" if delta is not None and delta > args.min_delta else "not_improved"
                results.append(
                    ProbeResult(
                        round_no=round_no,
                        source_path=probe.source_rel,
                        function=probe.target.function,
                        unit_name=probe.unit.name,
                        candidate_id=probe.candidate.candidate_id,
                        mutator=probe.candidate.mutator,
                        description=probe.candidate.description,
                        status=status,
                        baseline_match_percent=baseline_match,
                        candidate_match_percent=score.match_percent,
                        delta=delta,
                        instruction_diff_score=score.instruction_diff_score,
                        patch_path=str(patch_path.relative_to(output_dir)),
                    )
                )
            except subprocess.CalledProcessError as exc:
                results.append(
                    ProbeResult(
                        round_no=round_no,
                        source_path=probe.source_rel,
                        function=probe.target.function,
                        unit_name=probe.unit.name,
                        candidate_id=probe.candidate.candidate_id,
                        mutator=probe.candidate.mutator,
                        description=probe.candidate.description,
                        status="diff_failed",
                        baseline_match_percent=base_score.match_percent if base_score else None,
                        patch_path=str(patch_path.relative_to(output_dir)),
                        error=(exc.stderr or exc.stdout or str(exc)).strip(),
                    )
                )
    finally:
        for source_rel, original_text in original_sources.items():
            source_path = project_path(project, source_rel).resolve()
            if source_path.exists():
                source_path.write_text(original_text, encoding="utf-8")
        restore = run_logged_ninja(project, touched_units)
        (round_dir / "restore.stdout.log").write_text(restore.stdout, encoding="utf-8")
        (round_dir / "restore.stderr.log").write_text(restore.stderr, encoding="utf-8")

    return results


def write_markdown(
    path: Path,
    args: argparse.Namespace,
    targets: list[TargetFunction],
    by_target: dict[tuple[str, str], list[ProbeCandidate]],
    generation_errors: dict[str, str],
    results: list[ProbeResult],
    started_at: str,
    elapsed_seconds: float,
) -> None:
    improved = [result for result in results if result.status == "improved"]
    perfect = [
        result
        for result in improved
        if result.candidate_match_percent is not None and result.candidate_match_percent >= 100.0
    ]
    compile_failed = [result for result in results if result.status == "batch_compile_failed"]
    diff_failed = [result for result in results if result.status == "diff_failed"]
    not_improved = [result for result in results if result.status == "not_improved"]

    lines = [
        f"# Decomp Batch Probe: {','.join(sorted(args.mutator_filter)) if args.mutator_filter else 'all'}",
        "",
        f"- Started: `{started_at}`",
        f"- Finished: `{utc_now()}`",
        f"- Elapsed seconds: `{elapsed_seconds:.1f}`",
        f"- Project: `{args.project}`",
        f"- Sort: `{args.sort}`",
        f"- Targets selected: `{len(targets)}`",
        f"- Targets with candidates: `{len(by_target)}`",
        f"- Rounds run: `{args.rounds}`",
        f"- Results measured: `{len(results)}`",
        f"- Improved: `{len(improved)}`",
        f"- 100%: `{len(perfect)}`",
        f"- Not improved: `{len(not_improved)}`",
        f"- Batch compile failed rows: `{len(compile_failed)}`",
        f"- Diff failed rows: `{len(diff_failed)}`",
        f"- Generation errors: `{len(generation_errors)}`",
        "",
        "This is an advisory report. The tool restores sources after each round. "
        "Manually apply and verify promising patches before keeping them.",
        "",
        "## Improvements",
        "",
        "| Round | Source | Function | Candidate | Old % | New % | Delta | Patch |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in sorted(
        improved,
        key=lambda item: (
            -(item.delta or 0.0),
            item.source_path,
            item.function,
            item.round_no,
        ),
    ):
        lines.append(
            f"| {result.round_no} | `{result.source_path}` | `{result.function}` | "
            f"{result.candidate_id or ''} | {fmt_pct(result.baseline_match_percent)} | "
            f"{fmt_pct(result.candidate_match_percent)} | {fmt_delta(result.delta)} | "
            f"`{result.patch_path}` |"
        )
    if not improved:
        lines.append("| | | | | | | | |")

    lines.extend(
        [
            "",
            "## Compile Or Diff Failures",
            "",
            "| Round | Status | Source | Function | Candidate | Error |",
            "| ---: | --- | --- | --- | ---: | --- |",
        ]
    )
    failures = compile_failed + diff_failed + [
        result for result in results if result.status in {"baseline_build_failed", "compose_failed"}
    ]
    for result in failures[:200]:
        error = result.error.replace("\n", " ")[:240]
        lines.append(
            f"| {result.round_no} | `{result.status}` | `{result.source_path}` | "
            f"`{result.function}` | {result.candidate_id or ''} | `{error}` |"
        )
    if len(failures) > 200:
        lines.append(f"| | | | | | `{len(failures) - 200} more omitted` |")
    if not failures:
        lines.append("| | | | | | |")

    lines.extend(
        [
            "",
            "## Candidate Coverage",
            "",
            "| Source | Function | Candidate count |",
            "| --- | --- | ---: |",
        ]
    )
    for (source_rel, function), candidates in sorted(by_target.items()):
        lines.append(f"| `{source_rel}` | `{function}` | {len(candidates)} |")

    if generation_errors:
        lines.extend(
            [
                "",
                "## Generation Errors",
                "",
                "| Target | Error |",
                "| --- | --- |",
            ]
        )
        for target, error in sorted(generation_errors.items()):
            lines.append(f"| `{target}` | `{error.replace(chr(10), ' ')[:240]}` |")

    lines.extend(
        [
            "",
            "## All Measured Results",
            "",
            "| Round | Status | Source | Function | Candidate | Old % | New % | Delta | Patch |",
            "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for result in results:
        lines.append(
            f"| {result.round_no} | `{result.status}` | `{result.source_path}` | "
            f"`{result.function}` | {result.candidate_id or ''} | "
            f"{fmt_pct(result.baseline_match_percent)} | "
            f"{fmt_pct(result.candidate_match_percent)} | {fmt_delta(result.delta)} | "
            f"`{result.patch_path}` |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch-probe one mutator over many functions and write a report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Mutator lane ids (negative ids are accepted, so -1 is the last lane):\n"
            f"{MUTATOR_LANE_HELP_TEXT}"
        ),
    )
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--objdiff", type=Path, default=DEFAULT_OBJDIFF)
    parser.add_argument("--objdiff-cli", type=Path, default=DEFAULT_OBJDIFF_CLI)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--sources")
    parser.add_argument("--functions")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--min-match", type=float)
    parser.add_argument(
        "--sort",
        choices=("report", "best-first", "worst-first"),
        default="best-first",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="How many candidate indexes to batch-probe. Round 1 uses candidate 1 per function.",
    )
    parser.add_argument(
        "--max-candidates-per-function",
        type=int,
        default=200,
        help="Maximum candidates to generate per function before selecting rounds.",
    )
    parser.add_argument(
        "--min-delta",
        type=float,
        default=0.00001,
        help="Minimum positive match percent delta considered improved.",
    )
    parser.add_argument(
        "--mutator-tier",
        choices=("v1a", "v1b", "all"),
        default="v1a",
    )
    parser.add_argument("--mutators")
    parser.add_argument(
        "--mutator-id",
        type=int,
        action="append",
        help="Exact mutator lane id to run. Repeatable; negative ids are accepted.",
    )
    parser.add_argument("--no-progress-bar", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    started_at = utc_now()
    start_time = time.monotonic()
    project = args.project.resolve()
    args.project = project
    args.report = project_path(project, args.report).resolve()
    args.objdiff = project_path(project, args.objdiff).resolve()
    args.objdiff_cli = project_path(project, args.objdiff_cli).resolve()
    args.mutator_tier = "v1b" if args.mutator_tier == "all" else args.mutator_tier
    args.mutator_filter = parse_mutator_filter(args.mutators, args.mutator_id)
    if args.rounds < 1:
        raise ValueError("--rounds must be at least 1")

    if args.output_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        args.output_dir = project / "build" / "decomp-batch-probe" / stamp
    else:
        args.output_dir = project_path(project, args.output_dir).resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    report = load_json(args.report)
    targets = select_targets(
        iter_unmatched_targets(report),
        split_csv(args.sources),
        split_csv(args.functions),
        args.min_match,
        args.sort,
        args.limit,
        args.start_index,
    )

    by_target, generation_errors = collect_candidates(project, args, targets)
    original_sources: dict[str, str] = {}
    for candidates in by_target.values():
        if not candidates:
            continue
        probe = candidates[0]
        if probe.source_rel not in original_sources:
            original_sources[probe.source_rel] = probe.source_path.read_text(encoding="utf-8")

    results: list[ProbeResult] = []
    for round_no in range(1, args.rounds + 1):
        round_results = run_round(
            project,
            args,
            round_no,
            by_target,
            original_sources,
            args.output_dir,
        )
        results.extend(round_results)

    payload = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "elapsed_seconds": time.monotonic() - start_time,
        "project": str(project),
        "report": str(args.report),
        "objdiff": str(args.objdiff),
        "mutator_tier": args.mutator_tier,
        "mutators": sorted(args.mutator_filter) if args.mutator_filter else None,
        "rounds": args.rounds,
        "target_count": len(targets),
        "candidate_target_count": len(by_target),
        "generation_errors": generation_errors,
        "results": [asdict(result) for result in results],
    }
    write_json(args.output_dir / "summary.json", payload)
    write_markdown(
        args.output_dir / "report.md",
        args,
        targets,
        by_target,
        generation_errors,
        results,
        started_at,
        payload["elapsed_seconds"],
    )

    print(f"Wrote {args.output_dir / 'summary.json'}")
    print(f"Wrote {args.output_dir / 'report.md'}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run(parse_args()))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
