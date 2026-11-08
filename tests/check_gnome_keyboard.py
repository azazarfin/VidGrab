"""Opt-in checks for bounded virtual keyboard input; no VM is launched."""

import unittest

from tests.check_gnome_pointer import QMP


class KeyboardInput(unittest.TestCase):
    def keyboard(self):
        keyboard = QMP.__new__(QMP)
        keyboard.events = []
        keyboard.call = lambda operation, arguments: keyboard.events.append((operation, arguments))
        self.assertTrue(callable(getattr(keyboard, 'type_text', None)),
            'Virtual text input for the manual Firefox setup is missing')
        return keyboard

    def test_repeated_letters_release_each_key(self):
        keyboard = self.keyboard()
        observed = []
        keyboard.type_text('ee', observe=lambda prefix: observed.append((prefix, len(keyboard.events))))
        self.assertEqual([event for _, arguments in keyboard.events for event in arguments['events']], [
            {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': 'e'}}}
            for down in (True, False, True, False)])
        self.assertEqual(observed, [('e', 1), ('ee', 2)])

    def test_colon_releases_its_shift_modifier(self):
        keyboard = self.keyboard()
        keyboard.type_text(':')
        self.assertEqual(keyboard.events[0][1]['events'], [
            {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': key}}}
            for key, down in [('shift', True), ('semicolon', True), ('semicolon', False), ('shift', False)]])

    def test_preference_text_uses_the_existing_key_names(self):
        keyboard = self.keyboard()
        keyboard.type_text('widget.use-xdg-desktop-portal.native-messaging', observe=lambda prefix: None)
        pressed = [event['data']['key']['data'] for _, arguments in keyboard.events for event in arguments['events']
            if event['data']['down']]
        self.assertEqual(pressed,
            list('widget') + ['dot'] + list('use') + ['minus'] + list('xdg') + ['minus']
            + list('desktop') + ['minus'] + list('portal') + ['dot'] + list('native')
            + ['minus'] + list('messaging'))

    def test_unsupported_text_is_rejected_before_any_input(self):
        keyboard = self.keyboard()
        with self.assertRaises(ValueError):
            keyboard.type_text('abc বাংলা', observe=lambda prefix: None)
        self.assertEqual(keyboard.events, [])

    def test_oversized_text_is_rejected_before_any_input(self):
        keyboard = self.keyboard()
        with self.assertRaises(ValueError):
            keyboard.type_text('x' * 2049, observe=lambda prefix: None)
        self.assertEqual(keyboard.events, [])

    def test_shortcut_releases_modifiers_before_text_input(self):
        keyboard = self.keyboard()
        self.assertTrue(callable(getattr(keyboard, 'press_keys', None)),
            'Manual setup shortcuts must release modifiers before typing')
        keyboard.press_keys('ctrl', 'l')
        keyboard.type_text('a')
        self.assertEqual(keyboard.events[0], ('input-send-event', {'events': [
            {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': key}}}
            for key, down in [('ctrl', True), ('l', True), ('l', False), ('ctrl', False)]]}))
        self.assertEqual(len(keyboard.events), 2)

    def test_unsupported_shortcut_is_rejected_before_any_input(self):
        keyboard = self.keyboard()
        self.assertTrue(callable(getattr(keyboard, 'press_keys', None)),
            'Manual setup shortcuts must validate their key names')
        with self.assertRaises(ValueError):
            keyboard.press_keys('ctrl', 'unknown')
        self.assertEqual(keyboard.events, [])

    def test_multi_character_text_requires_observation(self):
        keyboard = self.keyboard()
        with self.assertRaises(ValueError, msg='Unobserved keyboard batches can drop native input'):
            keyboard.type_text('about:config')
        self.assertEqual(keyboard.events, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
