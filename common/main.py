"""Two-argument launcher for the four existing forensic analyzers."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import time

from acquisition import acquire, detect_environments, export_environment, ENVIRONMENTS

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parent
TARGET_NAMES = {
    "chatgpt_app": "ChatGPT Desktop",
    "chatgpt_web": "ChatGPT Web",
    "claude_app": "Claude Desktop",
    "claude_web": "Claude Web",
}


def print_run_info(source, output):
    print("LLM Artifact Analyzer v1.0.0")
    print("Author: jbsquad")
    print("Contact: jbsquad000@gmail.com\n")
    print("Command line:")
    print(f'  act_trace.cmd "{source}" "{output}"\n')
    print("System info:")
    print(f"  Machine name: {platform.node()}")
    print(f"  64-bit: {sys.maxsize > 2**32}")
    print(f"  User: {getpass.getuser()}")
    os_name = "Windows 11" if os.name == "nt" and sys.getwindowsversion().build >= 22000 else platform.platform()
    print(f"  OS: {os_name}\n")
    print("Source info:")
    print(f"  Type: {'Live' if source == 'live' else 'E01'}")
    print(f"  Source: {source}\n")
    print("Output info:")
    print(f"  Path: {output}\n", flush=True)


def native_cli_section(output, destination):
    """Keep each native CLI's analysis block, excluding its separate banner.

    Claude ActTrace works in a temporary folder. Its displayed report paths
    should identify the published folder without changing report contents.
    """
    lines = output.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if line.strip() == "Target:"), None)
    if start is None:
        return output.strip()
    # The integrated launcher has already printed the canonical Target block.
    start = min(start + 2, len(lines))
    end = next((index + 1 for index in range(start, len(lines))
                if lines[index].startswith("Total execution time:")), len(lines))
    section = "\n".join(lines[start:end]).strip()
    temporary_output = re.compile(
        re.escape(str(destination.parent)) + r"[\\/]\.actrace-[^\\/\s]+",
        re.IGNORECASE,
    )
    return temporary_output.sub(lambda _: str(destination), section)


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
    started = time.perf_counter()
    result = runner(command, cwd=str(REPOSITORY), env=env, check=False,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
    section = native_cli_section(result.stdout, destination)
    if section:
        print(section, flush=True)
    if result.stderr:
        print("Analysis diagnostics:", flush=True)
        print(result.stderr.rstrip(), flush=True)
    if "Total execution time:" not in section:
        print(f"\nTotal execution time: {time.perf_counter() - started:.2f} sec", flush=True)
    return result.returncode


def run(source, output, *, acquire_fn=acquire, detect_fn=detect_environments,
        worker=run_worker):
    output.mkdir(parents=True, exist_ok=True)
    print_run_info(source, output)
    failed = False
    # Only collected copies are passed to native parsers. This directory is ours,
    # is unique, and is removed by TemporaryDirectory even on an interrupted run.
    with tempfile.TemporaryDirectory(prefix=".act-trace-", dir=output) as temporary:
        work = Path(temporary)
        manifest = acquire_fn(source, work)
        detections, diagnostics = detect_fn(work / "files", manifest)
        acquisition_errors = [warning for warning in manifest.get("warnings", [])
                              if not warning.startswith("artifact not found:")]
        if acquisition_errors or diagnostics:
            print("Collection diagnostics:", flush=True)
            for warning in acquisition_errors:
                print(f"  {warning}", flush=True)
            for warning in diagnostics:
                print(f"  {warning}", flush=True)
            print(flush=True)
        failed = bool(diagnostics or any(f.get("status") != "ok" for f in manifest["files"]))
        failed |= any(not w.startswith("artifact not found:") for w in manifest.get("warnings", []))
        context = work / "context.json"
        context.write_text(json.dumps({"source": source,
            "source_type": "Live system" if source == "live" else "E01",
            "detection": detections}, ensure_ascii=False), encoding="utf-8")
        analyzed = 0
        for name in ENVIRONMENTS:
            if not detections[name]:
                continue
            if analyzed:
                print("\n" + "=" * 72, flush=True)
            analyzed += 1
            destination = output / name
            stage = work / name
            print("\nTarget:", flush=True)
            print(f"  {TARGET_NAMES[name]}\n", flush=True)
            try:
                export_environment(work / "files", manifest, name, stage)
                code = worker(name, stage, destination, context)
                if code:
                    failed = True
                    print(f"  {name}: analysis reported errors (exit {code})", flush=True)
            except Exception as exc:
                failed = True
                print(f"  {name}: {type(exc).__name__}: {exc}", flush=True)
        if not analyzed:
            print("No supported environment artifacts detected.", flush=True)
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
