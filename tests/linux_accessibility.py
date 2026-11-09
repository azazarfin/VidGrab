"""Read-only AT-SPI observations for the named disposable Ubuntu desktop."""

import os
from pathlib import Path


class Accessibility:
    def __init__(self):
        if os.environ.get('VIDGRAB_VALIDATION_VM_ISOLATED') != '1' or \
                Path('/etc/hostname').read_text().strip() != 'vidgrab-p02-ubuntu-validation':
            raise RuntimeError('AT-SPI checks require the named disposable Ubuntu VM')
        import pyatspi
        from gi.repository import Atspi
        Atspi.set_timeout(2000, 2000)
        self.api = pyatspi

    def snapshot(self, application, *, limit=1500):
        desktop = self.api.Registry.getDesktop(0)
        nodes = []

        def visit(node, path, depth=0):
            if depth > 40 or len(nodes) >= limit:
                raise RuntimeError('Accessibility tree exceeds the bounded test observation')
            try:
                role = node.getRoleName()
                state = node.getState()
                item = {'path': path, 'name': node.name, 'role': role,
                    'showing': state.contains(self.api.STATE_SHOWING),
                    'enabled': state.contains(self.api.STATE_ENABLED),
                    'expanded': state.contains(self.api.STATE_EXPANDED),
                    'focused': state.contains(self.api.STATE_FOCUSED)}
                try:
                    component = node.queryComponent()
                    for name, coordinates in [('screen', self.api.DESKTOP_COORDS), ('window', self.api.WINDOW_COORDS)]:
                        box = component.getExtents(coordinates)
                        item[name] = {'x': box.x, 'y': box.y, 'width': box.width, 'height': box.height}
                except NotImplementedError:
                    pass
                try:
                    text = node.queryText()
                    item['characters'] = text.characterCount
                    item['caret'] = text.caretOffset
                    item['selections'] = [text.getSelection(index) for index in range(min(text.getNSelections(), 8))]
                    if role != 'password text':
                        item['text'] = text.getText(0, item['characters'])
                except NotImplementedError:
                    pass
                nodes.append(item)
                children = node.childCount
            except Exception as error:
                # Toolkit objects may disappear while a dialog is being replaced.
                nodes.append({'path': path, 'unavailable': str(error)})
                return
            for index in range(children):
                try:
                    child = node.getChildAtIndex(index)
                except Exception:
                    continue
                if child is not None:
                    visit(child, [*path, index], depth + 1)

        for index in range(desktop.childCount):
            try:
                app = desktop.getChildAtIndex(index)
            except Exception:
                continue
            if app is not None and app.name.casefold() == application.casefold():
                visit(app, [index])
        return nodes

    def find(self, application, name, *, roles=None):
        matches = [node for node in self.snapshot(application) if node.get('name') == name
            and node.get('showing') and (roles is None or node.get('role') in roles)]
        if len(matches) > 1:
            raise AssertionError(f'Ambiguous accessibility control: {application}: {name}')
        return matches[0] if matches else None

    def focused_text(self, application):
        matches = [node for node in self.snapshot(application) if node.get('focused')
            and 'characters' in node]
        if len(matches) > 1:
            raise AssertionError('The selected application has multiple focused text fields')
        return matches[0] if matches else None
