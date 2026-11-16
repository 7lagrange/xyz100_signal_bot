"""Initialize the Windows user data path before the hidden scheduled task imports the bot."""

import ctypes
from datetime import datetime
import logging
import os
from pathlib import Path
import sys
import time


HERE = Path(__file__).resolve().parent


def local_app_data():
    current = os.environ.get('LOCALAPPDATA')
    if current:
        return current
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x001C, None, 0, buffer)
        if result == 0 and buffer.value:
            return buffer.value
    except (AttributeError, OSError):
        pass
    profile = os.environ.get('USERPROFILE')
    if profile:
        return str(Path(profile) / 'AppData' / 'Local')
    raise RuntimeError('Could not locate the current user Local AppData folder.')


def log_failure(error):
    try:
        state_dir = Path(os.environ['LOCALAPPDATA']) / 'XYZ100SignalBot'
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / 'watchdog.log').open('a', encoding='utf-8') as log:
            log.write(f'{datetime.now().astimezone().isoformat()} ERROR Windows launcher: '
                      f'{type(error).__name__}: {error}\n')
    except OSError:
        pass


def main():
    if os.name != 'nt':
        raise SystemExit('This launcher is only for Windows.')
    os.environ['LOCALAPPDATA'] = local_app_data()
    os.chdir(HERE)
    sys.argv = [str(HERE / 'bot.py')]
    import bot
    while True:
        try:
            result = bot.main()
            reason = f'collector returned {result!r}'
        except KeyboardInterrupt:
            return
        except SystemExit as exc:
            reason = f'collector exited with code {exc.code!r}'
        except Exception as exc:
            reason = f'collector crashed ({type(exc).__name__})'
            if logging.getLogger().handlers:
                logging.exception('Collector crashed; supervisor will restart it.')
        state_dir = Path(os.environ['LOCALAPPDATA']) / 'XYZ100SignalBot'
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / 'watchdog.log').open('a', encoding='utf-8') as log:
            log.write(f'{datetime.now().astimezone().isoformat()} WARNING {reason}; '
                      'retrying in 60 seconds.\n')
        time.sleep(60)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        log_failure(exc)
        raise
