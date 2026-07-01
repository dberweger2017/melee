#!/usr/bin/env python3
"""Generate and rank source-shape candidates for decompilation matching.

The tool is intentionally conservative: it mutates one requested function at a
time, compiles the owning object, scores the function with objdiff-cli, and
restores the original source before exiting.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import difflib
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = Path("build/GALE01/report.json")
DEFAULT_OBJDIFF = Path("objdiff.json")
DEFAULT_OBJDIFF_CLI = Path("build/tools/objdiff-cli")

MUTATOR_HELP = [
    (
        "v1a",
        "pad-stack",
        "Try nearby PAD_STACK sizes. Based on recent stack-frame fixes.",
    ),
    (
        "v1a",
        "float-literal",
        "Toggle simple decimal float spelling such as 0.0f and 0.0.",
    ),
    (
        "v1a",
        "assignment-split/combine",
        "Split or combine simple arithmetic assignments to alter live ranges.",
    ),
    (
        "v1a",
        "loop-init-fold",
        "Fold a simple assignment immediately before a for loop into the initializer.",
    ),
    (
        "v1b",
        "loop-first-null-init",
        "Initialize a loop-carried first pointer to NULL inside the for initializer.",
    ),
    (
        "v1a",
        "fake-use",
        "Insert (void) temp; after a simple assignment to perturb scheduling.",
    ),
    (
        "v1b",
        "zero-assignment-expression",
        "Thread a scalar zero assignment through a later zero-valued field store.",
    ),
    (
        "v1b",
        "assignment-expression",
        "Combine two same-value assignments, optionally preserving the temp reset.",
    ),
    (
        "v1b",
        "scoped-check-temp",
        "Wrap call+if checks in a local block with a duplicated check temp.",
    ),
    (
        "v1b",
        "declaration-move/narrow/widen/reorder",
        "Move simple declarations between top scope and first use, and swap adjacent declarations.",
    ),
    (
        "v1b",
        "declaration-promote",
        "Promote one later declaration within the same declaration section for MWCC register coloring.",
    ),
    (
        "v1b",
        "alias-remove/introduce",
        "Remove simple pointer aliases or introduce a bounded fp->x1A88 alias.",
    ),
    (
        "v1b",
        "abs-shape",
        "Toggle a simple manual absolute-value block and ABS(expr).",
    ),
    (
        "v1b",
        "clamp-branch-shape",
        "Expand simple MIN/MAX assignments into explicit branch clamps.",
    ),
    (
        "v1b",
        "local-pad-array-size",
        "Try nearby sizes for local byte padding arrays such as u8 pad[28].",
    ),
    (
        "v1b",
        "call-repeated-float-temp",
        "Introduce a scoped float temp for repeated member loads in one call.",
    ),
    (
        "v1b",
        "vararg-base-arg",
        "Append a divided expression's base value to OSReport-style vararg calls.",
    ),
    (
        "v1b",
        "assignment-in-condition",
        "Fold a simple preceding assignment into an immediately following if condition.",
    ),
    (
        "v1b",
        "global-struct-local-alias",
        "Introduce a local pointer alias for repeated global struct field accesses.",
    ),
    (
        "v1b",
        "pointer-iteration-vs-index",
        "Convert a simple indexed array loop into pointer iteration.",
    ),
    (
        "v1b",
        "vec-scalar-triplet",
        "Toggle Vec3 locals and simple x/y/z scalar triplets.",
    ),
    (
        "v1b",
        "remove-local-copy",
        "Remove a redundant local copy of a parameter or simple local expression.",
    ),
    (
        "v1b",
        "inline-negated-clamp",
        "Inline a temporary negative clamp bound into the comparison and assignment.",
    ),
    (
        "v1b",
        "stack-layout-pad",
        "Probe focused stack-frame pad and pad-declaration ordering changes.",
    ),
    (
        "v1b",
        "existing-pad-decl-order",
        "Move existing byte pad declarations earlier within a local declaration block.",
    ),
    (
        "v1b",
        "operand-layout",
        "Probe operand-only stack/register mismatches with scoped pads, address-taken locals, and scalar temps.",
    ),
]

MUTATOR_LANES = [
    ("v1a", "pad-stack", "Try nearby PAD_STACK sizes."),
    ("v1a", "float-literal", "Toggle simple decimal float spelling."),
    ("v1a", "assignment-split", "Split simple arithmetic assignments."),
    ("v1a", "assignment-combine", "Combine simple arithmetic assignments."),
    ("v1a", "loop-init-fold", "Fold a pre-loop assignment into a for initializer."),
    ("v1b", "loop-first-null-init", "Initialize a loop-carried first pointer in the for initializer."),
    ("v1a", "fake-use", "Insert (void) temp; after simple assignments."),
    ("v1b", "zero-assignment-expression", "Thread scalar zero assignments through later zero stores."),
    ("v1b", "assignment-expression", "Combine same-value assignments."),
    ("v1b", "scoped-check-temp", "Wrap call/check patterns in a local block."),
    ("v1b", "declaration-move", "Move simple declarations near first use."),
    ("v1b", "declaration-widen", "Widen declaration lifetime."),
    ("v1b", "declaration-reorder", "Swap adjacent declarations."),
    ("v1b", "declaration-promote", "Promote later declarations within a declaration section."),
    ("v1b", "alias-remove", "Remove simple pointer aliases."),
    ("v1b", "alias-introduce", "Introduce bounded pointer aliases."),
    ("v1b", "abs-shape", "Toggle manual absolute-value block and ABS(expr)."),
    ("v1b", "clamp-branch-shape", "Expand simple MIN/MAX assignments into branch clamps."),
    ("v1b", "local-pad-array-size", "Try nearby local byte padding array sizes."),
    ("v1b", "call-repeated-float-temp", "Introduce a scoped float temp for repeated member loads."),
    ("v1b", "vararg-base-arg", "Append a divided expression's base value to vararg calls."),
    ("v1b", "assignment-in-condition", "Fold an assignment into the following if condition."),
    ("v1b", "global-struct-local-alias", "Introduce local aliases for repeated global struct access."),
    ("v1b", "pointer-iteration-vs-index", "Convert simple indexed loops to pointer iteration."),
    ("v1b", "vec-scalar-triplet", "Toggle Vec3 locals and scalar x/y/z locals."),
    ("v1b", "remove-local-copy", "Remove redundant local copies."),
    ("v1b", "inline-negated-clamp", "Inline a temporary negative clamp bound."),
    ("v1b", "stack-layout-pad", "Probe focused stack-frame pad layout changes."),
    ("v1b", "operand-layout", "Probe operand-only stack/register layout mismatches."),
    (
        "v1b",
        "existing-pad-decl-order",
        "Move existing byte pad declarations earlier within a declaration block.",
    ),
]

MUTATOR_NAMES = {name for _, name, _ in MUTATOR_LANES}
SINGLE_STEP_MUTATORS = {"stack-layout-pad", "existing-pad-decl-order"}
MUTATOR_LANE_HELP_TEXT = "\n".join(
    f"  {idx:>2}  {tier:3}  {name:30} {description}"
    for idx, (tier, name, description) in enumerate(MUTATOR_LANES)
)

HELP_EPILOG = f"""\
Agent workflow:
  1. Start conservative with --mutator-tier v1a and a small --max-candidates.
  2. Inspect summary.md and best.patch; apply manually only after checking the diff.
  3. If v1a does not help, retry with --mutator-tier v1b for more speculative shapes.
  4. The tool restores the source file after eval; output patches are advisory.

Examples:
  Generate and rank candidates:
    {Path(sys.argv[0]).name} --path src/melee/gm/gmmain_lib.c \\
      --functions gmMainLib_8015F600 --max-candidates 100 --eval

  Generate patches only:
    {Path(sys.argv[0]).name} --path src/melee/ft/ft_0899.c \\
      --functions ft_80089B08 --generate-only --mutator-tier v1b

  Show available mutators:
    {Path(sys.argv[0]).name} --list-mutators

  Run one mutator family:
    {Path(sys.argv[0]).name} --path src/melee/ft/ft_0899.c \\
      --functions ft_80089B08 --generate-only --mutator-tier v1b \\
      --mutators scoped-check-temp

  Run one mutator lane by id:
    {Path(sys.argv[0]).name} --path src/melee/ft/ft_0899.c \\
      --functions ft_80089B08 --mutator-tier v1b --mutator-id -1

Mutator lane ids (negative ids are accepted, so -1 is the last lane):
{MUTATOR_LANE_HELP_TEXT}

Outputs:
  summary.json is for agents; summary.md is for humans.
  candidates/cand-N.patch contains each candidate patch.
  candidates/cand-N.diff.json is written for evaluated candidates.
  best.patch is written only when a candidate improves the measured match.
"""


@dataclass(frozen=True)
class FunctionSpan:
    name: str
    start: int
    body_open: int
    body_close: int
    end: int


@dataclass(frozen=True)
class UnitInfo:
    name: str
    source_path: str
    base_path: str


@dataclass(frozen=True)
class Mutation:
    tier: str
    mutator: str
    description: str
    body: str


@dataclass(frozen=True)
class Candidate:
    candidate_id: int
    function: str
    tier: str
    mutator: str
    description: str
    source_text: str
    patch_text: str


@dataclass
class CandidateResult:
    candidate_id: int
    function: str
    tier: str
    mutator: str
    description: str
    status: str
    patch_path: str
    c_path: str | None = None
    diff_path: str | None = None
    baseline_match_percent: float | None = None
    candidate_match_percent: float | None = None
    match_delta: float | None = None
    instruction_diff_score: float | None = None
    target_instruction_count: int | None = None
    source_instruction_count: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""


@dataclass(frozen=True)
class DiffScore:
    match_percent: float | None
    instruction_diff_score: float | None
    target_instruction_count: int
    source_instruction_count: int


@dataclass(frozen=True)
class ReportFunction:
    fuzzy_match_percent: float | None
    size: int | None
    virtual_address: str


def strip_noise(text: str) -> str:
    """Blank comments and string/char literals while preserving offsets."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c in "\"'":
            quote = c
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def find_matching(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return -1


def find_signature_start(code: str, name_idx: int) -> int:
    # Function replacement only uses the body span, but start/end make tests and
    # diagnostics clearer. Keep this conservative and line-oriented.
    line_start = code.rfind("\n", 0, name_idx) + 1
    prev_line_end = line_start - 1
    if prev_line_end <= 0:
        return line_start
    prev_line_start = code.rfind("\n", 0, prev_line_end) + 1
    prev_line = code[prev_line_start:prev_line_end].strip()
    if prev_line and not prev_line.endswith((";", "}", "{")):
        return prev_line_start
    return line_start


def find_function_span(text: str, name: str) -> FunctionSpan:
    code = strip_noise(text)
    pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
    for match in pattern.finditer(code):
        open_idx = code.find("(", match.end() - 1)
        close_idx = find_matching(code, open_idx, "(", ")")
        if close_idx < 0:
            continue
        idx = close_idx + 1
        while idx < len(code) and code[idx].isspace():
            idx += 1
        if idx >= len(code) or code[idx] != "{":
            continue
        body_close = find_matching(code, idx, "{", "}")
        if body_close < 0:
            continue
        start = find_signature_start(code, match.start())
        return FunctionSpan(name, start, idx, body_close, body_close + 1)
    raise ValueError(f"function not found or has no body: {name}")


def normalize_rel_path(path: Path | str) -> str:
    return Path(str(path).replace("\\", "/")).as_posix().removeprefix("./")


def project_path(project: Path, path: Path | str) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path
    return project / path


def path_relative_to_project(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_unit_for_source(objdiff_path: Path, source_rel: str) -> UnitInfo:
    objdiff = load_json(objdiff_path)
    source_rel = normalize_rel_path(source_rel)
    for unit in objdiff.get("units", []):
        metadata = unit.get("metadata", {})
        unit_source = normalize_rel_path(metadata.get("source_path", ""))
        if unit_source == source_rel:
            return UnitInfo(
                name=unit["name"],
                source_path=unit_source,
                base_path=unit["base_path"],
            )
    raise ValueError(f"source path not found in {objdiff_path}: {source_rel}")


def load_report_functions(
    report_path: Path, source_rel: str, functions: Iterable[str]
) -> dict[str, ReportFunction]:
    if not report_path.exists():
        return {}
    report = load_json(report_path)
    wanted = set(functions)
    source_rel = normalize_rel_path(source_rel)
    result: dict[str, ReportFunction] = {}
    for unit in report.get("units", []):
        metadata = unit.get("metadata", {})
        if normalize_rel_path(metadata.get("source_path", "")) != source_rel:
            continue
        for function in unit.get("functions") or []:
            name = function.get("name")
            if name not in wanted:
                continue
            fuzzy = function.get("fuzzy_match_percent")
            result[name] = ReportFunction(
                fuzzy_match_percent=float(fuzzy) if fuzzy is not None else None,
                size=int(function["size"]) if function.get("size") is not None else None,
                virtual_address=str(
                    function.get("metadata", {}).get("virtual_address", "")
                ),
            )
    return result


def unified_patch(source_rel: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{source_rel}",
            tofile=f"b/{source_rel}",
        )
    )


def replace_function_body(
    source_text: str, span: FunctionSpan, new_body: str
) -> str:
    return source_text[: span.body_open + 1] + new_body + source_text[span.body_close :]


def iter_mutations(body: str, enabled_tiers: set[str]) -> Iterator[Mutation]:
    mutators = [
        mutate_pad_stack,
        mutate_float_literals,
        mutate_assignment_shape,
        mutate_loop_initializer,
        mutate_loop_first_null_initializer,
        mutate_fake_uses,
        mutate_zero_assignment_expression,
        mutate_assignment_expression,
        mutate_scoped_check_temp,
        mutate_declaration_movement,
        mutate_declaration_widening,
        mutate_declaration_reorder,
        mutate_declaration_promote,
        mutate_alias_remove,
        mutate_alias_introduce_fp_x1a88,
        mutate_abs_shape,
        mutate_clamp_branch_shape,
        mutate_local_pad_array_size,
        mutate_call_repeated_float_temp,
        mutate_vararg_base_arg,
        mutate_assignment_in_condition,
        mutate_global_struct_local_alias,
        mutate_pointer_iteration_vs_index,
        mutate_vec_scalar_triplet,
        mutate_remove_local_copy,
        mutate_inline_negated_clamp,
        mutate_stack_layout_pad,
        mutate_operand_layout,
        mutate_existing_pad_decl_order,
    ]
    for mutator in mutators:
        for mutation in mutator(body):
            if mutation.tier in enabled_tiers:
                yield mutation


def resolve_mutator_id(mutator_id: int) -> str:
    index = mutator_id
    if index < 0:
        index = len(MUTATOR_LANES) + index
    if index < 0 or index >= len(MUTATOR_LANES):
        raise ValueError(
            f"unknown mutator id {mutator_id}; valid ids are "
            f"0..{len(MUTATOR_LANES) - 1} or negative indexes down to "
            f"{-len(MUTATOR_LANES)}"
        )
    return MUTATOR_LANES[index][1]


def parse_mutator_filter(
    raw_filter: str | None, mutator_ids: Iterable[int] | None = None
) -> set[str] | None:
    names = {name.strip() for name in (raw_filter or "").split(",") if name.strip()}
    if mutator_ids is not None:
        names.update(resolve_mutator_id(mutator_id) for mutator_id in mutator_ids)
    if not names:
        return None
    unknown = sorted(name for name in names if name not in MUTATOR_NAMES)
    if unknown:
        raise ValueError(
            "unknown mutator name(s): "
            + ", ".join(unknown)
            + ". Use --list-mutators to see valid lane ids and names."
        )
    return names


def make_mutation(
    tier: str, mutator: str, description: str, body: str
) -> Mutation:
    return Mutation(tier=tier, mutator=mutator, description=description, body=body)


def splice(text: str, start: int, end: int, replacement: str) -> str:
    return text[:start] + replacement + text[end:]


_ASSIGNMENT_OPERATOR_RE = re.compile(
    r"<<=|>>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|(?<![=!<>])=(?!=)"
)


def is_likely_pure_expr(expr: str) -> bool:
    stripped = strip_noise(expr).strip()
    if not stripped:
        return False
    if "++" in stripped or "--" in stripped or "," in stripped:
        return False
    if _ASSIGNMENT_OPERATOR_RE.search(stripped):
        return False
    if re.search(r"\b[A-Za-z_]\w*\s*\(", stripped):
        return False
    return True


def mutate_pad_stack(body: str) -> Iterator[Mutation]:
    pattern = re.compile(r"PAD_STACK\(\s*(?P<size>\d+)\s*\)\s*;")
    for match in pattern.finditer(body):
        size = int(match.group("size"))
        for delta in (-16, -12, -8, -4, 4, 8, 12, 16):
            new_size = size + delta
            if new_size < 0:
                continue
            replacement = f"PAD_STACK({new_size});"
            yield make_mutation(
                "v1a",
                "pad-stack",
                f"change PAD_STACK({size}) to PAD_STACK({new_size})",
                splice(body, match.start(), match.end(), replacement),
            )


def mutate_float_literals(body: str) -> Iterator[Mutation]:
    remove_suffix = re.compile(r"(?<![\w.])(?P<num>\d+)\.0(?P<suffix>[fF])\b")
    add_suffix = re.compile(r"(?<![\w.])(?P<num>\d+)\.0\b(?![fFlL])")

    for match in remove_suffix.finditer(body):
        replacement = f"{match.group('num')}.0"
        yield make_mutation(
            "v1a",
            "float-literal",
            f"remove float suffix from {match.group(0)}",
            splice(body, match.start(), match.end(), replacement),
        )

    for match in add_suffix.finditer(body):
        replacement = f"{match.group('num')}.0f"
        yield make_mutation(
            "v1a",
            "float-literal",
            f"add float suffix to {match.group(0)}",
            splice(body, match.start(), match.end(), replacement),
        )


_SIMPLE_LVALUE = r"[A-Za-z_]\w*(?:(?:->|\.)[A-Za-z_]\w*|\[[^\]\n;]+\])*"
_SIMPLE_EXPR = rf"{_SIMPLE_LVALUE}(?:\s*\([^;\n]*\))?"


def mutate_assignment_shape(body: str) -> Iterator[Mutation]:
    split_pattern = re.compile(
        rf"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        rf"(?P<left>{_SIMPLE_EXPR})\s*(?P<op>[+-])\s*"
        rf"(?P<right>{_SIMPLE_EXPR})\s*;"
    )
    for match in split_pattern.finditer(body):
        indent = match.group("indent")
        var = match.group("var")
        op = match.group("op")
        replacement = (
            f"{indent}{var} = {match.group('left')};\n"
            f"{indent}{var} {op}= {match.group('right')};"
        )
        yield make_mutation(
            "v1a",
            "assignment-split",
            f"split {var} assignment around {op}",
            splice(body, match.start(), match.end(), replacement),
        )

    combine_pattern = re.compile(
        rf"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        rf"(?P<left>{_SIMPLE_EXPR})\s*;\n"
        rf"(?P=indent)(?P=var)\s*(?P<op>[+-])=\s*"
        rf"(?P<right>{_SIMPLE_EXPR})\s*;"
    )
    for match in combine_pattern.finditer(body):
        replacement = (
            f"{match.group('indent')}{match.group('var')} = "
            f"{match.group('left')} {match.group('op')} {match.group('right')};"
        )
        yield make_mutation(
            "v1a",
            "assignment-combine",
            f"combine split {match.group('var')} assignment",
            splice(body, match.start(), match.end(), replacement),
        )


def mutate_loop_initializer(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<expr>[^;\n]+);\n(?P=indent)for\s*\(\s*(?P<init>[^;\n]+)"
    )
    for match in pattern.finditer(body):
        replacement = (
            f"{match.group('indent')}for ({match.group('var')} = "
            f"{match.group('expr').strip()}, {match.group('init').strip()}"
        )
        yield make_mutation(
            "v1a",
            "loop-init-fold",
            f"fold {match.group('var')} assignment into for initializer",
            splice(body, match.start(), match.end(), replacement),
        )


def mutate_loop_first_null_initializer(body: str) -> Iterator[Mutation]:
    code = strip_noise(body)
    for_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)for\s*\(\s*(?P<init>[^;\n]+)"
    )
    for match in for_pattern.finditer(code):
        open_idx = code.find("{", match.end())
        if open_idx < 0:
            continue
        loop_close = find_matching(code, open_idx, "{", "}")
        if loop_close < 0:
            continue
        loop_body = body[open_idx + 1 : loop_close]
        after_loop = body[loop_close + 1 :]
        before_loop = body[: match.start()]
        for decl in _DECL_PATTERN.finditer(before_loop):
            var = decl.group("var")
            if "*" not in decl.group("type"):
                continue
            if var in match.group("init"):
                continue
            if has_identifier(before_loop[decl.end() :], var):
                continue
            if not has_identifier(after_loop, var):
                continue
            first_assignment = re.search(
                rf"(?m)^\s*if\s*\([^;\n]*==\s*0[^;\n]*\)\s*\{{\s*\n"
                rf"\s*{re.escape(var)}\s*=",
                loop_body,
            )
            if first_assignment is None:
                continue
            for zero in ("NULL", "0"):
                replacement = f"{var} = {zero}, {match.group('init').strip()}"
                yield make_mutation(
                    "v1b",
                    "loop-first-null-init",
                    f"initialize {var} to {zero} in for initializer",
                    splice(body, match.start("init"), match.end("init"), replacement),
                )


def mutate_fake_uses(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*[^;\n]+;\n?"
    )
    for match in pattern.finditer(body):
        var = match.group("var")
        insert_at = match.end()
        if insert_at > 0 and body[insert_at - 1] == "\n":
            insert_at -= 1
        next_line_start = match.end()
        next_line_end = body.find("\n", next_line_start)
        next_line_end = len(body) if next_line_end < 0 else next_line_end
        if f"(void) {var};" in body[next_line_start:next_line_end]:
            continue
        replacement = f"\n{match.group('indent')}(void) {var};"
        yield make_mutation(
            "v1a",
            "fake-use",
            f"insert fake use for {var}",
            splice(body, insert_at, insert_at, replacement),
        )


def mutate_zero_assignment_expression(body: str) -> Iterator[Mutation]:
    scalar_zero_re = re.compile(
        r"^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<zero>0(?:\.0[fF]?)?)\s*;\n?$"
    )
    assignment_re = re.compile(
        rf"^(?P<indent>[ \t]+)(?P<lhs>{_SIMPLE_LVALUE})\s*=\s*"
        r"(?P<rhs>[^;\n]+);\n?$"
    )
    lines = body.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)

    for i, line in enumerate(lines):
        source = scalar_zero_re.match(line)
        if source is None:
            continue
        indent = source.group("indent")
        var = source.group("var")
        zero = source.group("zero")
        for j in range(i + 1, min(i + 7, len(lines))):
            between = "".join(lines[i + 1 : j])
            if has_identifier(between, var):
                break
            if any(
                candidate.strip() and assignment_re.match(candidate) is None
                for candidate in lines[i + 1 : j]
            ):
                break
            target = assignment_re.match(lines[j])
            if target is None:
                continue
            if target.group("indent") != indent:
                break
            if target.group("lhs") == var:
                continue
            if target.group("rhs").strip() != zero:
                continue
            replacement = (
                between
                + f"{indent}{target.group('lhs')} = ({var} = {zero});\n"
                + f"{indent}{var} = {zero};\n"
            )
            yield make_mutation(
                "v1b",
                "zero-assignment-expression",
                f"thread {var} = {zero} through {target.group('lhs')}",
                splice(body, offsets[i], offsets[j] + len(lines[j]), replacement),
            )


def mutate_assignment_expression(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        rf"(?m)^(?P<indent1>[ \t]+)(?P<first>[A-Za-z_]\w*)\s*=\s*"
        rf"(?P<expr>[^;\n]+);\n"
        rf"(?P<indent2>[ \t]+)(?P<second>{_SIMPLE_LVALUE})\s*=\s*"
        rf"(?P<expr2>[^;\n]+);"
    )
    for match in pattern.finditer(body):
        expr = match.group("expr").strip()
        if expr != match.group("expr2").strip():
            continue
        if not is_likely_pure_expr(expr):
            continue
        if match.group("first") == match.group("second"):
            continue
        replacement = (
            f"{match.group('indent2')}{match.group('second')} = "
            f"({match.group('first')} = {expr});"
        )
        yield make_mutation(
            "v1b",
            "assignment-expression",
            f"combine {match.group('first')} assignment into {match.group('second')}",
            splice(body, match.start(), match.end(), replacement),
        )
        replacement_with_reset = (
            f"{match.group('indent2')}{match.group('second')} = "
            f"({match.group('first')} = {expr});\n"
            f"{match.group('indent2')}{match.group('first')} = {expr};"
        )
        yield make_mutation(
            "v1b",
            "assignment-expression",
            f"combine {match.group('first')} assignment into "
            f"{match.group('second')} and keep reset",
            splice(body, match.start(), match.end(), replacement_with_reset),
        )


def mutate_scoped_check_temp(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        rf"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        rf"(?P<call>[A-Za-z_]\w*\([^;\n]*\))\s*;\n"
        rf"(?P=indent)if\s*\(\s*(?P=var)\s*(?P<cmp>!=|==)\s*"
        rf"(?P<const>-?\d+|NULL)\s*(?P<rest>&&[^\n]+)?\)\s*\{{"
    )
    for match in pattern.finditer(body):
        indent = match.group("indent")
        inner = indent + "    "
        var = match.group("var")
        check = f"{var}_check"
        if has_identifier(body, check):
            continue
        if_close = find_matching(strip_noise(body), match.end() - 1, "{", "}")
        if if_close < 0:
            continue
        condition_tail = match.group("rest") or ""
        if condition_tail and not condition_tail.startswith(" "):
            condition_tail = f" {condition_tail}"
        replacement = (
            f"{indent}{{\n"
            f"{inner}s32 {check} = {match.group('call')};\n"
            f"{inner}s32 {var} = {check};\n"
            f"{inner}if ({check} {match.group('cmp')} {match.group('const')}"
            f"{condition_tail}) {{"
        )
        new_body = splice(body, match.start(), match.end(), replacement)
        shifted_if_close = if_close + len(replacement) - (match.end() - match.start())
        body_start = new_body.find("\n", match.start() + len(replacement))
        if body_start >= 0 and body_start < shifted_if_close:
            original_inner = new_body[body_start + 1 : shifted_if_close]
            indented_inner = "".join(
                f"    {line}" if line.strip() else line
                for line in original_inner.splitlines(keepends=True)
            )
            new_body = (
                new_body[: body_start + 1]
                + indented_inner
                + new_body[shifted_if_close:]
            )
            shifted_if_close += len(indented_inner) - len(original_inner)
        new_body = splice(
            new_body,
            shifted_if_close + 1,
            shifted_if_close + 1,
            f"\n{indent}}}",
        )
        yield make_mutation(
            "v1b",
            "scoped-check-temp",
            f"wrap {var} call check in a scoped duplicated temp",
            new_body,
        )


_DECL_PATTERN = re.compile(
    r"(?m)^(?P<indent>[ \t]+)(?P<type>(?:(?:const|volatile)\s+)?"
    r"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s*\*)*)\s+"
    r"(?P<var>[A-Za-z_]\w*)\s*;\n"
)

_INIT_DECL_PATTERN = re.compile(
    r"(?m)^(?P<indent>[ \t]+)(?P<type>(?:(?:const|volatile)\s+)?"
    r"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s*\*)*)\s+"
    r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<expr>[^;\n]+);\n"
)


def has_identifier(text: str, name: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", strip_noise(text)) is not None


def has_standalone_identifier(text: str, name: str) -> bool:
    return (
        re.search(
            rf"(?<![.>])\b{re.escape(name)}\b",
            strip_noise(text),
        )
        is not None
    )


def body_lines(body: str) -> tuple[list[str], list[int]]:
    lines = body.splitlines(keepends=True)
    offsets = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    return lines, offsets


def is_simple_declaration_line(line: str) -> bool:
    return _DECL_PATTERN.fullmatch(line) is not None


def is_simple_init_declaration_line(line: str) -> bool:
    return _INIT_DECL_PATTERN.fullmatch(line) is not None


def is_simple_assignment_line(line: str) -> bool:
    return (
        re.fullmatch(
            rf"[ \t]+{_SIMPLE_LVALUE}\s*=\s*[^;\n]+;\n?",
            line,
        )
        is not None
    )


def declaration_insert_line(lines: list[str]) -> tuple[int, str]:
    insert = 0
    indent = "    "
    saw_declaration = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if saw_declaration:
                break
            insert = i + 1
            continue
        if is_simple_declaration_line(line) or is_simple_init_declaration_line(line):
            indent = line[: len(line) - len(line.lstrip())] or indent
            insert = i + 1
            saw_declaration = True
            continue
        indent = line[: len(line) - len(line.lstrip())] or indent
        break
    return insert, indent


def mutate_declaration_movement(body: str) -> Iterator[Mutation]:
    for decl in _DECL_PATTERN.finditer(body):
        var = decl.group("var")
        assign_pattern = re.compile(
            rf"(?m)^(?P<indent>[ \t]+){re.escape(var)}\s*=\s*"
            rf"(?P<expr>[^;\n]+);"
        )
        for assign in assign_pattern.finditer(body, decl.end()):
            if has_identifier(body[decl.end() : assign.start()], var):
                continue
            replacement = (
                f"{assign.group('indent')}{decl.group('type')} "
                f"{var} = {assign.group('expr').strip()};"
            )
            new_body = body[: decl.start()]
            new_body += body[decl.end() : assign.start()]
            new_body += replacement
            new_body += body[assign.end() :]
            yield make_mutation(
                "v1b",
                "declaration-move",
                f"move {var} declaration to first assignment",
                new_body,
            )
            break


def mutate_declaration_widening(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    insert_line, insert_indent = declaration_insert_line(lines)
    insert_offset = offsets[insert_line] if insert_line < len(offsets) else len(body)

    for line_i, line in enumerate(lines):
        match = _INIT_DECL_PATTERN.fullmatch(line)
        if match is None:
            continue
        if line_i < insert_line:
            continue
        var = match.group("var")
        before = "".join(lines[:line_i])
        if has_identifier(before, var):
            continue
        expr = match.group("expr").strip()
        line_start = offsets[line_i]
        line_end = line_start + len(line)
        decl_line = f"{insert_indent}{match.group('type')} {var};\n"
        assign_line = f"{match.group('indent')}{var} = {expr};\n"
        if is_simple_assignment_line(assign_line) is False:
            continue
        new_body = body[:line_start] + assign_line + body[line_end:]
        adjusted_insert = insert_offset
        if line_start < insert_offset:
            adjusted_insert += len(assign_line) - len(line)
        new_body = new_body[:adjusted_insert] + decl_line + new_body[adjusted_insert:]
        yield make_mutation(
            "v1b",
            "declaration-widen",
            f"move initialized {var} declaration to top scope",
            new_body,
        )


def mutate_declaration_reorder(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    for i in range(len(lines) - 1):
        first = lines[i]
        second = lines[i + 1]
        first_decl = _DECL_PATTERN.fullmatch(first) or _INIT_DECL_PATTERN.fullmatch(first)
        second_decl = _DECL_PATTERN.fullmatch(second) or _INIT_DECL_PATTERN.fullmatch(second)
        if first_decl is None or second_decl is None:
            continue
        if first_decl.group("indent") != second_decl.group("indent"):
            continue
        first_var = first_decl.group("var")
        second_var = second_decl.group("var")
        if has_identifier(second, first_var) or has_identifier(first, second_var):
            continue
        start = offsets[i]
        end = offsets[i + 1] + len(second)
        yield make_mutation(
            "v1b",
            "declaration-reorder",
            f"swap adjacent declarations {first_var} and {second_var}",
            splice(body, start, end, second + first),
        )


def declaration_line_match(line: str) -> re.Match[str] | None:
    return _DECL_PATTERN.fullmatch(line) or _INIT_DECL_PATTERN.fullmatch(line)


_PROMOTE_DECL_PATTERN = re.compile(
    r"^(?P<indent>[ \t]+)"
    r"(?P<type>(?:(?:UNUSED|const|volatile|signed|unsigned|long|short)\s+)*"
    r"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s+[A-Za-z_]\w*)*(?:\s*\*)*)\s+"
    r"(?P<var>[A-Za-z_]\w*)"
    r"(?:\s*\[[^\]\n]+\])*"
    r"(?:\s*=\s*(?P<expr>[^;\n]+))?"
    r"\s*;[ \t]*(?://[^\n]*)?\n?$"
)
_PROMOTE_DECL_DISALLOWED_TYPES = {
    "case",
    "do",
    "else",
    "for",
    "goto",
    "if",
    "return",
    "sizeof",
    "switch",
    "while",
}


def promotion_declaration_line_match(line: str) -> re.Match[str] | None:
    match = declaration_line_match(line) or _PROMOTE_DECL_PATTERN.fullmatch(line)
    if match is None:
        return None
    first_type_word = match.group("type").strip().split()[0]
    if first_type_word in _PROMOTE_DECL_DISALLOWED_TYPES:
        return None
    return match


def declaration_depends_on_prior_vars(
    decl: re.Match[str], prior_decls: list[re.Match[str]]
) -> bool:
    expr = decl.groupdict().get("expr")
    if expr is None:
        return False
    return any(has_identifier(expr, prior.group("var")) for prior in prior_decls)


def is_declaration_section_gap(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("//")
        or stripped.startswith("/*")
        or stripped.startswith("*")
    )


def mutate_declaration_promote(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    i = 0
    while i < len(lines):
        first_decl = promotion_declaration_line_match(lines[i])
        if first_decl is None:
            i += 1
            continue
        indent = first_decl.group("indent")
        run: list[tuple[int, re.Match[str]]] = []
        while i < len(lines):
            decl = promotion_declaration_line_match(lines[i])
            if decl is not None and decl.group("indent") == indent:
                run.append((i, decl))
                i += 1
                continue
            if run and is_declaration_section_gap(lines[i]):
                i += 1
                continue
            if decl is not None and decl.group("indent") != indent:
                break
            break
        if len(run) < 2:
            continue
        first_line = run[0][0]
        section_lines = lines[first_line:i]
        for entry_idx, (line_idx, decl) in enumerate(run[1:], start=1):
            prior_decls = [prior for _, prior in run[:entry_idx]]
            if declaration_depends_on_prior_vars(decl, prior_decls):
                continue
            source_line = lines[line_idx]
            relative_line = line_idx - first_line
            replacement_lines = (
                [source_line]
                + section_lines[:relative_line]
                + section_lines[relative_line + 1 :]
            )
            start = offsets[first_line]
            end = offsets[i - 1] + len(lines[i - 1])
            yield make_mutation(
                "v1b",
                "declaration-promote",
                f"promote {decl.group('var')} to front of declaration block",
                splice(body, start, end, "".join(replacement_lines)),
            )


def mutate_alias_remove(body: str) -> Iterator[Mutation]:
    for decl in _DECL_PATTERN.finditer(body):
        alias = decl.group("var")
        if "*" not in decl.group("type"):
            continue
        assign_pattern = re.compile(
            rf"(?m)^(?P<indent>[ \t]+){re.escape(alias)}\s*=\s*&"
            rf"(?P<base>{_SIMPLE_LVALUE})\s*;\n?"
        )
        assigns = list(assign_pattern.finditer(body))
        if len(assigns) != 1:
            continue
        assign = assigns[0]
        if assign.start() < decl.end():
            continue
        if has_identifier(body[decl.end() : assign.start()], alias):
            continue
        tail = body[assign.end() :]
        if not re.search(rf"\b{re.escape(alias)}->", tail):
            continue
        base = assign.group("base")
        tail = re.sub(rf"\b{re.escape(alias)}->", f"{base}.", tail)
        new_body = body[: decl.start()] + body[decl.end() : assign.start()] + tail
        if has_identifier(new_body, alias):
            continue
        yield make_mutation(
            "v1b",
            "alias-remove",
            f"replace {alias}-> uses with {base}.",
            new_body,
        )


def declaration_insert_offset(body: str) -> tuple[int, str]:
    lines = body.splitlines(keepends=True)
    offset = 0
    insert = 0
    indent = "    "
    for line in lines:
        stripped = line.strip()
        if not stripped:
            offset += len(line)
            insert = offset
            continue
        line_indent = line[: len(line) - len(line.lstrip())]
        if re.match(r"(?:[A-Za-z_]\w*|struct\s+[A-Za-z_]\w*)", stripped):
            if stripped.endswith(";") and not stripped.startswith(("return", "goto")):
                indent = line_indent
                offset += len(line)
                insert = offset
                continue
        indent = line_indent or indent
        break
    return insert, indent


def mutate_alias_introduce_fp_x1a88(body: str) -> Iterator[Mutation]:
    target = "fp->x1A88."
    if body.count(target) < 2:
        return
    for alias in ("item_data", "x1A88_data", "data2"):
        if not has_identifier(body, alias):
            break
    else:
        return
    insert_at, indent = declaration_insert_offset(body)
    first_use = body.find(target, insert_at)
    if first_use < 0:
        return
    line_start = body.rfind("\n", 0, first_use) + 1
    declaration = f"{indent}struct Fighter_x1A88_t* {alias};\n"
    assignment = f"{indent}{alias} = &fp->x1A88;\n"
    body_with_decl = body[:insert_at] + declaration + body[insert_at:]
    line_start += len(declaration)
    body_with_alias = (
        body_with_decl[:line_start]
        + assignment
        + body_with_decl[line_start:].replace(target, f"{alias}->")
    )
    yield make_mutation(
        "v1b",
        "alias-introduce",
        f"introduce {alias} alias for fp->x1A88",
        body_with_alias,
    )


def mutate_abs_shape(body: str) -> Iterator[Mutation]:
    manual_pattern = re.compile(
        r"(?ms)^(?P<indent>[ \t]+)if\s*\(\s*(?P<expr>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)"
        r"\s*<\s*0\.0[fF]?\s*\)\s*\{\s*\n"
        r"(?P=indent)[ \t]+(?P<var>[A-Za-z_]\w*)\s*=\s*-\s*(?P=expr)\s*;\s*\n"
        r"(?P=indent)\}\s*else\s*\{\s*\n"
        r"(?P=indent)[ \t]+(?P=var)\s*=\s*(?P=expr)\s*;\s*\n"
        r"(?P=indent)\}"
    )
    for match in manual_pattern.finditer(body):
        replacement = f"{match.group('indent')}{match.group('var')} = ABS({match.group('expr')});"
        yield make_mutation(
            "v1b",
            "abs-shape",
            f"replace manual abs block for {match.group('expr')} with ABS",
            splice(body, match.start(), match.end(), replacement),
        )

    abs_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        r"ABS\((?P<expr>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\)\s*;"
    )
    for match in abs_pattern.finditer(body):
        indent = match.group("indent")
        inner = indent + "    "
        expr = match.group("expr")
        var = match.group("var")
        replacement = (
            f"{indent}if ({expr} < 0.0f) {{\n"
            f"{inner}{var} = -{expr};\n"
            f"{indent}}} else {{\n"
            f"{inner}{var} = {expr};\n"
            f"{indent}}}"
        )
        yield make_mutation(
            "v1b",
            "abs-shape",
            f"expand ABS({expr}) into manual abs block",
            splice(body, match.start(), match.end(), replacement),
        )


def split_top_level_args(args: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    i = 0
    while i < len(args):
        c = args[i]
        if c in "\"'":
            quote = c
            i += 1
            while i < len(args):
                if args[i] == "\\":
                    i += 2
                    continue
                if args[i] == quote:
                    break
                i += 1
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(args[start:i].strip())
            start = i + 1
        i += 1
    tail = args[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def mutate_clamp_branch_shape(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<macro>MIN|MAX)\((?P<args>[^;\n]+)\)"
        r"(?P<scale>\s*\*\s*(?P<scale_expr>[^;\n]+))?\s*;"
    )
    for match in pattern.finditer(body):
        args = split_top_level_args(match.group("args"))
        if len(args) != 2:
            continue
        expr, limit = args
        if not is_likely_pure_expr(expr) or not is_likely_pure_expr(limit):
            continue
        if match.group("scale_expr") is not None:
            if not is_likely_pure_expr(match.group("scale_expr")):
                continue
        if has_identifier(expr, match.group("var")):
            continue
        indent = match.group("indent")
        inner = indent + "    "
        cmp_op = ">" if match.group("macro") == "MIN" else "<"
        replacement = (
            f"{indent}{match.group('var')} = {expr};\n"
            f"{indent}if ({match.group('var')} {cmp_op} {limit}) {{\n"
            f"{inner}{match.group('var')} = {limit};\n"
            f"{indent}}}"
        )
        if match.group("scale_expr") is not None:
            replacement += f"\n{indent}{match.group('var')} *= {match.group('scale_expr').strip()};"
        yield make_mutation(
            "v1b",
            "clamp-branch-shape",
            f"expand {match.group('macro')} clamp for {match.group('var')}",
            splice(body, match.start(), match.end(), replacement),
        )


def parse_int_literal(raw: str) -> int | None:
    try:
        return int(raw, 0)
    except ValueError:
        return None


def format_int_like(original: str, value: int) -> str:
    if original.lower().startswith("0x"):
        return f"0x{value:X}"
    return str(value)


def mutate_local_pad_array_size(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<type>u8|s8|char)\s+"
        r"(?P<name>_pad[A-Za-z0-9_]*|pad[A-Za-z0-9_]*|unused[A-Za-z0-9_]*|_+)"
        r"\[(?P<size>0x[0-9A-Fa-f]+|\d+)\]\s*;\s*$"
    )
    for match in pattern.finditer(body):
        size = parse_int_literal(match.group("size"))
        if size is None:
            continue
        for delta in (-32, -28, -24, -20, -16, -12, -8, -4, 4, 8, 12, 16, 20, 24, 28, 32):
            new_size = size + delta
            if new_size <= 0:
                continue
            replacement = (
                f"{match.group('indent')}{match.group('type')} "
                f"{match.group('name')}[{format_int_like(match.group('size'), new_size)}];"
            )
            yield make_mutation(
                "v1b",
                "local-pad-array-size",
                f"change {match.group('name')} padding array from {size} to {new_size}",
                splice(body, match.start(), match.end(), replacement),
            )


def member_temp_name(expr: str, body: str) -> str:
    name = re.split(r"->|\.", expr)[-1]
    name = re.sub(r"\W+", "_", name).strip("_") or "temp"
    for candidate in (name, f"{name}_temp", "temp"):
        if not has_standalone_identifier(body, candidate):
            return candidate
    suffix = 2
    while has_standalone_identifier(body, f"{name}_{suffix}"):
        suffix += 1
    return f"{name}_{suffix}"


def mutate_call_repeated_float_temp(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        r"(?ms)^(?P<indent>[ \t]+)(?P<stmt>(?!if\b|for\b|while\b|switch\b|return\b)"
        r"[A-Za-z_]\w*\s*\([^;{}]*\)\s*;)"
    )
    member_pattern = re.compile(
        r"\b[A-Za-z_]\w*(?:(?:->|\.)[A-Za-z_]\w*){2,}\b"
    )
    for match in pattern.finditer(body):
        stmt = match.group("stmt")
        counts: dict[str, int] = {}
        for member in member_pattern.findall(stmt):
            counts[member] = counts.get(member, 0) + 1
        for expr, count in counts.items():
            if count < 2:
                continue
            var = member_temp_name(expr, body)
            indent = match.group("indent")
            inner = indent + "    "
            replaced_stmt = stmt.replace(expr, var)
            stmt_lines = replaced_stmt.splitlines(keepends=True)
            indented_stmt = "".join(
                f"{inner}{line}" if i == 0 else f"    {line}"
                for i, line in enumerate(stmt_lines)
            )
            replacement = (
                f"{indent}{{\n"
                f"{inner}float {var} = {expr};\n"
                f"{indented_stmt}\n"
                f"{indent}}}"
            )
            yield make_mutation(
                "v1b",
                "call-repeated-float-temp",
                f"introduce float temp {var} for repeated {expr} call argument",
                splice(body, match.start(), match.end(), replacement),
            )
            break


def mutate_vararg_base_arg(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<func>[A-Za-z_]\w*Report)\s*"
        r"\((?P<args>[^;\n]+)\)\s*;"
    )
    divisor = re.compile(
        rf"^(?P<base>{_SIMPLE_LVALUE})\s*/\s*(?:0x[0-9A-Fa-f]+|\d+)$"
    )
    for match in pattern.finditer(body):
        args = split_top_level_args(match.group("args"))
        if len(args) < 2:
            continue
        last = divisor.match(args[-1])
        if last is None:
            continue
        base = last.group("base")
        if any(arg == base for arg in args[:-1]):
            continue
        replacement_args = ", ".join([*args, base])
        replacement = f"{match.group('indent')}{match.group('func')}({replacement_args});"
        yield make_mutation(
            "v1b",
            "vararg-base-arg",
            f"append {base} to {match.group('func')} vararg call",
            splice(body, match.start(), match.end(), replacement),
        )


def mutate_assignment_in_condition(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*"
        r"(?P<expr>[^;\n]+);\n"
        r"(?P=indent)if\s*\((?P<cond>[^;\n{}]+)\)\s*\{"
    )
    for match in pattern.finditer(body):
        var = match.group("var")
        expr = match.group("expr").strip()
        cond = match.group("cond").strip()
        if not is_likely_pure_expr(expr):
            continue
        if not has_identifier(cond, var):
            continue
        assign_expr = f"({var} = {expr})"
        deref_pattern = re.compile(rf"(?<![\w.>])\*\s*{re.escape(var)}\b")
        if deref_pattern.search(cond):
            new_cond = deref_pattern.sub(f"*{assign_expr}", cond, count=1)
        else:
            new_cond = re.sub(
                rf"(?<![.>])\b{re.escape(var)}\b",
                assign_expr,
                cond,
                count=1,
            )
        if new_cond == cond:
            continue
        replacement = f"{match.group('indent')}if ({new_cond}) {{"
        yield make_mutation(
            "v1b",
            "assignment-in-condition",
            f"fold {var} assignment into following if condition",
            splice(body, match.start(), match.end(), replacement),
        )


def alias_name_for_global(global_name: str, body: str) -> str:
    base = global_name
    if "_" in base:
        base = base.rsplit("_", 1)[-1]
    base = re.sub(r"\W+", "_", base).strip("_").lower() or "global"
    if base[0].isdigit():
        base = f"global_{base}"
    candidates = (f"{base}_ptr", "global_ptr")
    for candidate in candidates:
        if not has_standalone_identifier(body, candidate):
            return candidate
    suffix = 2
    while has_standalone_identifier(body, f"{base}_ptr_{suffix}"):
        suffix += 1
    return f"{base}_ptr_{suffix}"


def available_struct_pointer_type(body: str) -> str | None:
    for match in re.finditer(
        r"(?m)^[ \t]+(?P<type>struct\s+[A-Za-z_]\w+)\s*\*\s*[A-Za-z_]\w*"
        r"(?:\s*=\s*[^;\n]+)?\s*;",
        body,
    ):
        return match.group("type")
    return None


def local_declared_names(body: str) -> set[str]:
    names: set[str] = set()
    pattern = re.compile(
        r"(?m)^[ \t]+(?:UNUSED\s+)?(?:(?:const|volatile|signed|unsigned|long|short)\s+)*"
        r"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s*\*)*\s+"
        r"(?P<name>[A-Za-z_]\w*)\s*(?:[=;\[,])"
    )
    for match in pattern.finditer(body):
        names.add(match.group("name"))
    return names


def mutate_global_struct_local_alias(body: str) -> Iterator[Mutation]:
    direct = re.compile(r"(?<![.>])\b(?P<global>[A-Za-z_]\w*)\.(?P<field>[A-Za-z_]\w*)")
    arrow = re.compile(
        r"\(\s*&\s*(?P<global>[A-Za-z_]\w*)\s*\)\s*->(?P<field>[A-Za-z_]\w*)"
    )
    uses: dict[str, int] = defaultdict(int)
    locals_in_body = local_declared_names(body)
    for match in direct.finditer(strip_noise(body)):
        global_name = match.group("global")
        if global_name in {"fp", "gp", "ip", "jobj", "data", "self"}:
            continue
        if global_name in locals_in_body:
            continue
        uses[global_name] += 1
    for match in arrow.finditer(strip_noise(body)):
        global_name = match.group("global")
        if global_name in locals_in_body:
            continue
        uses[global_name] += 1
    struct_type = available_struct_pointer_type(body)
    if struct_type is None:
        return
    insert_at, indent = declaration_insert_offset(body)
    for global_name, count in uses.items():
        if count < 2:
            continue
        alias = alias_name_for_global(global_name, body)
        declaration = f"{indent}{struct_type}* {alias} = &{global_name};\n"
        tail = body[insert_at:]
        tail = re.sub(
            rf"\(\s*&\s*{re.escape(global_name)}\s*\)\s*->",
            f"{alias}->",
            tail,
        )
        tail = re.sub(
            rf"(?<![.>])\b{re.escape(global_name)}\.",
            f"{alias}->",
            tail,
        )
        new_body = body[:insert_at] + declaration + tail
        yield make_mutation(
            "v1b",
            "global-struct-local-alias",
            f"introduce {alias} alias for repeated {global_name} field accesses",
            new_body,
        )


def declared_pointer_or_array_type(body: str, name: str) -> str | None:
    pointer = re.search(
        rf"(?m)^[ \t]+(?P<type>(?:(?:const|volatile)\s+)?"
        rf"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s*\*)*)\s+"
        rf"{re.escape(name)}\s*(?:=\s*[^;\n]+)?;",
        body,
    )
    if pointer is not None and "*" in pointer.group("type"):
        return pointer.group("type")
    array = re.search(
        rf"(?m)^[ \t]+(?P<type>(?:(?:const|volatile)\s+)?"
        rf"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s*\*)*)\s+"
        rf"{re.escape(name)}\s*\[[^\]\n]+\]",
        body,
    )
    if array is not None:
        return f"{array.group('type')}*"
    return None


def pointer_iter_name(array_name: str, body: str) -> str:
    for candidate in (f"{array_name}_p", f"{array_name}_ptr", "p"):
        if not has_standalone_identifier(body, candidate):
            return candidate
    suffix = 2
    while has_standalone_identifier(body, f"{array_name}_p{suffix}"):
        suffix += 1
    return f"{array_name}_p{suffix}"


def mutate_pointer_iteration_vs_index(body: str) -> Iterator[Mutation]:
    code = strip_noise(body)
    pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)for\s*\(\s*(?P<idx>[A-Za-z_]\w*)\s*=\s*0\s*;"
        r"\s*(?P=idx)\s*<\s*(?P<limit>[^;\n]+)\s*;\s*(?P=idx)\+\+\s*\)\s*\{"
    )
    for match in pattern.finditer(code):
        open_idx = match.end() - 1
        close_idx = find_matching(code, open_idx, "{", "}")
        if close_idx < 0:
            continue
        idx = match.group("idx")
        loop_body = body[open_idx + 1 : close_idx]
        arrays = []
        for array_match in re.finditer(
            rf"\b(?P<array>[A-Za-z_]\w*)\s*\[\s*{re.escape(idx)}\s*\]",
            strip_noise(loop_body),
        ):
            array_name = array_match.group("array")
            if array_name not in arrays:
                arrays.append(array_name)
        for array_name in arrays:
            ptr_type = declared_pointer_or_array_type(body[: match.start()], array_name)
            if ptr_type is None:
                continue
            ptr = pointer_iter_name(array_name, body)
            indent = match.group("indent")
            replacement_for = (
                f"{indent}{ptr_type} {ptr} = {array_name};\n"
                f"{indent}for ({idx} = 0; {idx} < {match.group('limit').strip()}; "
                f"{idx}++, {ptr}++) {{"
            )
            new_body = splice(body, match.start(), match.end(), replacement_for)
            delta = len(replacement_for) - (match.end() - match.start())
            shifted_close = close_idx + delta
            inner_start = match.start() + len(replacement_for)
            inner = new_body[inner_start:shifted_close]
            inner = re.sub(
                rf"\b{re.escape(array_name)}\s*\[\s*{re.escape(idx)}\s*\]",
                f"(*{ptr})",
                inner,
            )
            new_body = new_body[:inner_start] + inner + new_body[shifted_close:]
            yield make_mutation(
                "v1b",
                "pointer-iteration-vs-index",
                f"iterate {array_name} with pointer {ptr} in {idx} loop",
                new_body,
            )


def mutate_vec_scalar_triplet(body: str) -> Iterator[Mutation]:
    for match in re.finditer(r"(?m)^(?P<indent>[ \t]+)Vec3\s+(?P<var>[A-Za-z_]\w*)\s*;\n", body):
        var = match.group("var")
        tail = body[match.end() :]
        if re.search(rf"(?<![.>])\b{re.escape(var)}\b(?!\s*[.])", tail):
            continue
        if f"&{var}" in tail:
            continue
        if not all(has_identifier(tail, f"{var}.{axis}") for axis in ("x", "y", "z")):
            continue
        replacement = f"{match.group('indent')}float {var}_x, {var}_y, {var}_z;\n"
        new_body = body[: match.start()] + replacement + body[match.end() :]
        for axis in ("x", "y", "z"):
            new_body = re.sub(
                rf"\b{re.escape(var)}\.{axis}\b",
                f"{var}_{axis}",
                new_body,
            )
        yield make_mutation(
            "v1b",
            "vec-scalar-triplet",
            f"replace Vec3 {var} with scalar x/y/z locals",
            new_body,
        )

    scalar_line = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<type>float|f32)\s+"
        r"(?P<base>[A-Za-z_]\w*)_x\s*,\s*(?P=base)_y\s*,\s*(?P=base)_z\s*;\n"
    )
    for match in scalar_line.finditer(body):
        base = match.group("base")
        if any(f"&{base}_{axis}" in body[match.end() :] for axis in ("x", "y", "z")):
            continue
        replacement = f"{match.group('indent')}Vec3 {base};\n"
        new_body = body[: match.start()] + replacement + body[match.end() :]
        for axis in ("x", "y", "z"):
            new_body = re.sub(
                rf"\b{re.escape(base)}_{axis}\b",
                f"{base}.{axis}",
                new_body,
            )
        yield make_mutation(
            "v1b",
            "vec-scalar-triplet",
            f"replace scalar {base}_x/y/z locals with Vec3 {base}",
            new_body,
        )


def has_local_declaration(body: str, name: str) -> bool:
    return (
        re.search(
            rf"(?m)^[ \t]+(?:struct\s+)?[A-Za-z_]\w+(?:\s*\*)*\s+"
            rf"{re.escape(name)}(?:\s*[=;\[,])",
            body,
        )
        is not None
    )


def mutate_remove_local_copy(body: str) -> Iterator[Mutation]:
    init_decl = re.compile(
        r"(?m)^(?P<indent>[ \t]+)(?P<type>(?:(?:const|volatile|signed|unsigned|long|short)\s+)*"
        r"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s*\*)*)\s+"
        r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<base>[A-Za-z_]\w*)\s*;\n"
    )
    for match in init_decl.finditer(body):
        var = match.group("var")
        base = match.group("base")
        if var == base or has_local_declaration(body[: match.start()], base):
            continue
        tail = body[match.end() :]
        if not has_identifier(tail, var):
            continue
        new_tail = re.sub(rf"(?<![.>])\b{re.escape(var)}\b", base, tail)
        yield make_mutation(
            "v1b",
            "remove-local-copy",
            f"replace local copy {var} with {base}",
            body[: match.start()] + new_tail,
        )

    split_decl = _DECL_PATTERN
    for decl in split_decl.finditer(body):
        var = decl.group("var")
        assign = re.search(
            rf"(?m)^(?P<indent>[ \t]+){re.escape(var)}\s*=\s*"
            rf"(?P<base>[A-Za-z_]\w*)\s*;\n",
            body[decl.end() :],
        )
        if assign is None:
            continue
        assign_start = decl.end() + assign.start()
        assign_end = decl.end() + assign.end()
        base = assign.group("base")
        if var == base or has_local_declaration(body[:decl.start()], base):
            continue
        if has_identifier(body[decl.end() : assign_start], var):
            continue
        tail = body[assign_end:]
        if not has_identifier(tail, var):
            continue
        new_tail = re.sub(rf"(?<![.>])\b{re.escape(var)}\b", base, tail)
        yield make_mutation(
            "v1b",
            "remove-local-copy",
            f"remove delayed local copy {var} of {base}",
            body[: decl.start()] + body[decl.end() : assign_start] + new_tail,
        )


def mutate_inline_negated_clamp(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        rf"(?ms)^(?P<indent>[ \t]+)\{{\s*\n"
        rf"(?P=indent)[ \t]+(?P<type>f32|float)\s+(?P<bound>[A-Za-z_]\w*)\s*=\s*"
        rf"-\s*(?P<expr>[^;\n]+);\s*\n"
        rf"(?P=indent)[ \t]+if\s*\(\s*(?P<lhs>{_SIMPLE_LVALUE})\s*<\s*(?P=bound)\s*\)\s*\{{\s*\n"
        rf"(?P=indent)[ \t]+[ \t]+(?P=lhs)\s*=\s*(?P=bound)\s*;\s*\n"
        rf"(?P=indent)[ \t]+\}}\s*\n"
        rf"(?P=indent)\}}"
    )
    for match in pattern.finditer(body):
        expr = match.group("expr").strip()
        if not is_likely_pure_expr(expr):
            continue
        indent = match.group("indent")
        inner = indent + "    "
        lhs = match.group("lhs")
        replacement = (
            f"{indent}if ({lhs} < -{expr}) {{\n"
            f"{inner}{lhs} = -{expr};\n"
            f"{indent}}}"
        )
        yield make_mutation(
            "v1b",
            "inline-negated-clamp",
            f"inline negative clamp bound {match.group('bound')} for {lhs}",
            splice(body, match.start(), match.end(), replacement),
        )


_OPERAND_DECL_LINE = re.compile(
    r"^(?P<indent>[ \t]+)"
    r"(?P<type>(?:(?:UNUSED|const|volatile|signed|unsigned|long|short)\s+)*"
    r"(?:(?:struct\s+)?[A-Za-z_]\w+)(?:\s+[A-Za-z_]\w+)*(?:\s*\*)*)\s+"
    r"(?P<var>[A-Za-z_]\w*)"
    r"(?P<array>(?:\s*\[[^\]\n]+\])*)"
    r"(?:\s*=\s*(?P<expr>[^;\n]+))?"
    r"\s*;[ \t]*(?://[^\n]*)?\n?$"
)
_OPERAND_DISALLOWED_TYPE_WORDS = {
    "case",
    "default",
    "do",
    "else",
    "for",
    "goto",
    "if",
    "return",
    "sizeof",
    "switch",
    "while",
}
_OPERAND_SCALAR_TYPES = {
    "bool",
    "char",
    "double",
    "f32",
    "f64",
    "float",
    "ftmotionid",
    "int",
    "s8",
    "s16",
    "s32",
    "s64",
    "u8",
    "u16",
    "u32",
    "u64",
    "unsigned",
    "unsigned int",
}
_OPERAND_FLOAT_TYPES = {"double", "f32", "f64", "float"}


def operand_decl_line_match(line: str) -> re.Match[str] | None:
    match = _OPERAND_DECL_LINE.fullmatch(line)
    if match is None:
        return None
    first_word = match.group("type").strip().split()[0]
    if first_word in _OPERAND_DISALLOWED_TYPE_WORDS:
        return None
    return match


def operand_type_key(type_text: str) -> str:
    text = type_text.replace("*", " ")
    words = [
        word.lower()
        for word in text.split()
        if word not in {"UNUSED", "const", "volatile", "signed"}
    ]
    return " ".join(words)


def is_operand_scalar_type(type_text: str) -> bool:
    if "*" in type_text:
        return False
    return operand_type_key(type_text) in _OPERAND_SCALAR_TYPES


def is_operand_float_type(type_text: str) -> bool:
    if "*" in type_text:
        return False
    return operand_type_key(type_text) in _OPERAND_FLOAT_TYPES


def operand_unique_name(body: str, base: str) -> str:
    cleaned = re.sub(r"\W+", "_", base).strip("_") or "operand"
    candidates = [cleaned, f"{cleaned}_2", f"{cleaned}_3"]
    for candidate in candidates:
        if not has_standalone_identifier(body, candidate):
            return candidate
    suffix = 4
    while has_standalone_identifier(body, f"{cleaned}_{suffix}"):
        suffix += 1
    return f"{cleaned}_{suffix}"


def operand_line_indent(line: str, fallback: str = "    ") -> str:
    indent = line[: len(line) - len(line.lstrip())]
    return indent or fallback


def operand_is_statement_start(stripped: str) -> bool:
    if not stripped or stripped.startswith(("//", "/*", "*")):
        return False
    if stripped.startswith(("}", "else", "case ", "default:")):
        return False
    return (
        re.match(r"(?:if|for|while|switch|return)\b", stripped) is not None
        or re.match(r"(?:[A-Za-z_]\w*|\*|\()", stripped) is not None
    )


def operand_pressure_line_indexes(body: str, limit: int = 10) -> list[int]:
    lines, _ = body_lines(body)
    insert_line, _ = declaration_insert_line(lines)
    points: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if i < insert_line:
            continue
        stripped = line.strip()
        if not operand_is_statement_start(stripped):
            continue
        if operand_decl_line_match(line) is not None:
            continue
        score = 0
        if "&" in stripped:
            score += 4
        if any(
            token in stripped
            for token in (
                "ABS(",
                "atan2f",
                "efSync_Spawn",
                "Fighter_ChangeMotionState",
                "ftYs_SpecialS_8012F0DC",
                "ftCommon_",
                "ftColl_",
                "HSD_",
                "lb_8000",
            )
        ):
            score += 3
        if any(token in stripped for token in ("wall_hit", "coll_result", "env_flags")):
            score += 2
        if "(" in stripped and stripped.endswith(";"):
            score += 1
        if score:
            points.append((score, i))
    points.sort(key=lambda item: (-item[0], item[1]))
    return [i for _, i in points[:limit]]


def operand_simple_statement_indexes(body: str, limit: int = 10) -> list[int]:
    lines, _ = body_lines(body)
    indexes: list[int] = []
    for i in operand_pressure_line_indexes(body, limit=limit * 2):
        stripped = lines[i].strip()
        if not stripped.endswith(";"):
            continue
        if "{" in stripped or "}" in stripped:
            continue
        if operand_decl_line_match(lines[i]) is not None:
            continue
        if not operand_is_statement_start(stripped):
            continue
        indexes.append(i)
        if len(indexes) >= limit:
            break
    return indexes


def operand_expr_can_temp(expr: str) -> bool:
    stripped = strip_noise(expr).strip()
    if not stripped:
        return False
    if "++" in stripped or "--" in stripped:
        return False
    if _ASSIGNMENT_OPERATOR_RE.search(stripped):
        return False
    return True


def operand_local_type_map(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in body.splitlines(keepends=True):
        match = operand_decl_line_match(line)
        if match is None or match.group("array"):
            continue
        result.setdefault(match.group("var"), match.group("type").strip())
    return result


def operand_expr_type_hint(expr: str) -> str | None:
    stripped = strip_noise(expr).strip()
    if re.search(r"\b(?:atan2f|fabsf|sinf|cosf|ABS)\s*\(", stripped):
        return "f32"
    if re.search(r"(?<![\w.])(?:\d+\.\d*|\.\d+)(?:[fF])?\b", stripped):
        return "f32"
    if any(token in stripped for token in ("M_PI", "M_TAU", "facing_dir")):
        return "f32"
    if re.fullmatch(r"-?(?:0x[0-9A-Fa-f]+|\d+|true|false|NULL)", stripped):
        return "s32"
    if re.search(r"\s[&|^]\s|<<|>>", stripped):
        return "s32"
    return None


def operand_declaration_insert_offset(body: str) -> tuple[int, str]:
    lines, offsets = body_lines(body)
    insert_line, indent = declaration_insert_line(lines)
    insert_offset = offsets[insert_line] if insert_line < len(offsets) else len(body)
    return insert_offset, indent


def operand_statement_after_declaration_run(
    lines: list[str], offsets: list[int], line_i: int, indent: str
) -> int:
    insert_i = line_i + 1
    while insert_i < len(lines):
        stripped = lines[insert_i].strip()
        if not stripped or stripped.startswith(("//", "/*", "*")):
            insert_i += 1
            continue
        decl = operand_decl_line_match(lines[insert_i])
        if decl is not None and decl.group("indent") == indent:
            insert_i += 1
            continue
        break
    return offsets[insert_i] if insert_i < len(offsets) else sum(map(len, lines))


def mutate_operand_top_pressure(body: str) -> Iterator[Mutation]:
    insert_at, indent = operand_declaration_insert_offset(body)
    for size in (4, 8, 12, 16, 20, 24, 28, 32):
        name = operand_unique_name(body, "operand_pad")
        declaration = f"{indent}u8 {name}[{size}];\n"
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"add top-scope operand pad {name}[{size}]",
            splice(body, insert_at, insert_at, declaration),
        )

    for type_name, base in (("f32", "operand_f"), ("s32", "operand_i"), ("u32", "operand_u")):
        name = operand_unique_name(body, base)
        declaration = f"{indent}{type_name} {name};\n"
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"add top-scope {type_name} pressure local {name}",
            splice(body, insert_at, insert_at, declaration),
        )


def mutate_operand_scoped_pads(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    for line_i in operand_pressure_line_indexes(body, limit=10):
        line = lines[line_i]
        indent = operand_line_indent(line)
        for size in (4, 8, 12, 16, 24, 28, 32):
            name = operand_unique_name(body, "operand_pad")
            block = f"{indent}{{\n{indent}    u8 {name}[{size}];\n{indent}}}\n"
            yield make_mutation(
                "v1b",
                "operand-layout",
                f"insert scoped operand pad {name}[{size}] before line {line_i + 1}",
                splice(body, offsets[line_i], offsets[line_i], block),
            )


def mutate_operand_wrap_statement_pads(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    for line_i in operand_simple_statement_indexes(body, limit=8):
        line = lines[line_i]
        indent = operand_line_indent(line)
        inner = indent + "    "
        stmt = line.lstrip()
        if not stmt.endswith("\n"):
            stmt += "\n"
        for size in (4, 8, 16, 28):
            name = operand_unique_name(body, "operand_pad")
            replacement = (
                f"{indent}{{\n"
                f"{inner}u8 {name}[{size}];\n"
                f"{inner}{stmt}"
                f"{indent}}}\n"
            )
            yield make_mutation(
                "v1b",
                "operand-layout",
                f"wrap line {line_i + 1} with scoped operand pad {name}[{size}]",
                splice(body, offsets[line_i], offsets[line_i] + len(line), replacement),
            )


def mutate_operand_address_fake_uses(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    declarations: list[tuple[int, re.Match[str]]] = []
    for i, line in enumerate(lines):
        match = operand_decl_line_match(line)
        if match is not None and not match.group("array"):
            declarations.append((i, match))

    for line_i, decl in declarations[:12]:
        var = decl.group("var")
        if not re.search(rf"&\s*{re.escape(var)}\b", strip_noise(body)):
            continue
        decl_end = operand_statement_after_declaration_run(
            lines, offsets, line_i, decl.group("indent")
        )
        fake_after_decl = f"{decl.group('indent')}(void) &{var};\n"
        if fake_after_decl not in body[decl_end : decl_end + len(fake_after_decl) + 8]:
            yield make_mutation(
                "v1b",
                "operand-layout",
                f"fake address use of {var} after declaration run",
                splice(body, decl_end, decl_end, fake_after_decl),
            )

        first_addr_line = None
        for use_i in range(line_i + 1, len(lines)):
            if re.search(rf"&\s*{re.escape(var)}\b", strip_noise(lines[use_i])):
                first_addr_line = use_i
                break
        if first_addr_line is None:
            continue
        use_indent = operand_line_indent(lines[first_addr_line], decl.group("indent"))
        fake_near_use = f"{use_indent}(void) &{var};\n"
        line_start = offsets[first_addr_line]
        addr_decl = operand_decl_line_match(lines[first_addr_line])
        if addr_decl is None:
            yield make_mutation(
                "v1b",
                "operand-layout",
                f"fake address use of {var} before first address-taken statement",
                splice(body, line_start, line_start, fake_near_use),
            )
            line_end = line_start + len(lines[first_addr_line])
        else:
            line_end = operand_statement_after_declaration_run(
                lines, offsets, first_addr_line, addr_decl.group("indent")
            )
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"fake address use of {var} after first address-taken statement",
            splice(body, line_end, line_end, fake_near_use),
        )

    fake_use_pattern = re.compile(
        r"(?m)^(?P<indent>[ \t]+)\(void\)\s*&?\s*(?P<var>[A-Za-z_]\w*)\s*;\n"
    )
    locals_in_body = local_declared_names(body)
    for match in fake_use_pattern.finditer(body):
        if match.group("var") not in locals_in_body:
            continue
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"remove fake use of {match.group('var')}",
            splice(body, match.start(), match.end(), ""),
        )


def mutate_operand_narrow_address_locals(body: str) -> Iterator[Mutation]:
    lines, _ = body_lines(body)
    for decl_i, line in enumerate(lines):
        decl = operand_decl_line_match(line)
        if decl is None or decl.group("expr") is not None or decl.group("array"):
            continue
        var = decl.group("var")
        for use_i in range(decl_i + 1, len(lines)):
            if not re.search(rf"&\s*{re.escape(var)}\b", strip_noise(lines[use_i])):
                continue
            stripped = lines[use_i].strip()
            if not stripped.endswith(";") or "{" in stripped or "}" in stripped:
                break
            if has_identifier("".join(lines[decl_i + 1 : use_i]), var):
                break
            if has_identifier("".join(lines[use_i + 1 :]), var):
                break
            use_indent = operand_line_indent(lines[use_i], decl.group("indent"))
            inner = use_indent + "    "
            stmt = lines[use_i].lstrip()
            if not stmt.endswith("\n"):
                stmt += "\n"
            declaration = (
                f"{decl.group('type').strip()} {var}{decl.group('array')};"
            )
            replacement = (
                f"{use_indent}{{\n"
                f"{inner}{declaration}\n"
                f"{inner}{stmt}"
                f"{use_indent}}}\n"
            )
            new_lines = lines.copy()
            new_lines[decl_i] = ""
            new_lines[use_i] = replacement
            yield make_mutation(
                "v1b",
                "operand-layout",
                f"narrow address-taken local {var} to first address use",
                "".join(new_lines),
            )
            break


def mutate_operand_widen_inner_declarations(body: str) -> Iterator[Mutation]:
    lines, _ = body_lines(body)
    insert_line, insert_indent = declaration_insert_line(lines)
    emitted = 0
    for line_i, line in enumerate(lines):
        if line_i <= insert_line:
            continue
        decl = operand_decl_line_match(line)
        if decl is None or decl.group("array"):
            continue
        if decl.group("indent") == insert_indent:
            continue
        var = decl.group("var")
        if has_identifier("".join(lines[:line_i]), var):
            continue
        type_text = decl.group("type").strip()
        if "const" in type_text.split():
            continue
        new_lines = lines.copy()
        top_decl = f"{insert_indent}{type_text} {var};\n"
        expr = decl.group("expr")
        if expr is None:
            new_lines[line_i] = ""
            description = f"widen inner local {var} to top declaration section"
        else:
            new_lines[line_i] = f"{decl.group('indent')}{var} = {expr.strip()};\n"
            description = f"split and widen initialized inner local {var}"
        new_lines.insert(insert_line, top_decl)
        yield make_mutation(
            "v1b",
            "operand-layout",
            description,
            "".join(new_lines),
        )
        emitted += 1
        if emitted >= 12:
            break


def mutate_operand_pointer_aliases(body: str) -> Iterator[Mutation]:
    alias_pattern = re.compile(
        rf"(?m)^(?P<indent>[ \t]+)"
        rf"(?P<type>(?:(?:const|volatile)\s+)?"
        rf"(?:(?:struct\s+)?[A-Za-z_]\w*)(?:\s*\*)+)\s+"
        rf"(?P<var>[A-Za-z_]\w*)\s*=\s*&(?P<base>{_SIMPLE_LVALUE})\s*;\n"
    )
    for match in alias_pattern.finditer(body):
        var = match.group("var")
        base = match.group("base")
        tail = body[match.end() :]
        if not re.search(rf"\b{re.escape(var)}\s*->", strip_noise(tail)):
            continue
        if re.search(rf"(?<![.>])\b{re.escape(var)}\b(?!\s*->)", strip_noise(tail)):
            continue

        def replace_arrow(field_match: re.Match[str]) -> str:
            return f"{base}.{field_match.group('field')}"

        new_tail = re.sub(
            rf"\b{re.escape(var)}\s*->\s*(?P<field>[A-Za-z_]\w*)",
            replace_arrow,
            tail,
        )
        if has_identifier(new_tail, var):
            continue
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"remove pointer alias {var} for {base}",
            body[: match.start()] + new_tail,
        )

    fp_names = [
        match.group("var")
        for match in re.finditer(
            r"(?m)^[ \t]+Fighter\s*\*\s*(?P<var>[A-Za-z_]\w*)\s*="
            r"\s*GET_FIGHTER\([^;\n]+\);\n",
            body,
        )
    ]
    known_fields = (
        ("coll_data", "CollData", "coll_data"),
        ("cur_pos", "Vec3", "cur_pos"),
        ("self_vel", "Vec3", "self_vel"),
        ("x74_anim_vel", "Vec3", "anim_vel"),
    )
    insert_at, indent = operand_declaration_insert_offset(body)
    for fp_name in fp_names[:2]:
        for field, type_name, base_alias in known_fields:
            chain = f"{fp_name}->{field}"
            tail = body[insert_at:]
            if tail.count(chain) < 2:
                continue
            alias = operand_unique_name(body, base_alias)
            declaration = f"{indent}{type_name}* {alias} = &{chain};\n"
            replaced_tail = re.sub(rf"&\s*{re.escape(chain)}\b", alias, tail)
            replaced_tail = re.sub(
                rf"\b{re.escape(chain)}\.",
                f"{alias}->",
                replaced_tail,
            )
            if replaced_tail == tail:
                continue
            yield make_mutation(
                "v1b",
                "operand-layout",
                f"introduce {alias} alias for repeated {chain}",
                body[:insert_at] + declaration + replaced_tail,
            )


def mutate_operand_scalar_temps(body: str) -> Iterator[Mutation]:
    local_types = operand_local_type_map(body)
    lines, offsets = body_lines(body)
    assignment = re.compile(
        r"^(?P<indent>[ \t]+)(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<expr>[^;\n]+);\n?$"
    )
    emitted = 0
    for line_i, line in enumerate(lines):
        match = assignment.fullmatch(line)
        if match is None:
            continue
        var = match.group("var")
        type_text = local_types.get(var)
        if type_text is None or not is_operand_scalar_type(type_text):
            continue
        expr = match.group("expr").strip()
        if not operand_expr_can_temp(expr):
            continue
        if re.fullmatch(r"[A-Za-z_]\w*", expr):
            continue
        temp = operand_unique_name(body, f"{var}_operand")
        inner = match.group("indent") + "    "
        replacement = (
            f"{match.group('indent')}{{\n"
            f"{inner}{type_text} {temp} = {expr};\n"
            f"{inner}{var} = {temp};\n"
            f"{match.group('indent')}}}\n"
        )
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"split {var} assignment through scalar temp {temp}",
            splice(body, offsets[line_i], offsets[line_i] + len(line), replacement),
        )
        emitted += 1
        if emitted >= 20:
            break

    emitted = 0
    for line_i, line in enumerate(lines):
        decl = operand_decl_line_match(line)
        if decl is None or decl.group("expr") is None or decl.group("array"):
            continue
        type_text = decl.group("type").strip()
        if not is_operand_scalar_type(type_text):
            continue
        expr = decl.group("expr").strip()
        if not operand_expr_can_temp(expr):
            continue
        var = decl.group("var")
        temp = operand_unique_name(body, f"{var}_operand")
        replacement = (
            f"{decl.group('indent')}{type_text} {temp} = {expr};\n"
            f"{decl.group('indent')}{type_text} {var} = {temp};\n"
        )
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"initialize {var} through scalar temp {temp}",
            splice(body, offsets[line_i], offsets[line_i] + len(line), replacement),
        )
        emitted += 1
        if emitted >= 16:
            break


def mutate_operand_field_temps(body: str) -> Iterator[Mutation]:
    pattern = re.compile(
        rf"(?m)^(?P<indent>[ \t]+)(?P<lhs>{_SIMPLE_LVALUE})\s*=\s*"
        r"(?P<expr>[^;\n]+);\n?"
    )
    emitted = 0
    for match in pattern.finditer(body):
        lhs = match.group("lhs")
        if "->" not in lhs and "." not in lhs and "[" not in lhs:
            continue
        expr = match.group("expr").strip()
        if not operand_expr_can_temp(expr):
            continue
        hint = operand_expr_type_hint(expr)
        if hint is None:
            continue
        temp = operand_unique_name(
            body, "operand_f" if hint == "f32" else "operand_i"
        )
        inner = match.group("indent") + "    "
        replacement = (
            f"{match.group('indent')}{{\n"
            f"{inner}{hint} {temp} = {expr};\n"
            f"{inner}{lhs} = {temp};\n"
            f"{match.group('indent')}}}\n"
        )
        yield make_mutation(
            "v1b",
            "operand-layout",
            f"store {lhs} through {hint} temp {temp}",
            splice(body, match.start(), match.end(), replacement),
        )
        emitted += 1
        if emitted >= 24:
            break


def mutate_operand_float_reloads(body: str) -> Iterator[Mutation]:
    local_types = operand_local_type_map(body)
    lines, offsets = body_lines(body)
    emitted = 0
    for line_i, line in enumerate(lines):
        for var, type_text in local_types.items():
            if not is_operand_float_type(type_text):
                continue
            if not has_identifier(line, var):
                continue
            if re.match(rf"^[ \t]+{re.escape(var)}\s*=", line):
                continue
            stripped = line.strip()
            if not stripped.endswith(";") or "{" in stripped or "}" in stripped:
                continue
            temp = operand_unique_name(body, f"{var}_reload")
            indent = operand_line_indent(line)
            replaced_line = re.sub(
                rf"(?<![.>])\b{re.escape(var)}\b",
                temp,
                line,
                count=1,
            )
            if replaced_line == line:
                continue
            inner = indent + "    "
            replacement = (
                f"{indent}{{\n"
                f"{inner}{type_text} {temp} = {var};\n"
                f"{inner}{replaced_line.lstrip()}"
                f"{indent}}}\n"
            )
            yield make_mutation(
                "v1b",
                "operand-layout",
                f"reload float local {var} through temp {temp}",
                splice(body, offsets[line_i], offsets[line_i] + len(line), replacement),
            )
            emitted += 1
            break
        if emitted >= 16:
            break


_STACK_LAYOUT_PAD_ARRAY_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]+)(?P<type>u8|s8|char)\s+"
    r"(?P<name>_pad[A-Za-z0-9_]*|pad[A-Za-z0-9_]*|unused[A-Za-z0-9_]*|_+)"
    r"\[(?P<size>0x[0-9A-Fa-f]+|\d+)\]\s*;\s*$"
)


def stack_layout_has_signal(body: str) -> bool:
    code = strip_noise(body)
    return (
        "PAD_STACK" in code
        or _STACK_LAYOUT_PAD_ARRAY_RE.search(code) is not None
        or re.search(r"&\s*[A-Za-z_]\w*\b", code) is not None
    )


def stack_layout_is_pad_decl(decl: re.Match[str]) -> bool:
    if not decl.group("array"):
        return False
    type_key = operand_type_key(decl.group("type"))
    if type_key not in {"u8", "s8", "char"}:
        return False
    return re.fullmatch(
        r"_pad[A-Za-z0-9_]*|pad[A-Za-z0-9_]*|unused[A-Za-z0-9_]*|_+",
        decl.group("var"),
    ) is not None


def stack_layout_is_scalar_decl(decl: re.Match[str]) -> bool:
    if decl.group("array") or decl.group("expr") is not None:
        return False
    return is_operand_scalar_type(decl.group("type"))


_EXISTING_PAD_DECL_ORDER_NAME_RE = re.compile(
    r"_pad[A-Za-z0-9_]*|pad[A-Za-z0-9_]*|unused[A-Za-z0-9_]*|"
    r"operand_pad[A-Za-z0-9_]*|_+"
)


def existing_pad_decl_order_is_pad_decl(decl: re.Match[str]) -> bool:
    if not decl.group("array"):
        return False
    type_key = operand_type_key(decl.group("type"))
    if type_key not in {"u8", "s8", "char"}:
        return False
    return _EXISTING_PAD_DECL_ORDER_NAME_RE.fullmatch(decl.group("var")) is not None


def mutate_stack_layout_top_pads(body: str) -> Iterator[Mutation]:
    if not stack_layout_has_signal(body):
        return
    insert_at, indent = operand_declaration_insert_offset(body)
    for size in (4, 8, 12, 16, 20, 24, 28, 32):
        name = operand_unique_name(body, "operand_pad")
        declaration = f"{indent}u8 {name}[{size}];\n"
        yield make_mutation(
            "v1b",
            "stack-layout-pad",
            f"add top-scope stack pad {name}[{size}]",
            splice(body, insert_at, insert_at, declaration),
        )


def mutate_stack_layout_pad_sizes(body: str) -> Iterator[Mutation]:
    for match in _STACK_LAYOUT_PAD_ARRAY_RE.finditer(body):
        size = parse_int_literal(match.group("size"))
        if size is None:
            continue
        for delta in (-16, -12, -8, -4, 4, 8, 12, 16):
            new_size = size + delta
            if new_size <= 0:
                continue
            replacement = (
                f"{match.group('indent')}{match.group('type')} "
                f"{match.group('name')}[{format_int_like(match.group('size'), new_size)}];"
            )
            yield make_mutation(
                "v1b",
                "stack-layout-pad",
                f"change stack pad {match.group('name')} from {size} to {new_size}",
                splice(body, match.start(), match.end(), replacement),
            )


def mutate_stack_layout_pad_declaration_order(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    emitted = 0
    i = 0
    while i < len(lines):
        first_decl = operand_decl_line_match(lines[i])
        if first_decl is None:
            i += 1
            continue
        indent = first_decl.group("indent")
        section_start = i
        run: list[tuple[int, re.Match[str]]] = []
        while i < len(lines):
            decl = operand_decl_line_match(lines[i])
            if decl is not None and decl.group("indent") == indent:
                run.append((i, decl))
                i += 1
                continue
            if run and is_declaration_section_gap(lines[i]):
                i += 1
                continue
            break
        section_end = i
        if len(run) < 2 or not any(stack_layout_is_pad_decl(decl) for _, decl in run):
            continue

        for entry_idx, (line_idx, decl) in enumerate(run):
            if not stack_layout_is_scalar_decl(decl):
                continue
            preceding_pads = [
                pad_line_idx
                for pad_line_idx, pad_decl in run[:entry_idx]
                if stack_layout_is_pad_decl(pad_decl)
            ]
            if not preceding_pads:
                continue

            target_line_idx = preceding_pads[0]
            section_lines = lines[section_start:section_end]
            source_rel = line_idx - section_start
            target_rel = target_line_idx - section_start
            moved_line = section_lines.pop(source_rel)
            section_lines.insert(target_rel, moved_line)
            start = offsets[section_start]
            end = offsets[section_end - 1] + len(lines[section_end - 1])
            yield make_mutation(
                "v1b",
                "stack-layout-pad",
                f"move scalar {decl.group('var')} before stack pad declaration",
                splice(body, start, end, "".join(section_lines)),
            )
            emitted += 1
            if emitted >= 16:
                return


def mutate_stack_layout_pad(body: str) -> Iterator[Mutation]:
    yield from mutate_stack_layout_top_pads(body)
    yield from mutate_stack_layout_pad_declaration_order(body)
    yield from mutate_stack_layout_pad_sizes(body)


def mutate_existing_pad_decl_order(body: str) -> Iterator[Mutation]:
    lines, offsets = body_lines(body)
    emitted = 0
    i = 0
    while i < len(lines):
        first_decl = operand_decl_line_match(lines[i])
        if first_decl is None:
            i += 1
            continue
        indent = first_decl.group("indent")
        section_start = i
        run: list[tuple[int, re.Match[str]]] = []
        while i < len(lines):
            decl = operand_decl_line_match(lines[i])
            if decl is not None and decl.group("indent") == indent:
                run.append((i, decl))
                i += 1
                continue
            if run and is_declaration_section_gap(lines[i]):
                i += 1
                continue
            break
        section_end = i
        if len(run) < 2:
            continue

        for entry_idx, (line_idx, decl) in enumerate(run):
            if not existing_pad_decl_order_is_pad_decl(decl):
                continue
            prior_decls = run[:entry_idx]
            if not prior_decls:
                continue

            target_line_indices: list[int] = []
            front_line_idx = run[0][0]
            if front_line_idx < line_idx:
                target_line_indices.append(front_line_idx)
            for target_line_idx, _target_decl in reversed(prior_decls[-6:]):
                if target_line_idx not in target_line_indices:
                    target_line_indices.append(target_line_idx)

            for target_line_idx in target_line_indices:
                section_lines = list(lines[section_start:section_end])
                source_rel = line_idx - section_start
                target_rel = target_line_idx - section_start
                moved_line = section_lines.pop(source_rel)
                section_lines.insert(target_rel, moved_line)
                start = offsets[section_start]
                end = offsets[section_end - 1] + len(lines[section_end - 1])
                yield make_mutation(
                    "v1b",
                    "existing-pad-decl-order",
                    f"move existing pad {decl.group('var')} earlier in declaration block",
                    splice(body, start, end, "".join(section_lines)),
                )
                emitted += 1
                if emitted >= 24:
                    return


def mutate_operand_layout(body: str) -> Iterator[Mutation]:
    yield from mutate_operand_top_pressure(body)
    yield from mutate_operand_narrow_address_locals(body)
    yield from mutate_operand_widen_inner_declarations(body)
    yield from mutate_operand_address_fake_uses(body)
    yield from mutate_operand_pointer_aliases(body)
    yield from mutate_operand_scalar_temps(body)
    yield from mutate_operand_field_temps(body)
    yield from mutate_operand_float_reloads(body)
    yield from mutate_operand_scoped_pads(body)
    yield from mutate_operand_wrap_statement_pads(body)


def enabled_tiers(mutator_tier: str) -> set[str]:
    if mutator_tier == "v1a":
        return {"v1a"}
    return {"v1a", "v1b"}


def iter_candidate_mutations(
    body: str,
    enabled: set[str],
    mutator_filter: set[str] | None,
) -> Iterator[Mutation]:
    base_mutations = [
        mutation
        for mutation in iter_mutations(body, enabled)
        if mutator_filter is None or mutation.mutator in mutator_filter
    ]
    balanced_base_mutations = list(balance_mutations(base_mutations))
    yield from balanced_base_mutations

    if "v1b" not in enabled:
        return
    for first in balanced_base_mutations:
        if first.mutator in SINGLE_STEP_MUTATORS:
            continue
        for second in iter_mutations(first.body, enabled):
            if mutator_filter is not None and second.mutator not in mutator_filter:
                continue
            if second.mutator in SINGLE_STEP_MUTATORS:
                continue
            if second.body == first.body:
                continue
            yield make_mutation(
                second.tier,
                f"{first.mutator}+{second.mutator}",
                f"{first.description}; then {second.description}",
                second.body,
            )


def balance_mutations(mutations: Iterable[Mutation]) -> Iterator[Mutation]:
    buckets: dict[str, list[Mutation]] = defaultdict(list)
    order: list[str] = []
    for mutation in mutations:
        key = mutation.mutator.split("+", 1)[0]
        if key not in buckets:
            order.append(key)
        buckets[key].append(mutation)

    index = 0
    while True:
        emitted = False
        for key in order:
            bucket = buckets[key]
            if index >= len(bucket):
                continue
            yield bucket[index]
            emitted = True
        if not emitted:
            break
        index += 1


def generate_candidates(
    source_text: str,
    source_rel: str,
    functions: Iterable[str],
    max_candidates: int,
    mutator_tier: str,
    mutator_filter: set[str] | None = None,
) -> list[Candidate]:
    tiers = enabled_tiers(mutator_tier)
    spans = [find_function_span(source_text, name) for name in functions]
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for span in spans:
        body = source_text[span.body_open + 1 : span.body_close]
        for mutation in iter_candidate_mutations(body, tiers, mutator_filter):
            new_source = replace_function_body(source_text, span, mutation.body)
            if new_source == source_text:
                continue
            digest = hashlib.sha256(new_source.encode("utf-8")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            candidate_id = len(candidates) + 1
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    function=span.name,
                    tier=mutation.tier,
                    mutator=mutation.mutator,
                    description=mutation.description,
                    source_text=new_source,
                    patch_text=unified_patch(source_rel, source_text, new_source),
                )
            )
            if len(candidates) >= max_candidates:
                return candidates
    return candidates


def find_symbol(side: dict[str, Any], name: str) -> dict[str, Any] | None:
    for symbol in side.get("symbols", []):
        if symbol.get("name") == name:
            return symbol
    return None


def symbol_instructions(symbol: dict[str, Any] | None) -> list[str]:
    if symbol is None:
        return []
    return [
        inst.get("instruction", {}).get("formatted", "")
        for inst in symbol.get("instructions", [])
        if inst.get("instruction", {}).get("formatted")
    ]


def score_diff(diff: dict[str, Any], function: str) -> DiffScore:
    left = find_symbol(diff.get("left", {}), function)
    right = find_symbol(diff.get("right", {}), function)
    left_instructions = symbol_instructions(left)
    right_instructions = symbol_instructions(right)
    if not left_instructions and not right_instructions:
        diff_score = None
    else:
        ratio = difflib.SequenceMatcher(
            a=left_instructions, b=right_instructions, autojunk=False
        ).ratio()
        diff_score = 1.0 - ratio
    match_percent = None
    for symbol in (right, left):
        if symbol is not None and symbol.get("match_percent") is not None:
            match_percent = float(symbol["match_percent"])
            break
    return DiffScore(
        match_percent=match_percent,
        instruction_diff_score=diff_score,
        target_instruction_count=len(left_instructions),
        source_instruction_count=len(right_instructions),
    )


def compact_diff(diff: dict[str, Any], function: str, score: DiffScore) -> dict[str, Any]:
    """Keep the function-level objdiff data needed for candidate inspection."""
    return {
        "function": function,
        "score": asdict(score),
        "left": {"symbol": find_symbol(diff.get("left", {}), function)},
        "right": {"symbol": find_symbol(diff.get("right", {}), function)},
    }


def run_ninja(project: Path, target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ninja", target],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_objdiff(
    project: Path, objdiff_cli: Path, unit: str, function: str
) -> dict[str, Any]:
    proc = subprocess.run(
        [
            str(objdiff_cli),
            "diff",
            "-p",
            str(project),
            "-u",
            unit,
            function,
            "--format",
            "json",
            "-o",
            "-",
        ],
        cwd=project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, proc.args, proc.stdout, proc.stderr
        )
    return json.loads(proc.stdout)


def candidate_sort_key(result: CandidateResult) -> tuple[bool, float, float, float, int]:
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
        -delta,
        -match,
        diff_score,
        result.candidate_id,
    )


def prepare_output_dir(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates_dir = output_dir / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    for path in candidates_dir.glob("cand-*"):
        if path.is_file():
            path.unlink()
    for path in (output_dir / "summary.json", output_dir / "summary.md", output_dir / "best.patch"):
        if path.exists():
            path.unlink()
    return candidates_dir


def write_summary(
    output_dir: Path,
    source_rel: str,
    unit: UnitInfo | None,
    candidates: list[Candidate],
    results: list[CandidateResult],
    baseline: dict[str, DiffScore],
    report_functions: dict[str, ReportFunction],
    args: argparse.Namespace,
) -> None:
    ranked = sorted(results, key=candidate_sort_key)
    payload = {
        "source_path": source_rel,
        "unit": asdict(unit) if unit is not None else None,
        "eval": bool(args.eval),
        "mutator_tier": args.mutator_tier,
        "mutators": sorted(args.mutator_filter) if args.mutator_filter else None,
        "generated_candidates": len(candidates),
        "baseline": {name: asdict(score) for name, score in baseline.items()},
        "report_functions": {
            name: asdict(function) for name, function in report_functions.items()
        },
        "results": [asdict(result) for result in ranked],
    }
    with (output_dir / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    lines = [
        "# Decomp candidate results",
        "",
        f"- Source: `{source_rel}`",
        f"- Unit: `{unit.name if unit is not None else 'n/a'}`",
        f"- Mutator tier: `{args.mutator_tier}`",
        f"- Mutators: `{', '.join(sorted(args.mutator_filter)) if args.mutator_filter else 'all'}`",
        f"- Generated candidates: {len(candidates)}",
    ]
    if report_functions:
        lines.extend(["", "## Baseline report", ""])
        for name, function in sorted(report_functions.items()):
            fuzzy = (
                f"{function.fuzzy_match_percent:.5f}%"
                if function.fuzzy_match_percent is not None
                else "n/a"
            )
            size = function.size if function.size is not None else "n/a"
            address = function.virtual_address or "n/a"
            lines.append(
                f"- `{name}`: fuzzy `{fuzzy}`, size `{size}`, address `{address}`"
            )
    if baseline:
        lines.extend(["", "## Live objdiff baseline", ""])
        for name, score in sorted(baseline.items()):
            match = (
                f"{score.match_percent:.5f}%"
                if score.match_percent is not None
                else "n/a"
            )
            diff_score = (
                f"{score.instruction_diff_score:.5f}"
                if score.instruction_diff_score is not None
                else "n/a"
            )
            lines.append(
                f"- `{name}`: match `{match}`, diff score `{diff_score}`, "
                f"instructions `{score.source_instruction_count}/{score.target_instruction_count}`"
            )
    lines.extend(
        [
            "",
            "| Rank | Candidate | Function | Tier | Mutator | Status | Match | Delta | Diff score | Patch |",
            "| ---: | ---: | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for rank, result in enumerate(ranked, start=1):
        match = (
            f"{result.candidate_match_percent:.5f}"
            if result.candidate_match_percent is not None
            else ""
        )
        delta = f"{result.match_delta:+.5f}" if result.match_delta is not None else ""
        diff_score = (
            f"{result.instruction_diff_score:.5f}"
            if result.instruction_diff_score is not None
            else ""
        )
        lines.append(
            f"| {rank} | {result.candidate_id} | `{result.function}` | "
            f"`{result.tier}` | `{result.mutator}` | {result.status} | "
            f"{match} | {delta} | {diff_score} | `{result.patch_path}` |"
        )
    lines.append("")
    with (output_dir / "summary.md").open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    best = next((result for result in ranked if result.match_delta and result.match_delta > 0), None)
    if best is not None:
        patch_text = (output_dir / best.patch_path).read_text(encoding="utf-8")
        (output_dir / "best.patch").write_text(patch_text, encoding="utf-8")


def evaluate_candidates(
    project: Path,
    source_path: Path,
    source_rel: str,
    unit: UnitInfo | None,
    objdiff_cli: Path,
    candidates: list[Candidate],
    args: argparse.Namespace,
) -> tuple[list[CandidateResult], dict[str, DiffScore]]:
    output_dir = args.output_dir
    candidates_dir = prepare_output_dir(output_dir)
    original = source_path.read_text(encoding="utf-8")
    baseline: dict[str, DiffScore] = {}
    results: list[CandidateResult] = []

    if args.eval and unit is None:
        raise ValueError("--eval requires the source path to exist in objdiff.json")

    if args.eval and args.jobs != 1:
        print("warning: --jobs is accepted in V1 but evaluation is serial", file=sys.stderr)

    try:
        if args.eval and unit is not None:
            build_proc = run_ninja(project, unit.base_path)
            if build_proc.returncode != 0:
                raise RuntimeError(
                    f"baseline build failed for {unit.base_path}\n{build_proc.stderr}"
                )
            for function in sorted({candidate.function for candidate in candidates}):
                baseline[function] = score_diff(
                    run_objdiff(project, objdiff_cli, unit.name, function), function
                )

        for candidate in candidates:
            patch_path = candidates_dir / f"cand-{candidate.candidate_id:06d}.patch"
            patch_path.write_text(candidate.patch_text, encoding="utf-8")
            c_path = None
            if args.keep_c_files:
                c_path = candidates_dir / f"cand-{candidate.candidate_id:06d}.c"
                c_path.write_text(candidate.source_text, encoding="utf-8")

            result = CandidateResult(
                candidate_id=candidate.candidate_id,
                function=candidate.function,
                tier=candidate.tier,
                mutator=candidate.mutator,
                description=candidate.description,
                status="generated",
                patch_path=str(patch_path.relative_to(output_dir)),
                c_path=str(c_path.relative_to(output_dir)) if c_path is not None else None,
            )

            if not args.eval:
                results.append(result)
                continue

            source_path.write_text(candidate.source_text, encoding="utf-8")
            build_proc = run_ninja(project, unit.base_path if unit is not None else "")
            result.stdout = build_proc.stdout
            result.stderr = build_proc.stderr
            if build_proc.returncode != 0:
                result.status = "compile_failed"
                result.error = build_proc.stderr.strip() or build_proc.stdout.strip()
                results.append(result)
                source_path.write_text(original, encoding="utf-8")
                continue

            try:
                diff = run_objdiff(
                    project, objdiff_cli, unit.name if unit is not None else "", candidate.function
                )
            except subprocess.CalledProcessError as exc:
                result.status = "diff_failed"
                result.error = (exc.stderr or exc.stdout or str(exc)).strip()
                results.append(result)
                source_path.write_text(original, encoding="utf-8")
                continue

            score = score_diff(diff, candidate.function)
            diff_path = candidates_dir / f"cand-{candidate.candidate_id:06d}.diff.json"
            diff_path.write_text(
                json.dumps(compact_diff(diff, candidate.function, score), indent=2) + "\n",
                encoding="utf-8",
            )
            base_score = baseline.get(candidate.function)
            result.status = "ok"
            result.diff_path = str(diff_path.relative_to(output_dir))
            result.baseline_match_percent = (
                base_score.match_percent if base_score is not None else None
            )
            result.candidate_match_percent = score.match_percent
            result.match_delta = (
                score.match_percent - base_score.match_percent
                if score.match_percent is not None
                and base_score is not None
                and base_score.match_percent is not None
                else None
            )
            result.instruction_diff_score = score.instruction_diff_score
            result.target_instruction_count = score.target_instruction_count
            result.source_instruction_count = score.source_instruction_count
            results.append(result)

            source_path.write_text(original, encoding="utf-8")
            if args.stop_on_perfect and score.match_percent is not None:
                if score.match_percent >= 100.0:
                    break
    finally:
        source_path.write_text(original, encoding="utf-8")
        if args.eval and unit is not None:
            restore_proc = run_ninja(project, unit.base_path)
            if restore_proc.returncode != 0:
                print(
                    f"warning: failed to rebuild original object {unit.base_path}\n"
                    f"{restore_proc.stderr.strip() or restore_proc.stdout.strip()}",
                    file=sys.stderr,
                )

    return results, baseline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and rank C source-shape candidates for decomp matching.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    parser.add_argument(
        "--list-mutators",
        action="store_true",
        help="Print available mutators and exit.",
    )
    parser.add_argument("--path", type=Path, help="C source file to mutate.")
    parser.add_argument("--functions", help="Comma-separated function names to mutate.")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=200,
        help="Maximum number of candidates to generate.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--eval",
        dest="eval",
        action="store_true",
        default=True,
        help="Compile and score candidates (default).",
    )
    mode.add_argument(
        "--generate-only",
        dest="eval",
        action="store_false",
        help="Only generate patches and summary files.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Accepted for future parallel evaluation; V1 runs serially.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: build/decomp-candidates/<source-stem>).",
    )
    parser.add_argument("--objdiff", default=DEFAULT_OBJDIFF, type=Path)
    parser.add_argument("--report", default=DEFAULT_REPORT, type=Path)
    parser.add_argument("--objdiff-cli", default=DEFAULT_OBJDIFF_CLI, type=Path)
    parser.add_argument("--project", default=Path("."), type=Path)
    parser.add_argument(
        "--keep-c-files",
        action="store_true",
        help="Write full candidate C files next to candidate patches.",
    )
    parser.add_argument(
        "--stop-on-perfect",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stop evaluation after a 100%% function match.",
    )
    parser.add_argument(
        "--mutator-tier",
        choices=("v1a", "v1b", "all"),
        default="v1a",
        help="v1a is conservative; v1b/all also enable speculative V1 mutators.",
    )
    parser.add_argument(
        "--mutators",
        help="Optional comma-separated mutator names to run, such as pad-stack.",
    )
    parser.add_argument(
        "--mutator-id",
        type=int,
        action="append",
        help="Exact mutator lane id to run. Repeatable; negative ids are accepted.",
    )
    return parser.parse_args(argv)


def print_mutators() -> None:
    print("Available decomp candidate mutator lanes:")
    print(MUTATOR_LANE_HELP_TEXT)
    print("\nGrouped overview:")
    for tier, name, description in MUTATOR_HELP:
        print(f"  {tier:3}  {name:28} {description}")


def run(args: argparse.Namespace) -> int:
    if args.list_mutators:
        print_mutators()
        return 0
    if args.path is None:
        raise ValueError("--path is required unless --list-mutators is used")
    if args.functions is None:
        raise ValueError("--functions is required unless --list-mutators is used")
    project = args.project.resolve()
    source_path = project_path(project, args.path).resolve()
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    source_rel = path_relative_to_project(project, source_path)
    if args.output_dir is None:
        args.output_dir = project / "build" / "decomp-candidates" / source_path.stem
    else:
        args.output_dir = project_path(project, args.output_dir).resolve()
    args.objdiff = project_path(project, args.objdiff).resolve()
    args.report = project_path(project, args.report).resolve()
    args.objdiff_cli = project_path(project, args.objdiff_cli).resolve()
    args.mutator_tier = "v1b" if args.mutator_tier == "all" else args.mutator_tier
    args.mutator_filter = parse_mutator_filter(args.mutators, args.mutator_id)

    source_text = source_path.read_text(encoding="utf-8")
    function_names = [name.strip() for name in args.functions.split(",") if name.strip()]
    if not function_names:
        raise ValueError("--functions must contain at least one function name")
    unit = find_unit_for_source(args.objdiff, source_rel) if args.objdiff.exists() else None
    if args.eval and unit is None:
        raise ValueError("--eval requires the source path to exist in objdiff.json")
    report_functions = load_report_functions(args.report, source_rel, function_names)
    candidates = generate_candidates(
        source_text,
        source_rel,
        function_names,
        args.max_candidates,
        args.mutator_tier,
        args.mutator_filter,
    )
    results, baseline = evaluate_candidates(
        project,
        source_path,
        source_rel,
        unit,
        args.objdiff_cli,
        candidates,
        args,
    )
    write_summary(
        args.output_dir,
        source_rel,
        unit,
        candidates,
        results,
        baseline,
        report_functions,
        args,
    )
    print(f"Wrote {args.output_dir / 'summary.json'}")
    print(f"Wrote {args.output_dir / 'summary.md'}")
    if (args.output_dir / "best.patch").exists():
        print(f"Wrote {args.output_dir / 'best.patch'}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run(parse_args()))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
