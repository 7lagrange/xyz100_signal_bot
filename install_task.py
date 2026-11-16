"""Install/update this bot's current-user Windows sign-in task, then start it."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET


TASK_NAME = 'XYZ100-MA14-99'
HERE = Path(__file__).resolve().parent
NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'


def task_xml(user_sid, executable, script, directory):
    ET.register_namespace('', NS)
    task = ET.Element(f'{{{NS}}}Task', version='1.3')

    def element(parent, name, value=None, **attrs):
        node = ET.SubElement(parent, f'{{{NS}}}{name}', attrs)
        if value is not None:
            node.text = value
        return node

    registration = element(task, 'RegistrationInfo')
    element(registration, 'Description', 'XYZ100 30m SMA14/99 Slack alerts. Public market data only.')
    trigger = element(element(task, 'Triggers'), 'LogonTrigger')
    element(trigger, 'Enabled', 'true')
    element(trigger, 'UserId', user_sid)
    principal = element(element(task, 'Principals'), 'Principal', id='Author')
    element(principal, 'UserId', user_sid)
    element(principal, 'LogonType', 'InteractiveToken')
    element(principal, 'RunLevel', 'LeastPrivilege')
    settings = element(task, 'Settings')
    for name, value in [('MultipleInstancesPolicy', 'IgnoreNew'),
                        ('DisallowStartIfOnBatteries', 'false'), ('StopIfGoingOnBatteries', 'false'),
                        ('AllowHardTerminate', 'true'), ('StartWhenAvailable', 'true'),
                        ('RunOnlyIfNetworkAvailable', 'false'), ('AllowStartOnDemand', 'true'),
                        ('Enabled', 'true'), ('Hidden', 'false'), ('RunOnlyIfIdle', 'false'),
                        ('WakeToRun', 'false'), ('ExecutionTimeLimit', 'PT0S'), ('Priority', '7')]:
        element(settings, name, value)
    restart = element(settings, 'RestartOnFailure')
    element(restart, 'Interval', 'PT1M')
    element(restart, 'Count', '3')
    action = element(element(task, 'Actions', Context='Author'), 'Exec')
    element(action, 'Command', str(executable))
    element(action, 'Arguments', subprocess.list2cmdline(['-u', str(script)]))
    element(action, 'WorkingDirectory', str(directory))
    # shortcut: starts after sign-in; use a Windows service for pre-login operation.
    return ET.tostring(task, encoding='utf-16', xml_declaration=True)


def main():
    if os.name != 'nt':
        raise SystemExit('This installer only runs on Windows.')
    from bot import load_config, STATE_DIR
    load_config()
    identity = subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'])
    match = re.search(rb'S-1-(?:\d+-)+\d+', identity)
    if not match:
        raise RuntimeError('Could not determine the current Windows user SID.')
    executable = Path(sys.executable)
    hidden = executable.with_name('pythonw.exe')
    if hidden.exists():
        executable = hidden
    xml = task_xml(match.group().decode('ascii'), executable, HERE / 'bot.py', HERE)
    with tempfile.TemporaryDirectory(prefix='xyz100-task-') as temporary:
        path = Path(temporary) / 'task.xml'
        path.write_bytes(xml)
        subprocess.run(['schtasks.exe', '/Create', '/TN', TASK_NAME, '/XML', str(path), '/F'], check=True)
    subprocess.run(['schtasks.exe', '/Run', '/TN', TASK_NAME], check=True)
    print(f'Installed and started {TASK_NAME}. Logs/config/database: {STATE_DIR}')
    print('Starts after Windows sign-in; it does not run before sign-in or while the PC is asleep.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(1)
