"""LLM Artifact Analyzer — Windows 11 + Chrome에서 Claude(claude.ai)를 쓴 행위를 아티팩트로 확인한다.

사용법: python main.py <E01 경로 | live> <결과 저장 폴더(절대 경로)> [--verify-hash]
  --verify-hash  E01 전체를 읽어 E01에 저장된 해시(MD5, SHA1)와 비교한다. 다르면 분석하지 않는다(종료 코드 2).

main.py는 단계 순서와 콘솔 출력만 맡는다. 판정 로직은 analyzer.py와 rules.csv에 있다.
  1 입력 판별         source.py
  2 수집 (복사, 해시)  collector.py
  3 ZIP               collector.py
  4 파싱 -> JSON      parsers/
  5 판정              analyzer.py + rules.csv
  6 보고서            report_csv.py, report_html.py
"""
import datetime
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "third_party"))
sys.path.insert(0, BASE)

from definitions import (ACTIONS, ARTIFACT_DEFS, AUTHOR, CONTACT, CSV_NAME, HTML_NAME, JSON_NAME,  # noqa: E402
                         TARGET, TOOL_NAME, VERSION, ZIP_NAME)

RULES_PATH = os.path.join(BASE, "rules.csv")
EXIT_OK, EXIT_PARTIAL, EXIT_INPUT = 0, 1, 2


def _quote(a):
    return a if a and all(c.isalnum() or c in "._-" for c in a) else '"%s"' % a


def _status_line(ok, err=None):
    print("  Status: OK" if ok else "  Status: FAILED (%s)" % err)


def run_cli(argv):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    print("%s %s" % (TOOL_NAME, VERSION))
    print("Author: %s" % AUTHOR)
    print("Contact: %s" % CONTACT)
    print()
    if sys.version_info < (3, 10):
        print("ERROR: Python 3.10 이상이 필요합니다 (현재 %s)" % sys.version.split()[0])
        return EXIT_INPUT
    verify_hash = "--verify-hash" in argv
    args = [a for a in argv if a != "--verify-hash"]
    if len(args) != 2 or any(a.startswith("--") for a in args):
        print("Usage: python main.py <E01 file | live> <output folder (absolute path)> [--verify-hash]")
        print('  예) python main.py "C:\\Evidence\\case01.E01" "C:\\Result" --verify-hash')
        print('      python main.py live "C:\\Result"')
        return EXIT_INPUT
    src_arg, out_dir = args
    command_line = "python %s %s" % (os.path.basename(sys.argv[0] or "main.py"), " ".join(_quote(a) for a in argv))
    print("Command line:")
    print("  " + command_line)
    print()
    if not os.path.isabs(out_dir):
        print("ERROR: 결과 저장 경로는 절대 경로여야 합니다: %s" % out_dir)
        return EXIT_INPUT

    from source import SourceError, open_source
    _print_system_info()
    try:
        source = open_source(src_arg)
    except SourceError as e:
        kind = "Live system" if src_arg.lower() == "live" else "E01" if src_arg.lower().endswith((".e01", ".ex01")) else "Unknown"
        print("Source info:")
        print("  Type: %s" % kind)
        print("  Source: %s" % src_arg)
        for line in e.details:
            print("  " + line)
        print("  ERROR: %s" % e)
        return EXIT_INPUT
    return execute(source, out_dir, command_line, verify_hash=verify_hash)


def _print_system_info():
    from source import system_info
    si = system_info()
    print("System info:")
    print("  Machine name: %s" % si["machine"])
    print("  64-bit: %s" % si["is_64bit"])
    print("  User: %s" % si["user"])
    print("  OS: %s" % si["os"])
    print()
    return si


def _check_image(source, verify_hash):
    """E01 세그먼트와 저장된 해시를 출력하고, 요청하면 해시를 검증한다.
    -> (계속 여부, 검증 결과 dict). 해시가 다르면 계속하지 않는다."""
    result = {"requested": verify_hash, "result": "not_run", "computed": {}}
    if source.kind != "E01":
        if verify_hash:
            print("  NOTE: --verify-hash는 E01 입력에만 쓰입니다.")
            result["result"] = "not_applicable"
        return True, result
    ewf = source.ewf
    print("  Segments: %s" % ewf.segment_line())
    stored = ", ".join("%s %s" % (k.upper(), v) for k, v in (("md5", ewf.stored_md5), ("sha1", ewf.stored_sha1)) if v)
    print("  Stored hash: %s" % (stored or "none"))
    if not verify_hash:
        print("  Hash verification: not run (add --verify-hash)")
        return True, result
    if not stored:
        print("  Hash verification: not possible (no hash stored in E01)")
        result["result"] = "no_stored_hash"
        return True, result
    from ewf_check import verify
    t0 = time.perf_counter()

    shown = [-1]

    def progress(done, total):
        pct = done * 100 // max(total, 1)
        if pct != shown[0]:                     # 퍼센트가 바뀔 때만 같은 줄에 다시 쓴다
            shown[0] = pct
            print("\r  Hash verification: computing... %3d%%" % pct, end="", flush=True)

    try:
        computed = verify(ewf, progress)
    except Exception as e:
        print()
        print("  Hash verification: FAILED (%s: %s)" % (type(e).__name__, e))
        result["result"] = "error"
        return False, result
    print()
    result["computed"] = computed
    ok = all(computed[k] == v for k, v in (("md5", ewf.stored_md5), ("sha1", ewf.stored_sha1)) if v)
    for k, v in (("md5", ewf.stored_md5), ("sha1", ewf.stored_sha1)):
        if v:
            print("  Hash verification: %s %s (%.1f sec)" % (k.upper(), "OK" if computed[k] == v else "MISMATCH",
                                                           time.perf_counter() - t0))
            if computed[k] != v:
                print("    stored  : %s" % v)
                print("    computed: %s" % computed[k])
    result["result"] = "ok" if ok else "mismatch"
    return ok, result


def execute(source, out_dir, command_line, rules_path=RULES_PATH, verify_hash=False):
    """입력이 열린 뒤의 전체 과정. 시험 코드도 이 함수를 그대로 부른다."""
    from analyzer import RuleError, analyze, load_rules
    from collector import collect, make_zip
    from parsers import parse_unit
    from report_csv import write_csv
    from report_html import write_html
    from source import system_info

    t0 = time.perf_counter()
    started = datetime.datetime.now(datetime.timezone.utc)
    si = system_info()
    ev = source.evidence_info()

    print("Source info:")
    print("  Type: %s" % source.kind)
    print("  Source: %s" % (source.display if source.kind != "Live system" else "live (%s)" % source.root))
    go_on, hash_result = _check_image(source, verify_hash)
    if not go_on:
        print("  ERROR: E01 해시 검증에 실패해 분석하지 않습니다. 이미지가 손상되었거나 바뀌었을 수 있습니다.")
        source.close()
        return EXIT_INPUT
    print()
    print("Output info:")
    print("  Path: %s" % out_dir)
    print()
    print("Target:")
    print("  %s" % TARGET)
    print()
    if source.kind == "Live system":
        print("NOTE: Close Chrome before running. Files locked by Chrome cannot be copied.")
        print("NOTE: Run as administrator to read other users' folders.")
        print()

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print("ERROR: 결과 폴더를 만들 수 없습니다: %s" % e)
        source.close()
        return EXIT_INPUT

    print("Loading artifact definitions...")
    try:
        rules = load_rules(rules_path)
    except (RuleError, OSError, KeyError) as e:
        print("  ERROR: rules.csv를 읽을 수 없습니다. 아래를 고친 뒤 다시 실행하세요.")
        for line in str(e).splitlines():
            print("    " + line)
        source.close()
        return EXIT_INPUT
    print("  Found %d artifact definitions." % len(ARTIFACT_DEFS))
    print("  Loaded %d rules (%s)." % (len(rules), os.path.basename(rules_path)))
    print()

    workdir = tempfile.mkdtemp(prefix="_work_", dir=out_dir)
    exit_code = EXIT_OK
    try:
        # ---------------------------------------------------------- 수집
        print("Artifact collection:")

        def on_def(i, adef, c):
            fail = " (failed: %d)" % c["failed"] if c["failed"] else ""
            print("  [%d/%d] %s" % (i, len(ARTIFACT_DEFS), adef.name))
            print("        Found: %d files%s" % (c["found"], fail))
            print()

        res = collect(source, workdir, on_def)
        units = res.units
        found = sum(c["found"] for c in res.per_def.values())
        extracted = sum(c["extracted"] for c in res.per_def.values())
        failed = sum(c["failed"] for c in res.per_def.values())
        print("Artifact extraction completed.")
        print("  Chrome profiles      : %d" % len(res.profiles))
        for user, name, prof in res.profiles:
            print("    %s" % prof)
        for sk in res.skipped:
            print("  Skipped: %s (%s)" % (sk.path, sk.reason))
        print("  Artifact definitions : %d" % len(ARTIFACT_DEFS))
        print("  Files found          : %d" % found)
        print("  Files extracted      : %d" % extracted)
        print("  Extraction failures  : %d" % failed)
        print()

        # ---------------------------------------------------------- ZIP (파싱 전에 묶어 손대지 않은 복사본을 남긴다)
        zip_path = os.path.join(out_dir, ZIP_NAME)
        print("Generating ZIP archive...")
        print("  " + zip_path)
        try:
            make_zip(units, workdir, zip_path)
            _status_line(True)
        except Exception as e:
            _status_line(False, "%s: %s" % (type(e).__name__, e))
            exit_code = EXIT_PARTIAL
        print()

        # ---------------------------------------------------------- 파싱
        print("Parsing artifacts...")
        for i, adef in enumerate(ARTIFACT_DEFS, 1):
            us = [u for u in units if u.definition == adef.name]
            for u in us:
                parse_unit(u)
            n_rec = sum(len(u.records) for u in us)
            bad = [u.parse_status for u in us if u.parse_status != "ok"]
            extra = ""
            if bad:
                extra = ", partial %d, failed %d" % (bad.count("partial"), bad.count("error"))
            tag = "[%d/%d]" % (i, len(ARTIFACT_DEFS))
            print("  %-7s %-16s: %d artifact(s), %d records%s" % (tag, adef.name, len(us), n_rec, extra))
        parse_failed = sum(1 for u in units if u.parse_status == "error" and u.collection_status != "error")
        print()

        json_path = os.path.join(out_dir, JSON_NAME)
        print("Generating JSON...")
        print("  " + json_path)
        try:
            _write_json(json_path, units, source, ev, si, command_line, started, rules_path, hash_result)
            _status_line(True)
        except Exception as e:
            _status_line(False, "%s: %s" % (type(e).__name__, e))
            exit_code = EXIT_PARTIAL
        print()

        # ---------------------------------------------------------- 판정
        print("Analyzing user actions...")
        print()
        rows, status = analyze(units, rules, res)
        state_names = (("path_missing", "Path not found"), ("read_failed", "Read failed"),
                       ("no_match", "Read, no match"))
        for aid, ko, en in ACTIONS:
            st = status[aid]
            print("  [%s] %s" % (aid, en))
            if st["state"] == "unsupported":
                print("      Not supported")
            else:
                print("      Direct evidence     : %d" % st["direct"])
                print("      Supporting evidence : %d" % st["support"])
                for key, label in state_names:
                    if st["artifacts"][key]:
                        print("      %-19s : %s" % (label, ", ".join(st["artifacts"][key])))
            print()
        detected = sum(1 for s in status.values() if s["state"] == "detected")
        n_direct = sum(s["direct"] for s in status.values())
        n_support = sum(s["support"] for s in status.values())
        print("User action analysis completed.")
        print("  Actions detected    : %d / %d" % (detected, len(ACTIONS)))
        print("  Direct evidence     : %d" % n_direct)
        print("  Supporting evidence : %d" % n_support)
        print()

        # ---------------------------------------------------------- 보고서
        csv_path = os.path.join(out_dir, CSV_NAME)
        print("Generating CSV report...")
        print("  " + csv_path)
        try:
            write_csv(rows, csv_path)
            _status_line(True)
        except Exception as e:
            _status_line(False, "%s: %s" % (type(e).__name__, e))
            exit_code = EXIT_PARTIAL
        print()

        html_path = os.path.join(out_dir, HTML_NAME)
        print("Generating HTML report...")
        print("  " + html_path)
        info = {"source_type": source.kind, "actions_detected": detected,
                "direct": n_direct, "support": n_support}
        try:
            write_html(rows, status, info, html_path, rules)
            _status_line(True)
        except Exception as e:
            _status_line(False, "%s: %s" % (type(e).__name__, e))
            exit_code = EXIT_PARTIAL
        print()

        partial = sum(1 for u in units if u.parse_status == "partial")
        skipped = len(res.skipped)
        if failed or parse_failed or partial or skipped or hash_result["result"] == "no_stored_hash":
            exit_code = EXIT_PARTIAL

        print("Execution summary:")
        print("  Target               : %s" % TARGET)
        print("  Artifact definitions : %d" % len(ARTIFACT_DEFS))
        print("  Files found          : %d" % found)
        print("  Files extracted      : %d" % extracted)
        print("  Actions detected     : %d / %d" % (detected, len(ACTIONS)))
        print("  Failed               : %d (extraction %d, parsing %d, skipped folders %d)"
              % (failed + parse_failed + skipped, failed, parse_failed, skipped))
        if partial:
            print("  Partially parsed     : %d" % partial)
        print()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        source.close()
    print("Total execution time: %.2f sec" % (time.perf_counter() - t0))
    return exit_code


def _write_json(path, units, source, ev, si, command_line, started, rules_path, hash_result):
    from timeutil import iso_utc
    doc = {
        "tool": {"name": TOOL_NAME, "version": VERSION, "target": TARGET},
        "run": {"command_line": command_line, "started_at": iso_utc(started),
                "machine": si["machine"], "user": si["user"], "os": si["os"],
                "rules": os.path.basename(rules_path)},
        "source": {"type": source.kind,
                   "path": source.display if source.kind != "Live system" else source.root,
                   "evidence_hostname": ev.get("hostname"), "evidence_os": ev.get("os"),
                   "ewf": dict(source.ewf.to_json(), hash_verification=hash_result) if source.kind == "E01" else None},
        "artifact_definitions": [a.name for a in ARTIFACT_DEFS],
        "artifacts": [{
            "definition": u.definition,
            "path": u.evidence_path,
            "zip_path": u.zip_path,
            "user": u.user,
            "profile": u.profile,
            "collection_status": u.collection_status,
            "collection_note": u.collection_note,
            "parse_status": u.parse_status,
            "parse_note": u.parse_note,
            "files": u.files,
            "records": u.records,
        } for u in units],
    }
    # 짝이 없는 서로게이트 문자(깨진 이모지 등)는 \udXXX 로 써서 JSON 문법을 지킨다
    with open(path, "w", encoding="utf-8", errors="backslashreplace") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(run_cli(sys.argv[1:]))
