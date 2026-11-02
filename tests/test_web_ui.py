"""Offline WebView presentation bridge tests; never runs a network worker."""
import sys
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch, Mock

BASE = Path(__file__).resolve().parent.parent
for name in ('iphone-usb-app', 'iphone-usb-network', 'iphone-usb-test'):
    sys.path.insert(0, str(BASE / 'src'))
from web_ui import Bridge, Controller, Dialogs
import web_ui
import backend as b
import win_support as win


class WebUITests(unittest.TestCase):
    def test_bridge_rejects_arbitrary_actions(self):
        bridge = Bridge()
        for action in ('connect', 'elevate', '__dict__', '../foo', None, {'action':'power'}):
            self.assertFalse(bridge.command(action))
        self.assertTrue(bridge._actions.empty())

    def test_disabled_button_cannot_enqueue(self):
        bridge = Bridge()
        self.assertFalse(bridge.command('power'))
        bridge._state['power_button']['state'] = 'normal'
        self.assertTrue(bridge.command('power'))
        self.assertEqual(bridge._actions.get_nowait(), 'power')

    def test_state_is_a_copy(self):
        bridge = Bridge()
        snapshot = bridge.get_state()
        snapshot['power_button']['state'] = 'normal'
        self.assertEqual(bridge.get_state()['power_button']['state'], 'disabled')

    def test_dialog_requires_matching_id_and_boolean(self):
        bridge = Bridge()
        result = []
        thread = threading.Thread(target=lambda: result.append(Dialogs(bridge).askokcancel('Connect?', 'Confirm')))
        thread.start()
        for _ in range(100):
            if bridge.get_state()['dialog']:
                break
            time.sleep(.01)
        identifier = bridge.get_state()['dialog']['id']
        self.assertFalse(bridge.answer_dialog('wrong', True))
        self.assertFalse(bridge.answer_dialog(identifier, 'yes'))
        self.assertFalse(bridge.command('power'))
        self.assertTrue(bridge.answer_dialog(identifier, False))
        thread.join(2)
        self.assertEqual(result, [False])
        self.assertIsNone(bridge.get_state()['dialog'])

    def test_close_is_vetoed_until_controller_cleanup(self):
        bridge = Bridge()
        self.assertFalse(bridge._closing())
        self.assertEqual(bridge._actions.get_nowait(), 'close')
        bridge._allow_close = True
        self.assertTrue(bridge._closing())

    def test_startup_and_successful_check_never_elevate(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            bridge = Bridge()
            with patch.object(b, 'verify_bundle'), patch.object(b, 'check_phone', return_value='Phone ready.'), patch.object(win, 'elevate') as elevate:
                controller = Controller(root, bridge)
                self.assertIsNone(controller.process)
                self.assertFalse(controller.check_passed)
                controller.power_action()
                for _ in range(100):
                    root.update()
                    if controller.check_passed:
                        break
                    time.sleep(.01)
                self.assertTrue(controller.check_passed)
                self.assertEqual(bridge.get_state()['power_button']['text'], '⏻  Connect PC')
                elevate.assert_not_called()
        finally:
            root.destroy()

    def test_stylesheet_matches_reviewed_theme(self):
        import hashlib
        self.assertEqual(hashlib.sha256((BASE/'src/ui/pc-center.css').read_bytes()).hexdigest(), '6dfa9b1484637a66895624c4b12f6aa338cbc80550e3a5dc2fa8d624f01a5752')

    def test_requested_clutter_is_absent_from_html(self):
        text = (BASE / 'src/ui/index.html').read_text(encoding='utf-8')
        self.assertIn('<title>iPhone USB Internet</title>', text)
        for absent in ('Dark dashboard', '17 Oct', 'YOUR CONNECTION', 'Phone internet, one cable', 'Recheck', 'progressbar', '<progress'):
            self.assertNotIn(absent, text)
        for absent in ('<footer', 'id="help"', 'id="details"', 'id="maintenance"', 'error-panel', 'Full error message'):
            self.assertNotIn(absent, text)

    def make_controller(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        bridge = Bridge()
        controller = Controller(root, bridge)
        return root, bridge, controller

    def test_information_is_inline_not_a_modal(self):
        bridge = Bridge()
        Dialogs(bridge).showinfo('Wait', 'Operation is running.')
        self.assertIsNone(bridge.get_state()['dialog'])
        self.assertEqual(bridge.get_state()['notice']['message'], 'Operation is running.')

    def test_receiver_prepare_is_offline_and_persists(self):
        _, bridge, controller = self.make_controller()
        with patch.object(win, 'connection_active', return_value=False), patch.object(b, 'load_pairing', return_value={}), patch.object(web_ui.phone_upgrade, 'receiver_command', return_value='public-checksum-command'), patch.object(web_ui.phone_upgrade, 'send') as send:
            controller.prepare_update()
            state = bridge.get_state()['maintenance']
            self.assertEqual(state['receiver'], 'public-checksum-command')
            self.assertTrue(state['can_send'])
            self.assertIn('Nothing has been sent', state['update_result'])
            send.assert_not_called()

    def test_maintenance_refuses_active_connection(self):
        _, bridge, controller = self.make_controller()
        controller._receiver = 'prepared'
        controller.check_passed = True
        with patch.object(win, 'connection_active', return_value=True), patch.object(web_ui.phone_upgrade, 'send') as send, patch.object(web_ui.credential_cleanup, 'retire') as retire:
            controller.prepare_update()
            controller.send_update()
            controller.retire_keys()
            send.assert_not_called()
            retire.assert_not_called()
            self.assertIn('Nothing was changed', bridge.get_state()['maintenance']['update_result'])
            self.assertIsNone(bridge.get_state()['dialog'])

    def test_canceled_maintenance_does_not_mutate(self):
        _, bridge, controller = self.make_controller()
        controller._receiver = 'prepared'
        controller.check_passed = True
        controller.messagebox = Mock()
        controller.messagebox.askokcancel.return_value = False
        with patch.object(win, 'connection_active', return_value=False), patch.object(web_ui.phone_upgrade, 'send') as send, patch.object(web_ui.credential_cleanup, 'retire') as retire:
            controller.send_update()
            controller.retire_keys()
            send.assert_not_called()
            retire.assert_not_called()
            self.assertIn('No keys were removed', bridge.get_state()['maintenance']['keys_result'])

    def test_connection_starting_during_confirmation_blocks_maintenance(self):
        _, _, controller = self.make_controller()
        controller._receiver = 'prepared'
        controller.messagebox = Mock()
        controller.messagebox.askokcancel.return_value = True
        with patch.object(win, 'connection_active', side_effect=[False, True]), patch.object(web_ui.phone_upgrade, 'send') as send:
            controller.send_update()
            send.assert_not_called()

    def test_key_retirement_requires_phone_check(self):
        _, bridge, controller = self.make_controller()
        with patch.object(win, 'connection_active', return_value=False), patch.object(web_ui.credential_cleanup, 'retire') as retire:
            controller.retire_keys()
            retire.assert_not_called()
            self.assertIn('Check phone first', bridge.get_state()['maintenance']['keys_result'])

    def test_update_completion_is_inline_and_invalidates_old_check(self):
        root, bridge, controller = self.make_controller()
        controller._receiver = 'prepared'
        controller.check_passed = True
        controller.messagebox = Mock()
        controller.messagebox.askokcancel.return_value = True
        with patch.object(win, 'connection_active', return_value=False), patch.object(b, 'load_pairing', return_value={}), patch.object(web_ui.phone_upgrade, 'send', return_value='Saved fixture') as send:
            controller.send_update()
            for _ in range(100):
                root.update()
                if not controller.busy:
                    break
                time.sleep(.01)
            send.assert_called_once()
            self.assertFalse(controller.busy)
            self.assertFalse(controller.check_passed)
            self.assertEqual(bridge.get_state()['maintenance']['update_result'], 'Saved fixture')
            self.assertIsNone(bridge.get_state()['dialog'])

    def test_retirement_result_is_inline(self):
        _, bridge, controller = self.make_controller()
        controller.check_passed = True
        controller.messagebox = Mock()
        controller.messagebox.askokcancel.return_value = True
        with patch.object(win, 'connection_active', return_value=False), patch.object(web_ui.credential_cleanup, 'retire', return_value=['fixture']):
            controller.retire_keys()
            self.assertIn('1 plaintext key files removed', bridge.get_state()['maintenance']['keys_result'])
            self.assertIsNone(bridge.get_state()['dialog'])

    def test_long_check_error_is_simple_and_original_retained(self):
        _, bridge, controller = self.make_controller()
        error = 'Phone check did not pass: ' + 'complete error ' * 80
        controller.set_status('error', error)
        self.assertEqual(controller.detail_message, error)
        self.assertNotIn('error_detail', bridge.get_state())
        self.assertEqual(bridge.get_state()['message']['text'], 'Could not check the phone. Keep it unlocked with iSH open, then try again.')

    def test_missing_phone_has_one_simple_message(self):
        _, bridge, controller = self.make_controller()
        controller.set_status('error', 'Phone check did not pass: Connect exactly one iPhone by USB, unlock it, and tap Trust. If already paired here, reconnect that same iPhone.\nKeep the paired phone unlocked with iSH running. PC internet is unchanged.')
        self.assertEqual(bridge.get_state()['message']['text'], 'Connect your paired iPhone by USB, unlock it, and tap Trust if asked.')
        self.assertNotIn('error_detail', bridge.get_state())
        self.assertIsNone(bridge.get_state()['notice'])

    def test_other_errors_keep_the_actual_cause(self):
        _, bridge, controller = self.make_controller()
        controller.set_status('error', 'Phone check did not pass: Authentication failed.\nKeep the paired phone unlocked with iSH running. PC internet is unchanged.')
        self.assertEqual(bridge.get_state()['message']['text'], 'Authentication failed.')
        controller.set_status('cleanup-needed', 'Restart Windows before reconnecting.')
        self.assertEqual(bridge.get_state()['message']['text'], 'Restart Windows before reconnecting.')


if __name__ == '__main__':
    unittest.main(verbosity=2)
