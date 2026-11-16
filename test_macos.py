from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import bot
import install_launch_agent as installer


class MacTests(unittest.TestCase):
    def test_agent_uses_absolute_argument_list_and_failure_restart(self):
        app = Path('/Users/test/Library/Application Support/XYZ100SignalBot/app')
        data = installer.agent_definition('/Users/test/Python Environment/bin/python', app, app.parent)
        roundtrip = plistlib.loads(plistlib.dumps(data))
        self.assertEqual(roundtrip['ProgramArguments'],
                         ['/Users/test/Python Environment/bin/python', '-u', str(app / 'bot.py')])
        self.assertTrue(roundtrip['RunAtLoad'])
        self.assertEqual(roundtrip['KeepAlive'], {'SuccessfulExit': False})
        self.assertEqual(roundtrip['ThrottleInterval'], 60)
        self.assertNotIn('slack_bot_token', str(roundtrip))

    def test_private_config_permissions(self):
        if bot.os.name == 'nt':
            self.skipTest('POSIX file permissions only')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'private' / 'config.json'
            with self.assertRaises(ValueError):
                bot.load_config(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_install_copies_code_preserves_config_and_handles_existing_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, state, agents = root / 'Cloud Source', root / 'Local State', root / 'LaunchAgents'
            source.mkdir()
            state.mkdir()
            (source / 'bot.py').write_text('print("runtime")\n')
            (source / 'dashboard.py').write_text('print("dashboard")\n')
            (source / 'dashboard.html').write_text('<title>Dashboard</title>')
            (source / 'site_sync.py').write_text('print("site sync")\n')
            (source / 'config.example.json').write_text('{}')
            config = {'slack_bot_token': 'xoxb-fake', 'slack_channel': '#unit-test'}
            (state / 'config.json').write_text(json.dumps(config))
            commands = []

            def run(command, **kwargs):
                commands.append(command)
                if command[0] == '/usr/bin/plutil':
                    plistlib.loads(Path(command[-1]).read_bytes())
                return subprocess.CompletedProcess(command, 0)

            with patch.object(installer, 'HERE', source), patch.object(installer, 'STATE_DIR', state), \
                 patch.object(installer, 'LAUNCH_AGENTS', agents), patch.object(installer.sys, 'platform', 'darwin'), \
                 patch.object(installer.subprocess, 'run', side_effect=run), redirect_stdout(StringIO()):
                installer.main()
            self.assertEqual((state / 'app' / 'bot.py').read_bytes(), (source / 'bot.py').read_bytes())
            self.assertEqual((state / 'app' / 'dashboard.html').read_bytes(), (source / 'dashboard.html').read_bytes())
            self.assertEqual(json.loads((state / 'config.json').read_text()), config)
            definition = plistlib.loads((agents / f'{installer.LABEL}.plist').read_bytes())
            self.assertEqual(definition['WorkingDirectory'], str(state / 'app'))
            self.assertNotIn(str(source), str(definition))
            bootout = next(i for i, command in enumerate(commands) if command[1] == 'bootout')
            bootstrap = next(i for i, command in enumerate(commands) if command[1] == 'bootstrap')
            self.assertLess(bootout, bootstrap)

    def test_unconfigured_install_never_registers_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / 'State'
            with patch.object(installer, 'STATE_DIR', state), patch.object(installer.sys, 'platform', 'darwin'), \
                 patch.object(installer.subprocess, 'run') as run, self.assertRaises(ValueError):
                installer.main()
            run.assert_not_called()
            self.assertEqual(json.loads((state / 'config.json').read_text())['slack_bot_token'], '')

    def test_background_logging_uses_rotating_file_without_console_duplication(self):
        with tempfile.TemporaryDirectory() as temporary:
            stderr = Mock()
            stderr.isatty.return_value = False
            with patch.object(bot, 'STATE_DIR', Path(temporary)), patch.object(bot.sys, 'stderr', stderr), \
                 patch.object(bot.logging, 'basicConfig') as configure:
                bot.setup_logging()
            handlers = configure.call_args.kwargs['handlers']
            self.assertEqual(len(handlers), 1)
            self.assertIsInstance(handlers[0], bot.RotatingFileHandler)
            handlers[0].close()


if __name__ == '__main__':
    unittest.main()
