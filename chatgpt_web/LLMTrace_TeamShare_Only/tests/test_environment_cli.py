import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from llmtrace.cli_output import ConsoleReporter, evidence_counts
from llmtrace.core.profile import load_profile
from llmtrace.core.schema import Inference
from llmtrace.environment_cli import resolve_source, validate_output


class EnvironmentCliTest(unittest.TestCase):
    def test_live_alias_resolves_current_user_appdata(self):
        with tempfile.TemporaryDirectory() as td:
            appdata = Path(td) / 'AppData'
            appdata.mkdir()
            with patch.dict(os.environ, {'USERPROFILE': td}):
                source, is_live = resolve_source('live system')
            self.assertTrue(is_live)
            self.assertEqual(source, appdata)

    def test_output_must_be_absolute_and_outside_folder_input(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'input'
            source.mkdir()
            with self.assertRaisesRegex(ValueError, '절대 경로'):
                validate_output('relative-output', source)
            with self.assertRaisesRegex(ValueError, '입력 폴더 내부'):
                validate_output(str(source / 'result'), source)

    def test_environment_command_wrappers_exist(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            'chatgpt-web.cmd': 'chatgpt_web',
            'claude-desktop.cmd': 'claude_app',
            'chatgpt-desktop.cmd': 'chatgpt_desktop',
            'claude-web.cmd': 'claude_web',
        }
        for filename, profile in expected.items():
            text = (root / filename).read_text(encoding='utf-8')
            self.assertIn(profile, text)
            self.assertIn('_run-environment.cmd', text)

    def test_console_report_uses_minimum_refs_as_direct_evidence(self):
        profile = load_profile('chatgpt_web')
        inference = Inference(
            id='inf:0001', behavior_id='B01', behavior_name='ChatGPT Web 접근',
            rule_id='R-ACCESS-STD-01', supported_by=['obs:1', 'obs:2'],
            detail={
                'base_rule_id': 'R-ACCESS-01',
                'refs': {
                    'access_history': {'observation_id': 'obs:1'},
                    'unauth_cache': {'observation_id': 'obs:2'},
                },
            },
        )
        self.assertEqual(evidence_counts(inference, profile.rules), (1, 1))

    def test_console_report_has_requested_sections(self):
        profile = load_profile('chatgpt_web')
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / 'case.E01'
            output = Path(td) / 'result'
            source.touch()
            reporter = ConsoleReporter(profile, str(source), source, output, False)
            stream = io.StringIO()
            with redirect_stdout(stream):
                reporter.print_header()
                reporter({
                    'event': 'artifact_processed', 'index': 1, 'total': 10,
                    'artifact': 'history', 'source_count': 2,
                    'record_count': 5, 'failure_count': 0,
                })
                reporter({'event': 'action_analysis_started'})
                reporter({'event': 'action_analysis_completed', 'inferences': []})
                reporter({'event': 'report_started', 'report_type': 'CSV'})
                reporter({
                    'event': 'report_completed', 'report_type': 'CSV',
                    'path': str(output / 'behaviors.csv'),
                })
                reporter.print_summary({'problems': 0}, 1.25)
            rendered = stream.getvalue()
            self.assertIn('LLMTrace Team Share Exporter v', rendered)
            self.assertIn('Author: jbsquad', rendered)
            self.assertIn('System info:', rendered)
            self.assertIn('Source info:', rendered)
            self.assertIn('Artifact collection:', rendered)
            self.assertIn('[1/10] Browser History', rendered)
            self.assertIn('Analyzing user actions...', rendered)
            self.assertIn('Generating CSV report...', rendered)
            self.assertIn('Execution summary:', rendered)
            self.assertIn('Total execution time: 1.25 sec', rendered)


if __name__ == '__main__':
    unittest.main()
