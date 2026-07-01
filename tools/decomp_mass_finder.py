#!/usr/bin/env python3
"""Run decomp candidate searches across many nonmatching functions.

This is a driver around tools/decomp_candidates.py. It evaluates one function
at a time, applies only measured improvements, then retries that same function
from the improved source until no candidate improves it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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
        CandidateResult,
        DiffScore,
        ReportFunction,
        UnitInfo,
        candidate_sort_key,
        evaluate_candidates,
        find_unit_for_source,
        generate_candidates,
        load_json,
        load_report_functions,
        MUTATOR_LANE_HELP_TEXT,
        normalize_rel_path,
        parse_mutator_filter,
        path_relative_to_project,
        project_path,
        run_ninja,
        write_summary,
    )
except ModuleNotFoundError:  # Allows `python tools/decomp_mass_finder.py`.
    from decomp_candidates import (  # type: ignore[no-redef]
        DEFAULT_OBJDIFF,
        DEFAULT_OBJDIFF_CLI,
        DEFAULT_REPORT,
        Candidate,
        CandidateResult,
        DiffScore,
        ReportFunction,
        UnitInfo,
        candidate_sort_key,
        evaluate_candidates,
        find_unit_for_source,
        generate_candidates,
        load_json,
        load_report_functions,
        MUTATOR_LANE_HELP_TEXT,
        normalize_rel_path,
        parse_mutator_filter,
        path_relative_to_project,
        project_path,
        run_ninja,
        write_summary,
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
class AcceptedImprovement:
    source_path: str
    function: str
    pass_no: int
    candidate_id: int
    mutator: str
    description: str
    old_match_percent: float | None
    new_match_percent: float | None
    delta: float | None
    patch_path: str
    run_dir: str


@dataclass
class FunctionSummary:
    source_path: str
    function: str
    status: str
    passes: int = 0
    generated_candidates: int = 0
    ok_candidates: int = 0
    failed_candidates: int = 0
    initial_match_percent: float | None = None
    final_match_percent: float | None = None
    improvements: list[AcceptedImprovement] | None = None
    error: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.5f}"


def fmt_delta(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.5f}"


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

    if limit is not None:
        selected = selected[:limit]
    return selected


def perfect_first_candidate_sort_key(
    result: CandidateResult,
) -> tuple[bool, bool, float, float, float, int]:
    is_perfect = (
        result.candidate_match_percent is not None
        and result.candidate_match_percent >= 100.0
    )
    delta = result.match_delta if result.match_delta is not None else -999999.0
    match = (
        result.candidate_match_percent
        if result.candidate_match_percent is not None
        else -1.0
    )
    diff_score = (
        result.instruction_diff_score
        if result.instruction_diff_score is not None
        else 999999.0
    )
    return (
        result.candidate_match_percent is None,
        not is_perfect,
        -delta,
        -match,
        diff_score,
        result.candidate_id,
    )


def improvement_sort_key(
    result: CandidateResult, selection_policy: str
) -> tuple[object, ...]:
    if selection_policy == "perfect-first":
        return perfect_first_candidate_sort_key(result)
    return candidate_sort_key(result)


def best_improvement(
    results: Iterable[CandidateResult], min_delta: float, selection_policy: str
) -> CandidateResult | None:
    ok_results = [
        result
        for result in results
        if result.status == "ok"
        and result.match_delta is not None
        and result.match_delta > min_delta
    ]
    if not ok_results:
        return None
    return sorted(
        ok_results, key=lambda result: improvement_sort_key(result, selection_policy)
    )[0]


def safe_run_name(source_rel: str, function: str) -> str:
    raw = f"{source_rel}__{function}"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "__", raw)
    return safe[-180:]


class ProgressLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._file.close()

    def write(self, event: str, **fields: Any) -> str:
        parts = [utc_now(), event]
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value).replace("\n", " ")
            parts.append(f"{key}={text}")
        line = " ".join(parts)
        self._file.write(line + "\n")
        self._file.flush()
        return line


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def make_candidate_args(
    output_dir: Path,
    args: argparse.Namespace,
) -> argparse.Namespace:
    return SimpleNamespace(
        output_dir=output_dir,
        eval=True,
        jobs=1,
        keep_c_files=args.keep_c_files,
        stop_on_perfect=args.stop_on_perfect,
        mutator_tier=args.mutator_tier,
        mutator_filter=args.mutator_filter,
    )


def count_status(results: Iterable[CandidateResult]) -> tuple[int, int]:
    ok = 0
    failed = 0
    for result in results:
        if result.status == "ok":
            ok += 1
        elif result.status != "generated":
            failed += 1
    return ok, failed


def report_function_for_target(
    report_functions: dict[str, ReportFunction], target: TargetFunction
) -> dict[str, ReportFunction]:
    if target.function in report_functions:
        return {target.function: report_functions[target.function]}
    return {
        target.function: ReportFunction(
            fuzzy_match_percent=target.match_percent,
            size=target.size,
            virtual_address=target.virtual_address,
        )
    }


def run_target(
    project: Path,
    target: TargetFunction,
    unit: UnitInfo,
    args: argparse.Namespace,
    log: ProgressLog,
) -> FunctionSummary:
    source_path = project_path(project, target.source_path).resolve()
    source_rel = path_relative_to_project(project, source_path)
    function_dir = args.output_dir / "runs" / safe_run_name(source_rel, target.function)
    accepted_dir = args.output_dir / "accepted"
    accepted_dir.mkdir(parents=True, exist_ok=True)

    summary = FunctionSummary(
        source_path=source_rel,
        function=target.function,
        status="stable",
        initial_match_percent=target.match_percent,
        final_match_percent=target.match_percent,
        improvements=[],
    )

    pass_no = 1
    while True:
        if args.deadline is not None and time.monotonic() >= args.deadline:
            summary.status = "time_limit"
            log.write("TIME_LIMIT", source=source_rel, function=target.function)
            break
        if args.max_passes_per_function > 0 and pass_no > args.max_passes_per_function:
            summary.status = "pass_limit"
            log.write(
                "PASS_LIMIT",
                source=source_rel,
                function=target.function,
                passes=args.max_passes_per_function,
            )
            break

        source_text = source_path.read_text(encoding="utf-8")
        try:
            candidates = generate_candidates(
                source_text,
                source_rel,
                [target.function],
                args.max_candidates,
                args.mutator_tier,
                args.mutator_filter,
            )
        except Exception as exc:
            summary.status = "candidate_error"
            summary.error = str(exc)
            log.write(
                "CANDIDATE_ERROR",
                source=source_rel,
                function=target.function,
                error=exc,
            )
            break

        summary.generated_candidates += len(candidates)
        if not candidates:
            log.write(
                "NO_CANDIDATES",
                source=source_rel,
                function=target.function,
                pass_no=pass_no,
            )
            break

        pass_dir = function_dir / f"pass-{pass_no:02d}"
        candidate_args = make_candidate_args(pass_dir, args)
        report_functions = load_report_functions(args.report, source_rel, [target.function])
        log.write(
            "TRY",
            source=source_rel,
            function=target.function,
            pass_no=pass_no,
            candidates=len(candidates),
            baseline=fmt_pct(summary.final_match_percent),
        )

        try:
            results, baseline = evaluate_candidates(
                project,
                source_path,
                source_rel,
                unit,
                args.objdiff_cli,
                candidates,
                candidate_args,
            )
            write_summary(
                pass_dir,
                source_rel,
                unit,
                candidates,
                results,
                baseline,
                report_function_for_target(report_functions, target),
                candidate_args,
            )
        except Exception as exc:
            summary.status = "eval_error"
            summary.error = str(exc)
            log.write(
                "EVAL_ERROR",
                source=source_rel,
                function=target.function,
                pass_no=pass_no,
                error=exc,
            )
            break

        summary.passes = pass_no
        ok_count, failed_count = count_status(results)
        summary.ok_candidates += ok_count
        summary.failed_candidates += failed_count
        best = best_improvement(results, args.min_delta, args.selection_policy)
        if best is None:
            best_result = sorted(results, key=candidate_sort_key)[0] if results else None
            log.write(
                "STABLE",
                source=source_rel,
                function=target.function,
                pass_no=pass_no,
                ok=ok_count,
                failed=failed_count,
                best=fmt_pct(
                    best_result.candidate_match_percent if best_result is not None else None
                ),
                delta=fmt_delta(best_result.match_delta if best_result is not None else None),
            )
            break

        candidate_by_id: dict[int, Candidate] = {
            candidate.candidate_id: candidate for candidate in candidates
        }
        candidate = candidate_by_id[best.candidate_id]
        patch_name = (
            f"{safe_run_name(source_rel, target.function)}"
            f"__pass-{pass_no:02d}__cand-{best.candidate_id:06d}.patch"
        )
        patch_path = accepted_dir / patch_name
        patch_path.write_text(candidate.patch_text, encoding="utf-8")

        improvement = AcceptedImprovement(
            source_path=source_rel,
            function=target.function,
            pass_no=pass_no,
            candidate_id=best.candidate_id,
            mutator=best.mutator,
            description=best.description,
            old_match_percent=best.baseline_match_percent,
            new_match_percent=best.candidate_match_percent,
            delta=best.match_delta,
            patch_path=str(patch_path.relative_to(args.output_dir)),
            run_dir=str(pass_dir.relative_to(args.output_dir)),
        )

        if not args.apply:
            summary.status = "found_unapplied"
            summary.final_match_percent = best.candidate_match_percent
            summary.improvements.append(improvement)
            log.write(
                "FOUND",
                source=source_rel,
                function=target.function,
                pass_no=pass_no,
                candidate=best.candidate_id,
                mutator=best.mutator,
                old=fmt_pct(best.baseline_match_percent),
                new=fmt_pct(best.candidate_match_percent),
                delta=fmt_delta(best.match_delta),
                patch=improvement.patch_path,
            )
            break

        source_path.write_text(candidate.source_text, encoding="utf-8")
        build_proc = run_ninja(project, unit.base_path)
        if build_proc.returncode != 0:
            source_path.write_text(source_text, encoding="utf-8")
            run_ninja(project, unit.base_path)
            summary.status = "apply_build_failed"
            summary.error = build_proc.stderr.strip() or build_proc.stdout.strip()
            log.write(
                "APPLY_BUILD_FAILED",
                source=source_rel,
                function=target.function,
                pass_no=pass_no,
                candidate=best.candidate_id,
                error=summary.error,
            )
            break

        summary.improvements.append(improvement)
        summary.final_match_percent = best.candidate_match_percent
        log.write(
            "IMPROVED",
            source=source_rel,
            function=target.function,
            pass_no=pass_no,
            candidate=best.candidate_id,
            mutator=best.mutator,
            old=fmt_pct(best.baseline_match_percent),
            new=fmt_pct(best.candidate_match_percent),
            delta=fmt_delta(best.match_delta),
            patch=improvement.patch_path,
        )

        if args.stop_on_perfect and best.candidate_match_percent is not None:
            if best.candidate_match_percent >= 100.0:
                summary.status = "perfect"
                break
        pass_no += 1

    if summary.improvements:
        if summary.status == "stable":
            summary.status = "improved"
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mass-run decomp candidate searches over nonmatching functions.",
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
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Default: build/decomp-mass-finder/<UTC timestamp>.",
    )
    parser.add_argument(
        "--progress-log",
        type=Path,
        help="Default: <output-dir>/progress.txt.",
    )
    parser.add_argument(
        "--sources",
        help="Optional comma-separated source paths to include.",
    )
    parser.add_argument(
        "--functions",
        help="Optional comma-separated function names to include.",
    )
    parser.add_argument("--limit", type=int, help="Maximum target functions to scan.")
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Skip this many selected targets after filtering and sorting.",
    )
    parser.add_argument(
        "--min-match",
        type=float,
        help="Only scan functions with at least this current match percent.",
    )
    parser.add_argument(
        "--sort",
        choices=("report", "best-first", "worst-first"),
        default="best-first",
    )
    parser.add_argument("--max-candidates", type=int, default=200)
    parser.add_argument(
        "--max-passes-per-function",
        type=int,
        default=0,
        help="0 means retry until stable.",
    )
    parser.add_argument(
        "--min-delta",
        type=float,
        default=0.00001,
        help="Minimum positive match percent delta required before applying.",
    )
    parser.add_argument(
        "--selection-policy",
        choices=("delta", "perfect-first"),
        default="delta",
        help=(
            "How to choose among improving candidates. delta keeps existing "
            "largest-delta behavior; perfect-first prefers candidates that "
            "reach 100%% before falling back to delta."
        ),
    )
    parser.add_argument(
        "--mutator-tier",
        choices=("v1a", "v1b", "all"),
        default="v1a",
    )
    parser.add_argument(
        "--mutators",
        help="Optional comma-separated mutator names to pass to decomp_candidates.",
    )
    parser.add_argument(
        "--mutator-id",
        type=int,
        action="append",
        help="Exact mutator lane id to run. Repeatable; negative ids are accepted.",
    )
    parser.add_argument(
        "--keep-c-files",
        action="store_true",
        help="Keep full candidate source files inside each pass directory.",
    )
    parser.add_argument(
        "--stop-on-perfect",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--apply",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Apply measured improvements. Use --no-apply for advisory mode.",
    )
    parser.add_argument(
        "--time-limit-minutes",
        type=float,
        help="Stop starting new passes after this many minutes.",
    )
    parser.add_argument(
        "--no-progress-bar",
        action="store_true",
        help="Disable tqdm progress output.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    project = args.project.resolve()
    args.report = project_path(project, args.report).resolve()
    args.objdiff = project_path(project, args.objdiff).resolve()
    args.objdiff_cli = project_path(project, args.objdiff_cli).resolve()
    args.mutator_tier = "v1b" if args.mutator_tier == "all" else args.mutator_tier
    args.mutator_filter = parse_mutator_filter(args.mutators, args.mutator_id)
    if args.output_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        args.output_dir = project / "build" / "decomp-mass-finder" / stamp
    else:
        args.output_dir = project_path(project, args.output_dir).resolve()
    if args.progress_log is None:
        args.progress_log = args.output_dir / "progress.txt"
    else:
        args.progress_log = project_path(project, args.progress_log).resolve()
    args.deadline = (
        time.monotonic() + args.time_limit_minutes * 60
        if args.time_limit_minutes is not None
        else None
    )

    report = load_json(args.report)
    targets = select_targets(
        iter_unmatched_targets(report),
        split_csv(args.sources),
        split_csv(args.functions),
        args.min_match,
        args.sort,
        args.limit,
    )
    total_targets = len(targets)
    if args.start_index < 0:
        raise ValueError("--start-index must be non-negative")
    if args.start_index:
        targets = targets[args.start_index :]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    log = ProgressLog(args.progress_log)
    summaries: list[FunctionSummary] = []
    unit_cache: dict[str, UnitInfo] = {}
    run_summary_path = args.output_dir / "summary.json"

    started_at = utc_now()
    log.write(
        "START",
        total=total_targets,
        selected=len(targets),
        start_index=args.start_index,
        tier=args.mutator_tier,
        mutators=",".join(sorted(args.mutator_filter)) if args.mutator_filter else "all",
        max_candidates=args.max_candidates,
        max_passes=args.max_passes_per_function,
        selection_policy=args.selection_policy,
        apply=args.apply,
        output=args.output_dir,
    )

    progress_iter: Iterable[TargetFunction]
    progress_bar = None
    if tqdm is not None and not args.no_progress_bar:
        progress_bar = tqdm(targets, unit="fn")
        progress_iter = progress_bar
    else:
        progress_iter = targets

    try:
        for index, target in enumerate(progress_iter, start=1):
            if args.deadline is not None and time.monotonic() >= args.deadline:
                log.write("TIME_LIMIT", checked=index - 1, total=len(targets))
                break
            if progress_bar is not None:
                progress_bar.set_description(target.function[:40])

            try:
                unit = unit_cache.get(target.source_path)
                if unit is None:
                    unit = find_unit_for_source(args.objdiff, target.source_path)
                    unit_cache[target.source_path] = unit
                summary = run_target(project, target, unit, args, log)
            except Exception as exc:
                summary = FunctionSummary(
                    source_path=target.source_path,
                    function=target.function,
                    status="error",
                    initial_match_percent=target.match_percent,
                    final_match_percent=target.match_percent,
                    improvements=[],
                    error=str(exc),
                )
                log.write(
                    "ERROR",
                    index=index,
                    total=len(targets),
                    source=target.source_path,
                    function=target.function,
                    error=exc,
                )

            summaries.append(summary)
            write_json(
                run_summary_path,
                {
                    "started_at": started_at,
                    "updated_at": utc_now(),
                    "project": str(project),
                    "report": str(args.report),
                    "objdiff": str(args.objdiff),
                    "apply": args.apply,
                    "mutator_tier": args.mutator_tier,
                    "mutators": sorted(args.mutator_filter) if args.mutator_filter else None,
                    "max_candidates": args.max_candidates,
                    "min_delta": args.min_delta,
                    "selection_policy": args.selection_policy,
                    "target_count": total_targets,
                    "selected_count": len(targets),
                    "start_index": args.start_index,
                    "completed_count": len(summaries),
                    "improvement_count": sum(
                        len(summary.improvements or []) for summary in summaries
                    ),
                    "functions": [asdict(summary) for summary in summaries],
                },
            )
    finally:
        if progress_bar is not None:
            progress_bar.close()
        log.write(
            "DONE",
            checked=len(summaries),
            total=total_targets,
            selected=len(targets),
            start_index=args.start_index,
            improvements=sum(len(summary.improvements or []) for summary in summaries),
            summary=run_summary_path,
        )
        log.close()

    print(f"Wrote {run_summary_path}")
    print(f"Wrote {args.progress_log}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run(parse_args()))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
