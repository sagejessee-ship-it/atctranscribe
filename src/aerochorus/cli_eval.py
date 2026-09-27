"""CLI: benchmark preparation, gold import, evaluation reports (Phase 4)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aerochorus.cli_transcription import _emit, _pct, _table


def _eval_client(args: argparse.Namespace):
    from aerochorus.cli import _api_url
    from aerochorus.eval_client import EvalClient

    return EvalClient(_api_url(args))


def cmd_atco2_prepare(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from aerochorus.datasets.atco2 import prepare_fixed_clips

    summary = prepare_fixed_clips(args.data, args.out)
    report = asdict(summary)
    report["gold_speech_minutes"] = round(summary.gold_speech_ms / 60000, 2)
    report["warnings"] = report["warnings"][:20] + (
        [f"... {len(summary.warnings) - 20} more"] if len(summary.warnings) > 20 else []
    )
    print(json.dumps(report, indent=2))
    return 0


def cmd_atco2_import(args: argparse.Namespace) -> int:
    from aerochorus.datasets.atco2 import read_gold_references
    from aerochorus.eval_contracts import GoldReferenceIn

    rows = [GoldReferenceIn.model_validate(r) for r in read_gold_references(args.data)]
    with _eval_client(args) as client:
        result = client.import_references(args.source, rows)
    splits: dict[str, int] = {}
    for r in rows:
        splits[r.split] = splits.get(r.split, 0) + 1
    print(json.dumps(result.model_dump() | {"references": len(rows), "splits": splits}, indent=2))
    return 0


def _fmt(value) -> str:
    return "" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)


def cmd_eval_report(args: argparse.Namespace) -> int:
    with _eval_client(args) as client:
        report = client.evaluate(
            args.id,
            canonical_numbers=args.canonical_numbers,
            english_only=args.english_only,
            split=args.split,
        )
    if args.json:
        _emit(args, report)
        return 0
    print(
        f"sweep {report.sweep_id} ({report.suite}) on {report.source_key}: "
        f"{report.segments_scored} segments scored, {report.reference_tokens} reference tokens"
        f"{', split=' + report.split if report.split else ''}"
        f"{', english-only' if report.english_only else ''}"
        f"{', canonical numbers' if report.canonical_numbers else ''}"
    )
    rows = []
    for m in report.models:
        ent = m.entities
        rows.append(
            {
                "model": m.logical_name + (" [contaminated]" if m.contaminated else ""),
                "TER": _fmt(m.token_error_rate),
                "S/D/I": f"{m.substitutions}/{m.deletions}/{m.insertions}",
                "CER": _fmt(m.char_error_rate),
                "exact": _fmt(m.exact_match_rate),
                "callsign": _fmt(ent["callsign"].recall),
                "value": _fmt(ent["value"].recall),
                "command": _fmt(ent["command"].recall),
                "abstain": _pct(m.abstained, m.segments),
                "error": _pct(m.error, m.segments),
                "rtf": _fmt(m.real_time_factor),
                "top flags": ", ".join(f"{k}:{v}" for k, v in list(m.flags.items())[:3]),
            }
        )
    cols = [
        "model",
        "TER",
        "S/D/I",
        "CER",
        "exact",
        "callsign",
        "value",
        "command",
        "abstain",
        "error",
        "rtf",
        "top flags",
    ]
    print(_table(rows, cols))
    print()
    if report.best_single:
        print(
            f"best single model : {report.best_single['model']}  "
            f"TER {_fmt(report.best_single['token_error_rate'])}"
        )
    if report.oracle:
        print(
            f"oracle (per-seg)  : TER {_fmt(report.oracle['token_error_rate'])}  "
            f"wins {report.oracle['segment_wins_incl_ties']}"
        )
    print("ensemble          : not implemented (Phase 6)")
    if report.by_split:
        print("\nby split (TER):")
        names = [m.logical_name for m in report.models]
        split_rows = [
            {"split": s, "segments": v.get("_segments"), **{n: _fmt(v.get(n)) for n in names}}
            for s, v in report.by_split.items()
        ]
        print(_table(split_rows, ["split", "segments", *names]))
    print("\nfamily agreement vs error:")
    print(
        _table(
            [b.model_dump() for b in report.agreement_bins],
            ["agreement_range", "segments", "oracle_ter", "best_single_ter"],
        )
    )
    if report.gold_flag_counts:
        base = ", ".join(f"{k}:{v}" for k, v in report.gold_flag_counts.items())
        print()
        print(f"flag base rate on the human gold ({report.segments_scored} segments): {base}")
    for note in report.notes:
        print(f"note: {note}")
    return 0


def cmd_eval_segments(args: argparse.Namespace) -> int:
    with _eval_client(args) as client:
        rows = client.evaluation_segments(
            args.id, model=args.model, limit=args.limit, offset=args.offset
        )
    if args.json:
        _emit(args, rows)
        return 0
    for row in rows:
        print(f"\n[{row.segment_id}] {row.relative_path} ({row.split})")
        print(f"   {'GOLD':<30} {row.reference_scoring}")
        for model, r in sorted(row.results.items()):
            text = r["text"] if r["status"] == "success" else f"<{r['status']}>"
            flags = f"  {r['flags']}" if r["flags"] else ""
            print(f"   {model[:26]:<26} {r['errors']:>3} {text}{flags}")
    return 0


def cmd_results_reflag(args: argparse.Namespace) -> int:
    with _eval_client(args) as client:
        _emit(args, client.reflag())
    return 0


def register(groups: argparse._SubParsersAction, api_opt) -> None:
    ev = groups.add_parser("eval", help="benchmark evaluation (ATCO2)").add_subparsers(
        dest="cmd", required=True
    )
    atco2 = ev.add_parser("atco2", help="ATCO2 benchmark adapter").add_subparsers(
        dest="atco2_cmd", required=True
    )
    p = atco2.add_parser("prepare", help="clip gold segments at exact XML boundaries (no text)")
    p.add_argument("--data", type=Path, required=True, help="ATCO2 DATA directory")
    p.add_argument("--out", type=Path, required=True, help="derived clip corpus directory")
    p.set_defaults(func=cmd_atco2_prepare)
    p = atco2.add_parser("import-references", help="load gold text into the reference schema")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--source", default="atco2_fixed", help="corpus source of the prepared clips")
    api_opt(p)
    p.set_defaults(func=cmd_atco2_import)

    p = ev.add_parser("report", help="score a sweep against gold")
    p.add_argument("id", type=int)
    p.add_argument("--canonical-numbers", action="store_true", help="fold number format")
    p.add_argument("--english-only", action="store_true")
    p.add_argument("--split", choices=["calibration", "test"])
    p.add_argument("--json", action="store_true")
    api_opt(p)
    p.set_defaults(func=cmd_eval_report)
    p = ev.add_parser("segments", help="gold vs every hypothesis, worst first")
    p.add_argument("id", type=int)
    p.add_argument("--model")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--json", action="store_true")
    api_opt(p)
    p.set_defaults(func=cmd_eval_segments)

    results = groups.add_parser("results", help="transcription results").add_subparsers(
        dest="cmd", required=True
    )
    p = results.add_parser("reflag", help="recompute quality flags after a lexicon change")
    p.add_argument("--json", action="store_true")
    api_opt(p)
    p.set_defaults(func=cmd_results_reflag)
