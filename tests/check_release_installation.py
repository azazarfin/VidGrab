"""Opt-in release GUI steps using virtual input in the named disposable Ubuntu VM."""

import argparse
import json
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.native_support import wait_for
from tests.release_installation_controls import Controls


STEPS = ('deb-install', 'launch-file', 'launch-applications', 'fixture-start',
    'fixture-complete', 'appimage-install', 'quit', 'history', 'save',
    'prepare-remove', 'appimage-remove', 'deb-remove', 'file-permission')


def deb_install(controls):
    if any(window['title'] == 'App Center' for window in controls.guest.call('snapshot')['windows']):
        controls.focus('App Center')
        controls.key('alt', 'f4')
        controls.guest.call('window-gone', title='App Center')
    controls.launch_file()
    controls.focus('App Center')
    opened = wait_for(lambda: (value if value['labels']['Install'] else None)
        if (value := controls.screenshot('deb-install', ['Install'], segmentation=6)) else None,
        timeout=15, label='local package Install button')
    controls.click(opened['labels']['Install'], opened['snapshot'])
    screen = opened['snapshot']['screen']
    region = {'x': screen['width'] / 2, 'y': screen['height'] / 2,
        'width': screen['width'] / 4, 'height': screen['height'] / 4}
    confirm = wait_for(lambda: (value if value['labels']['Install'] else None)
        if (value := controls.screenshot('deb-confirm-install', ['Install'],
            region=region, segmentation=6)) else None, timeout=8, label='App Center installation confirmation')
    controls.click(confirm['labels']['Install'], confirm['snapshot'])
    controls.authenticate(controls.guest.call('package-or-auth', expected=True))
    controls.guest.call('installed', expected=True)


def deb_remove(controls):
    controls.focus('App Center')
    # A running App Center keeps its AppStream pool after local reinstall.
    # Close/reopen through Files so its installed list reads the shipped catalog.
    controls.key('alt', 'f4')
    controls.guest.call('window-gone', title='App Center')
    controls.launch_file()
    wait_for(lambda: any(window['title'] == 'App Center' for window in controls.guest.call('snapshot')['windows']),
        timeout=20, label='software manager reopens from Files')
    controls.focus('App Center')
    view = wait_for(lambda: (value if value['labels']['Manage'] else None)
        if (value := controls.screenshot('manager', ['Manage'],
            region={'x': 0, 'y': 0, 'width': 210, 'height': 800})) else None,
        timeout=15, label='rendered software manager navigation')
    controls.click(view['labels']['Manage'], view['snapshot'])
    wait_for(lambda: controls.screenshot('manager-ready', ['Installed apps'])['labels']['Installed apps'],
        timeout=15, label='installed software page')
    # The section heading precedes the asynchronous installed rows. Earlier
    # checks scrolled a loading spinner and mistook it for an absent package.
    wait_for(lambda: (value if any(word['text'] == 'Uninstall' and word['confidence'] >= 75
        and word['y'] > 380 for word in value['words']) else None)
        if (value := controls.screenshot('installed-rows-ready')) else None,
        timeout=45, label='installed application rows finish loading')
    heading = controls.screenshot('installed-filter-heading', ['Installed apps'])['labels']['Installed apps']
    assert heading
    field_region = {'x': heading['x'] - 10, 'y': heading['y'] + 20, 'width': 190, 'height': 55}
    search = wait_for(lambda: (value if value['labels']['Search installed'] else None)
        if (value := controls.screenshot('installed-filter', ['Search installed'],
            region=field_region, segmentation=6)) else None,
        timeout=10, label='installed application filter')
    field = search['labels']['Search installed']
    controls.click(field, search['snapshot'])
    # Flutter exposes no editable AT-SPI text and its caret confuses OCR of
    # partial words. Keep each virtual HID key held for QMP's 50 ms, then
    # verify the resulting sole application row before any removal click.
    controls.qmp.key('ctrl', 'a')
    time.sleep(.08)
    for character in 'vidgrab':
        controls.qmp.key(character)
        time.sleep(.08)
    screen = search['snapshot']['screen']
    below = {'x': 300, 'y': field['y'] + 45, 'width': screen['width'] - 300,
        'height': screen['height'] - field['y'] - 45}
    view = wait_for(lambda: (value if value['labels']['VidGrab'] else None)
        if (value := controls.screenshot('filtered-uninstall-row', ['VidGrab'],
            region=below, segmentation=6)) else None,
        timeout=10, label='sole VidGrab uninstall row')
    row = view['labels']['VidGrab']
    action = controls.guest.call('screen', path=view['capture_path'], labels=['Uninstall'],
        region={'x': 1000, 'y': row['y'] - 15, 'width': 210, 'height': 55}, segmentation=6)
    button = action['labels']['Uninstall']
    assert button, 'Read the uninstall button from the same captured VidGrab row'
    assert abs(row['y'] - button['y']) < 18, 'Bind the button to VidGrab in the same captured frame'
    controls.click(button, view['snapshot'])
    controls.authenticate(controls.guest.call('package-or-auth', expected=False))
    controls.guest.call('installed', expected=False)


def removal(controls, image):
    controls.application_section()
    controls.app_click('Remove installed copy…' if image else 'Prepare to remove…')
    node, _ = controls.node('vidgrab-desktop', 'Keep VidGrab')
    assert node['focused'], 'Removal confirmation must initially focus Keep VidGrab'
    controls.key('esc')
    controls.guest.call('owner-ready')
    controls.app_click('Remove installed copy…' if image else 'Prepare to remove…')
    controls.screenshot('remove-confirmation')
    controls.app_click('Remove and quit' if image else 'Disconnect and quit')
    controls.guest.call('owner-down')
    controls.guest.call('window-gone', title='VidGrab')
    if image:
        files = controls.guest.call('application-files')
        assert not any(files['paths'].values()), files
        assert files['connection']['enabled'] is False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vm', type=Path, required=True)
    parser.add_argument('--package', required=True, help='Selected package in the disposable guest')
    parser.add_argument('--fixture-root', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--screenshots', type=Path, required=True)
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('--preserved-report', type=Path)
    parser.add_argument('--steps', choices=STEPS, nargs='+', required=True)
    args = parser.parse_args()
    vm = json.loads(args.vm.read_text())
    root = Path(vm['root']).resolve(strict=True)
    if root != args.vm.resolve().parent or root.stat().st_mode & 0o077 or vm['state'] != 'running':
        parser.error('Select the running private disposable VM controls')
    if args.report.exists() or args.screenshots.exists() or not args.report.resolve().is_relative_to(root) \
            or not args.screenshots.resolve().is_relative_to(root):
        parser.error('Select fresh report/screenshots inside the private VM controls')
    args.screenshots.mkdir(mode=0o700, parents=True)
    controls = Controls(root, vm, args)
    receipt = {'result': 'running', 'command': controls.guest.command, 'steps': [],
        'limits': ['named disposable Ubuntu GNOME guest and virtual input only',
            'selected fixture descriptor/state/media; no real-user installation',
            'App Center geometry is the validated 1280×800 guest configuration']}
    baseline = json.loads(args.preserved_report.read_text())['saved'] if args.preserved_report else None
    fixture_owner = None
    try:
        package = controls.guest.call('package-file')
        receipt['package'] = package
        other_packages = controls.guest.call('other-packages') if args.package.endswith('.deb') else None
        receipt['other_packages_before'] = other_packages
        for step in args.steps:
            if step == 'deb-install':
                deb_install(controls)
            elif step == 'deb-remove':
                deb_remove(controls)
            elif step == 'launch-file':
                controls.launch_file()
                controls.guest.call('window', title='VidGrab')
                controls.guest.call('owner-ready')
            elif step == 'launch-applications':
                controls.launch_applications('VidGrab (AppImage preview)' if args.package.endswith('.AppImage') else 'VidGrab')
                controls.guest.call('owner-ready')
            elif step == 'fixture-start':
                url = controls.guest.call('fixture-start')['url']
                node, rectangle, snapshot = controls.reveal('Fixture URL', 'entry')
                controls.click(rectangle, snapshot)
                controls.key('ctrl', 'a')
                controls.qmp.type_text(url, observe=lambda prefix: controls.guest.call('text',
                    application='vidgrab-desktop', prefix=prefix))
                controls.app_click('Download test audio')
                fixture_owner = controls.guest.call('owner-active')['supervisor_pid']
            elif step == 'fixture-complete':
                controls.guest.call('fixture-release')
                controls.guest.call('owner-complete')
                wait_for(lambda: controls.guest.call('owner').get('downloads_active') is False,
                    timeout=15, label='all fixture downloads finish')
                fixture_owner = None
            elif step == 'appimage-install':
                controls.application_section()
                controls.app_click('Install AppImage')
                controls.guest.call('app-state', text='Installed in Applications')
                files = controls.guest.call('application-files')
                assert all(files['paths'].values()), files
                if fixture_owner:
                    active = controls.guest.call('owner-active')
                    assert active['supervisor_pid'] == fixture_owner
                    node, _, _ = controls.reveal('Remove installed copy…')
                    assert not node['enabled'], 'Active download removal must be disabled'
            elif step == 'quit':
                controls.app_click('Quit VidGrab')
                controls.guest.call('owner-down')
                controls.guest.call('window-gone', title='VidGrab')
            elif step == 'history':
                owner = controls.guest.call('owner-ready')
                assert any(job['status'] == 'complete' for job in owner['jobs'])
                receipt['history'] = owner
                controls.guest.call('app-state', text='Disconnected')
            elif step == 'save':
                saved = controls.guest.call('saved')
                if baseline:
                    assert saved['files'] == baseline['files'], 'Removal/reinstall changed saved fixture files'
                assert saved['connection']['enabled'] is False
                receipt['saved'] = saved
                baseline = saved
            elif step in ('prepare-remove', 'appimage-remove'):
                removal(controls, step == 'appimage-remove')
            elif step == 'file-permission':
                snapshot = controls.guest.call('snapshot')
                if any(window['focused'] and window['title'] == 'Properties' for window in snapshot['windows']):
                    controls.key('alt', 'f4')
                    controls.guest.call('window-gone', title='Properties')
                controls.focus('release-ux')
                controls.click_node('org.gnome.Nautilus', Path(args.package).name, 'table cell')
                controls.key('alt', 'ret')
                properties = wait_for(lambda: (value if any(window['focused'] and window['title'] != 'release-ux'
                    for window in value['windows']) else None) if (value := controls.guest.call('snapshot')) else None,
                    timeout=8, label='Files Properties dialog')
                receipt['properties'] = controls.guest.call('a11y', application='org.gnome.Nautilus')
                controls.screenshot('file-properties')
                if not controls.guest.call('package-file')['mode'] & 0o111:
                    controls.click_node('org.gnome.Nautilus', 'Executable as Program', 'check box')
                    wait_for(lambda: controls.guest.call('package-file')['mode'] & 0o111,
                        timeout=5, label='Files sets executable permission')
                controls.key('alt', 'f4')
                controls.guest.call('window-gone', title='Properties')
            if step in ('deb-install', 'deb-remove'):
                def packages_settled():
                    after = controls.guest.call('other-packages')
                    changes = {name: {'before': other_packages.get(name), 'after': after.get(name)}
                        for name in other_packages.keys() | after.keys()
                        if other_packages.get(name) != after.get(name)}
                    receipt['other_package_changes'] = changes
                    return not changes
                # dpkg marks VidGrab installed before icon/desktop triggers finish.
                wait_for(packages_settled, timeout=15, label='other OS packages retain their settled states')
            controls.screenshot(step)
            receipt['steps'].append(step)
            print(step + ': passed', flush=True)
        assert controls.guest.call('package-file')['sha256'] == package['sha256']
        receipt['result'] = 'passed'
    except BaseException:
        receipt.update(result='failed', failure=traceback.format_exc())
        controls.qmp.screenshot(args.screenshots / 'failure-before-cleanup.png')
        raise
    finally:
        try:
            controls.close()
        finally:
            receipt['observations'] = controls.records
            args.report.write_text(json.dumps(receipt, indent=2) + '\n')


if __name__ == '__main__':
    main()
