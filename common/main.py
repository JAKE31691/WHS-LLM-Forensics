"""Two-argument launcher for the four existing forensic analyzers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from acquisition import acquire, detect_environments, export_environment, ENVIRONMENTS

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parent


def validate_arguments(argv=None):
    parser = argparse.ArgumentParser(
        prog="act_trace.cmd", usage="act_trace.cmd <E01 file or live> <absolute output directory>")
    parser.add_argument("source")
    parser.add_argument("output")
    args = parser.parse_args(argv)
    if args.source != "live":
        source = Path(args.source).expanduser()
        if source.suffix.lower() != ".e01" or not source.is_file():
            parser.error("source must be an existing E01 file or exactly 'live'")
        args.source = str(source.resolve())
    elif os.name != "nt":
        parser.error("live collection requires Windows")
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        parser.error("output must be an absolute directory path")
    if output.resolve() == Path(output.anchor):
        parser.error("output must be a dedicated directory, not a drive root")
    if output.exists() and not output.is_dir():
        parser.error("output is not a directory")
    for name in ENVIRONMENTS:
        if (output / name).exists():
            parser.error(f"output already contains {name}; use a new output directory")
    # Avoid selecting the repository or an evidence directory as a result tree.
    if args.source != "live" and Path(args.source).is_relative_to(output.resolve()):
        parser.error("output must not contain the source E01")
    return args.source, output.resolve()


def run_worker(environment, stage, destination, context, *, runner=subprocess.run):
    command = [sys.executable, "-u", str(ROOT / "worker.py"), environment,
               str(stage), str(destination), str(context)]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    result = runner(command, cwd=str(REPOSITORY), env=env, check=False)
    return result.returncode


def run(source, output, *, acquire_fn=acquire, detect_fn=detect_environments,
        worker=run_worker):
    output.mkdir(parents=True, exist_ok=True)
    print("ActTrace — integrated artifact analyzer", flush=True)
    print(f'Command line: act_trace.cmd "{source}" "{output}"', flush=True)
    print(f"Source: {source}\nOutput: {output}", flush=True)
    failed = False
    # Only collected copies are passed to native parsers. This directory is ours,
    # is unique, and is removed by TemporaryDirectory even on an interrupted run.
    with tempfile.TemporaryDirectory(prefix=".act-trace-", dir=output) as temporary:
        work = Path(temporary)
        print("\nCollecting registered artifacts for environment detection...", flush=True)
        manifest = acquire_fn(source, work)
        detections, diagnostics = detect_fn(work / "files", manifest)
        for warning in manifest.get("warnings", []):
            print(f"  Collection diagnostic: {warning}", flush=True)
        for warning in diagnostics:
            print(f"  Detection diagnostic: {warning}", flush=True)
        failed = bool(diagnostics or any(f.get("status") != "ok" for f in manifest["files"]))
        failed |= any(not w.startswith("artifact not found:") for w in manifest.get("warnings", []))
        print("\nEnvironment detection:", flush=True)
        for name in ENVIRONMENTS:
            if detections[name]:
                print(f"  {name}: detected ({len(detections[name])} artifact sources)", flush=True)
            else:
                print(f"  {name}: no confirmed trace" + (" (collection/detection errors occurred)" if failed else ""), flush=True)
        context = work / "context.json"
        context.write_text(json.dumps({"source": source,
            "source_type": "Live system" if source == "live" else "E01",
            "detection": detections}, ensure_ascii=False), encoding="utf-8")
        for name in ENVIRONMENTS:
            if not detections[name]:
                continue
            destination = output / name
            stage = work / name
            try:
                export_environment(work / "files", manifest, name, stage)
                print(f"\nAnalyzing {name} -> {destination}", flush=True)
                code = worker(name, stage, destination, context)
                if code:
                    failed = True
                    print(f"  {name}: analysis reported errors (exit {code})", flush=True)
            except Exception as exc:
                failed = True
                print(f"  {name}: {type(exc).__name__}: {exc}", flush=True)
        print(f"\nDetected environments: {sum(bool(v) for v in detections.values())}", flush=True)
    return 1 if failed else 0


def main(argv=None):
    source, output = validate_arguments(argv)
    try:
        return run(source, output)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
