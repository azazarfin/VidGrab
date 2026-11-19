"""Bounded native permission input in the explicitly selected Windows VM."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4


def validate_receipt(receipt, expected):
    assert isinstance(receipt, dict) and receipt.get('result') == 'passed', receipt
    assert receipt.get('acknowledgement') == expected, 'Foreign native permission acknowledgement'
    assert receipt.get('input') == 'UIAutomation InvokePattern', receipt
    assert isinstance(receipt.get('target'), dict) and receipt['target'].get('name'), receipt
    names = ('Deny', 'Cancel', "Don't Allow") if expected['choice'] == 'deny' else (
        'Allow', 'Add permissions', 'Allow permissions')
    assert receipt['target']['name'] in names, 'Native button differs from the requested choice'
    assert receipt['target'].get('pid') == expected.get('pid'), 'Native button belongs to another process'
    return receipt


def owned_browser_hash(pid, created, program, audit):
    assert audit['pid'] == pid and audit['creation_filetime'] == created
    assert audit['program'] == program, 'The elevated launcher belongs to another executable'
    digest = audit['program_sha256']
    assert re.fullmatch(r'[0-9a-f]{64}', digest), 'Malformed elevated launcher hash'
    return digest


def reserve_reply(path):
    with path.open('x', encoding='utf-8') as stream:
        stream.write('{}')


def browser_identity(browser):
    if browser.kind == 'chrome':
        cdp = browser.context.browser.new_browser_cdp_session()
        try:
            pid = next(int(item['id']) for item in cdp.send('SystemInfo.getProcessInfo')['processInfo']
                if item['type'] == 'browser')
        finally:
            cdp.detach()
    else:
        pid = int(browser.version['moz:processID'])
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4))
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    handle = kernel.OpenProcess(0x1000, False, pid)
    assert handle, 'Cannot identify the selected browser'
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        assert kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
        program, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        assert kernel.QueryFullProcessImageNameW(handle, 0, program, ctypes.byref(size))
        session = wintypes.DWORD()
        assert kernel.ProcessIdToSessionId(pid, ctypes.byref(session)) and session.value == 1
        created = times[0].dwLowDateTime + (times[0].dwHighDateTime << 32)
        if getattr(browser, 'chrome_task', None):
            digest = owned_browser_hash(pid, created, program.value, browser.chrome_task.audit)
        else:
            with Path(program.value).open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        return {'pid': pid, 'creation_filetime': created,
            'program': program.value, 'program_sha256': digest, 'session': session.value}
    finally:
        kernel.CloseHandle(handle)


def permission_input(browser, choice):
    assert os.name == 'nt' and os.environ.get('COMPUTERNAME') == 'DESKTOP-OSOIBNA'
    assert os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') == '1'
    assert os.environ.get('USERNAME', '').casefold() == 'azaz' and choice in ('deny', 'allow')
    started = time.monotonic()
    expected = {'browser': browser.kind, 'choice': choice, 'nonce': str(uuid4()), **browser_identity(browser)}
    prefix = browser.root / ('native-' + choice)
    request, report = prefix.with_suffix('.request.json'), prefix.with_suffix('.reply.json')
    assert not request.exists() and not report.exists()
    request.write_text(json.dumps(expected) + '\n', encoding='utf-8')
    tasks = getattr(getattr(browser, 'chrome_task', None), 'metadata', {}).get('permission_tasks')
    if tasks:
        selected = tasks[choice]
        name, digest = selected['name'], selected['xml_sha256']
        assert re.fullmatch(r'VidGrab-P05-native-input-[a-z0-9]+-\d+-' + choice, name)
        assert re.fullmatch(r'[0-9a-f]{64}', digest)
        assert Path(selected['request']) == request and Path(selected['report']) == report
        reserve_reply(report)
        script = "$ErrorActionPreference='Stop';$service=New-Object -ComObject Schedule.Service;" \
            "$service.Connect();$task=$service.GetFolder('\\').GetTask('" + name + "');" \
            "$sha=[Security.Cryptography.SHA256]::Create();" \
            "$actual=[BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$task.Xml))).Replace('-','').ToLower();" \
            "if($actual -ne '" + digest + "'){throw 'Native input task changed'};$task.Run($null)|Out-Null"
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script]
    else:
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(Path(__file__).with_suffix('.ps1')), '-Request', str(request), '-Report', str(report)]
    result = subprocess.run(command, capture_output=True, timeout=max(0.01, 25 - (time.monotonic() - started)))
    prefix.with_suffix('.outer.json').write_text(json.dumps({'exit': result.returncode,
        'stdout': result.stdout.decode('utf-8', errors='replace'),
        'stderr': result.stderr.decode('utf-8', errors='replace'),
        'seconds': time.monotonic() - started}, indent=2) + '\n', encoding='utf-8')
    result.check_returncode()
    if tasks:
        from tests.native_client_support import windows_wait_file
        windows_wait_file(report, timeout=max(0.01, 25 - (time.monotonic() - started)))
    assert report.is_file() and not report.is_symlink() and report.stat().st_size <= 65536
    return validate_receipt(json.loads(report.read_text(encoding='utf-8-sig')), expected)
