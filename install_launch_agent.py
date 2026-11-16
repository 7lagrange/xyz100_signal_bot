"""Install this bot as the current macOS user's login LaunchAgent."""

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from bot import STATE_DIR, load_config


LABEL = 'local.xyz100.signalbot'
HERE = Path(__file__).resolve().parent
LAUNCH_AGENTS = Path.home() / 'Library' / 'LaunchAgents'


def agent_definition(executable, app_dir, state_dir):
    return {
        'Label': LABEL,
        'ProgramArguments': [str(executable), '-u', str(app_dir / 'bot.py')],
        'WorkingDirectory': str(app_dir),
        'RunAtLoad': True,
        'KeepAlive': {'SuccessfulExit': False},
        'ThrottleInterval': 60,
        'ProcessType': 'Background',
        'StandardOutPath': '/dev/null',
        'StandardErrorPath': str(state_dir / 'launchd.error.log'),
        'Umask': 0o077,
    }


def main():
    if sys.platform != 'darwin':
        raise ValueError('This installer only runs on macOS.')
    load_config(STATE_DIR / 'config.json')
    app_dir = STATE_DIR / 'app'
    definition = agent_definition(Path(sys.executable).absolute(), app_dir, STATE_DIR)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = STATE_DIR / 'launch-agent.new.plist'
    temporary.write_bytes(plistlib.dumps(definition, sort_keys=False))
    temporary.chmod(0o600)
    subprocess.run(['/usr/bin/plutil', '-lint', str(temporary)], check=True)

    target = f'gui/{os.getuid()}/{LABEL}'
    loaded = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True)
    if loaded.returncode == 0:
        subprocess.run(['/bin/launchctl', 'bootout', target], check=True)
    app_dir.mkdir(exist_ok=True, mode=0o700)
    for name in ['bot.py', 'dashboard.py', 'dashboard.html', 'site_sync.py', 'config.example.json']:
        source, destination = HERE / name, app_dir / name
        if source != destination:
            staging = app_dir / (name + '.new')
            shutil.copy2(source, staging)
            os.replace(staging, destination)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist = LAUNCH_AGENTS / f'{LABEL}.plist'
    os.replace(temporary, plist)
    subprocess.run(['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=True)
    subprocess.run(['/bin/launchctl', 'print', target], check=True, capture_output=True)
    print(f'Installed and loaded {LABEL}.')
    print(f'Local runtime: {app_dir}')
    print(f'Configuration / database / log: {STATE_DIR}')
    # shortcut: a user LaunchAgent runs after login; use a daemon for pre-login operation.
    print('Starts after Mac login; the Mac must remain awake. Keep this Python environment installed.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
