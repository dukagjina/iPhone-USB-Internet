"""Final regression checks. No USB, UAC, real routing or user key changes."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import test_web_ui
from test_web_ui import BASE, b, win
import gateway


class FinalAuditTests(unittest.TestCase):
    make_controller = test_web_ui.WebUITests.make_controller

    def test_native_chrome_is_refreshed_on_restore(self):
        import web_ui
        import webview
        import window_chrome
        import desktop_app
        isolated_instance = Mock(is_first=True)
        window = Mock()
        callbacks = {}
        class Event:
            def __init__(self, name): self.name = name
            def __iadd__(self, callback):
                callbacks.setdefault(self.name, []).append(callback)
                return self
        for event in ('before_show', 'shown', 'restored', 'closing'):
            setattr(window.events, event, Event(event))
        with patch.object(desktop_app, 'SingleInstance', return_value=isolated_instance), patch.object(web_ui.pairing, 'preserve_for_installed_app'), patch.object(webview, 'create_window', return_value=window), patch.object(webview, 'start'), patch.object(window_chrome, 'configure_process'), patch.object(window_chrome, 'configure_window') as configure, patch.object(window_chrome, 'install_restore_refresh') as install, patch.object(window_chrome, 'refresh_webview') as refresh:
            web_ui.run()
            for callback in callbacks['shown']: callback()
            for callback in callbacks['restored']: callback()
            self.assertEqual(configure.call_count, 2)
            configure.assert_called_with(window)
            install.assert_called_once_with(window)
            refresh.assert_called_once_with(window)

    def test_restore_refresh_preserves_document_and_ignores_minimized(self):
        import window_chrome
        native = Mock()
        native.IsDisposed = native.webview.IsDisposed = False
        native.WindowState.ToString.return_value = 'Minimized'
        window_chrome._repaint_webview(native)
        native.PerformLayout.assert_not_called()
        native.WindowState.ToString.return_value = 'Normal'
        window_chrome._repaint_webview(native)
        self.assertEqual(native.webview.Bounds, native.ClientRectangle)
        self.assertTrue(native.webview.Visible)
        native.webview.Refresh.assert_called_once()
        native.webview.Reload.assert_not_called()

    def test_rapid_power_clicks_queue_once(self):
        _, bridge, controller = self.make_controller()
        controller.power_action = Mock()
        self.assertTrue(bridge.command('power'))
        for _ in range(30):
            self.assertFalse(bridge.command('power'))
        self.assertEqual(bridge.get_state()['power_button']['state'], 'disabled')
        controller._dispatch()
        controller.power_action.assert_called_once()
        self.assertFalse(bridge._pending_actions)
        self.assertTrue(bridge.command('power'))

    def test_dispatch_failure_is_single_inline_error_and_releases_action(self):
        _, bridge, controller = self.make_controller()
        controller.power_action = Mock(side_effect=RuntimeError('Fixture failure'))
        bridge.command('power')
        controller._dispatch()
        self.assertFalse(bridge._pending_actions)
        self.assertEqual(bridge.get_state()['message']['text'], 'Fixture failure')
        self.assertIsNone(bridge.get_state()['notice'])

    def test_connection_preparation_failure_is_retryable_without_elevation(self):
        for stage in ('directory', 'encryption'):
            with self.subTest(stage=stage):
                _, bridge, controller = self.make_controller()
                controller.check_passed = True
                controller.messagebox = Mock()
                controller.messagebox.askokcancel.return_value = True
                with tempfile.TemporaryDirectory() as folder:
                    target = Mock() if stage == 'directory' else Path(folder) / 'session'
                    if stage == 'directory':
                        target.mkdir.side_effect = PermissionError('Fixture read-only directory')
                    with patch.object(b, 'session_dir', return_value=target), patch.object(win, 'protect', side_effect=OSError('Fixture encryption error')), patch.object(win, 'elevate') as elevate:
                        controller.connect()
                        elevate.assert_not_called()
                self.assertFalse(controller.busy)
                self.assertFalse(controller.check_passed)
                self.assertIsNone(controller.session)
                self.assertEqual(bridge.get_state()['power_button']['state'], 'normal')
                self.assertIn('Could not prepare', bridge.get_state()['message']['text'])

    def test_malformed_status_does_not_stop_polling(self):
        _, bridge, controller = self.make_controller()
        controller.process = 123
        controller.session = '00000000-0000-0000-0000-000000000001'
        bad = [None, [], 7, {}, {'state':[], 'message':'x'},
               {'state':'connected','message':'x','stats':None},
               {'state':'connected','message':'x','stats':{'download':'bad'}},
               {'state':'connected','message':'x','health':{'usb':None}}]
        with patch.object(win, 'alive', return_value=True):
            for value in bad:
                with self.subTest(value=value), patch.object(win, 'read_status', return_value=value):
                    controller.poll()
                    self.assertIn('health is not confirmed', bridge.get_state()['health_label']['text'])
            with patch.object(win, 'read_status', return_value={'state':'connected','message':'Recovered','stats':{},'updated_at':0}):
                controller.poll()
                self.assertIn('unknown', bridge.get_state()['health_label']['text'])

    def test_worker_exit_clears_live_health_and_reenables_check(self):
        _, bridge, controller = self.make_controller()
        controller.process = 123
        controller.session = '00000000-0000-0000-0000-000000000001'
        controller.check_passed = True
        with patch.object(win, 'read_status', return_value={'state':'connected','message':'Previously connected'}), patch.object(win, 'alive', return_value=False), patch.object(win, 'close'):
            controller.poll()
        self.assertIsNone(controller.process)
        self.assertFalse(controller.check_passed)
        self.assertIn('inactive', bridge.get_state()['health_label']['text'])
        self.assertEqual(bridge.get_state()['power_button']['text'], '⏻  Check phone')

    def test_dual_stack_udp_cannot_exhaust_phone_descriptors(self):
        # Admission-only test; listening socket stays on loopback and is never served.
        server = gateway.Gateway({}, 'fixture', queue_timeout=.001)
        self.addCleanup(server.server_close)
        for _ in range(298):
            self.assertTrue(server.acquire_slot(3))
        self.assertFalse(server.acquire_slot(3))
        self.assertLessEqual(3 * server.by_kind[3] + 128, 1024)
        for _ in range(298):
            server.release_slot(3)
        for _ in range(256):
            self.assertTrue(server.acquire_slot(1))
        for _ in range(128):
            self.assertTrue(server.acquire_slot(3))
        self.assertEqual(server.snapshot()['active'], 384)
        self.assertEqual(2 * server.by_kind[1] + 3 * server.by_kind[3] + 128, 1024)
        for kind, count in ((1,256),(3,128)):
            for _ in range(count):
                server.release_slot(kind)


if __name__ == '__main__':
    unittest.main(verbosity=2)
