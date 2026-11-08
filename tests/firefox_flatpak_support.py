"""Process observations for Firefox in a disposable Flatpak test session."""

from pathlib import Path


def process_tree(root_pid, *, proc=Path('/proc')):
    entries = {}
    for directory in proc.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            fields = dict(line.split(':', 1) for line in (directory / 'status').read_text().splitlines()
                if ':' in line)
            entries[int(directory.name)] = {
                'parent': int(fields['PPid']),
                'namespace': [int(value) for value in fields.get('NSpid', '').split()],
                'seccomp': fields['Seccomp'].strip(),
                'no_new_privileges': fields['NoNewPrivs'].strip(),
            }
        except (OSError, KeyError, ValueError):
            continue
    owned = {root_pid}
    while children := {pid for pid, fields in entries.items()
            if fields['parent'] in owned and pid not in owned}:
        owned.update(children)
    return {pid: entries[pid] for pid in owned if pid in entries}


class FlatpakProcesses:
    def __init__(self, root_pid, *, proc=Path('/proc')):
        self.entries = process_tree(root_pid, proc=proc)
        assert root_pid in self.entries, 'Flatpak instance process is missing'
        # Flatpak reports its outer bwrap PID, which can remain in the host namespace.
        depths = [len(fields['namespace']) - 1 for fields in self.entries.values()
            if len(fields['namespace']) > 1]
        assert depths, 'Flatpak instance must have a private PID namespace'
        self.depth = min(depths)

    def host_pid(self, namespace_pid):
        matches = [pid for pid, fields in self.entries.items()
            if len(fields['namespace']) > self.depth and fields['namespace'][self.depth] == namespace_pid]
        assert matches, f'Flatpak PID {namespace_pid} is missing from this instance'
        assert len(matches) == 1, f'Flatpak PID {namespace_pid} is ambiguous within this instance'
        return matches[0]

    def content(self, information):
        content = []
        for process in information:
            if not (process['type'].startswith('web') or process['type'] == 'extension'):
                continue
            pid = self.host_pid(process['pid'])
            fields = self.entries[pid]
            content.append({'pid': pid, 'namespace_pid': process['pid'], 'type': process['type'],
                'seccomp': fields['seccomp'], 'no_new_privileges': fields['no_new_privileges']})
        assert content and all(item['seccomp'] == '2' and item['no_new_privileges'] == '1'
            for item in content), f'Firefox content sandbox audit failed: {content}'
        return content
