"""Offline setup/desktop regressions. No real device or routing operations."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

BASE=Path(__file__).resolve().parents[1]
for name in ('iphone-usb-app','iphone-usb-network','iphone-usb-test'):
    sys.path.insert(0,str(BASE/'src'))
import pairing
import win_support as win
import desktop_app
import window_placement

DEVICE={'Properties':{'ConnectionType':'USB','SerialNumber':'FIXTURE-DEVICE-0001'}}

class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'app'; self.data=Path(self.temp.name)/'data'
        self.root.mkdir()
        self.guard=patch.object(win,'connection_active',return_value=False)
        self.guard.start();self.addCleanup(self.guard.stop)

    def test_new_pairing_encrypted_and_repeat_preserves_identity(self):
        value=pairing.create(self.root,self.data,[DEVICE])
        blob=(self.data/'pairing.dpapi').read_bytes()
        self.assertNotIn(value['token'].encode(),blob)
        self.assertEqual(pairing.load(self.root,self.data),value)
        self.assertEqual(pairing.create(self.root,self.data,[]),value)
        self.assertEqual((self.data/'pairing.dpapi').read_bytes(),blob)

    def test_no_device_multiple_devices_and_network_devices_do_not_create_key(self):
        for devices in ([],[DEVICE,DEVICE],[{'Properties':{'ConnectionType':'Network','SerialNumber':'FIXTURE-DEVICE-0001'}}]):
            with self.assertRaises(RuntimeError):pairing.create(self.root,self.data,devices)
            self.assertFalse((self.data/'pairing.dpapi').exists())

    def test_active_connection_blocks_pairing(self):
        with patch.object(win,'connection_active',return_value=True):
            with self.assertRaises(RuntimeError):pairing.create(self.root,self.data,[DEVICE])
        self.assertFalse(self.data.exists())

    def test_corrupt_pairing_is_never_replaced(self):
        target=self.root/'pairing.dpapi';target.write_bytes(b'broken')
        with self.assertRaises(OSError):pairing.create(self.root,self.data,[DEVICE])
        self.assertEqual(target.read_bytes(),b'broken')

    def test_legacy_copy_does_not_overwrite_a_different_installed_pairing(self):
        value={'serial':'FIXTURE-DEVICE-OLD','token':'a'*64}
        legacy=win.protect(json.dumps(value).encode())
        (self.root/'pairing.dpapi').write_bytes(legacy)
        pairing.preserve_for_installed_app(self.root,self.data)
        self.assertEqual((self.data/'pairing.dpapi').read_bytes(),legacy)
        other=win.protect(json.dumps({'serial':'FIXTURE-DEVICE-NEW','token':'b'*64}).encode())
        (self.data/'pairing.dpapi').write_bytes(other)
        pairing.preserve_for_installed_app(self.root,self.data)
        self.assertEqual((self.data/'pairing.dpapi').read_bytes(),other)
        self.assertEqual(pairing.load(self.root,self.data),value)

    def test_invalid_device_identifier_does_not_write(self):
        for serial in (None,'','../bad','a\nwrong'):
            with self.assertRaises(ValueError):pairing.create(self.root,self.data,[{'Properties':{'ConnectionType':'USB','SerialNumber':serial}}])
        self.assertFalse(self.data.exists())

class DesktopTests(unittest.TestCase):
    def test_duplicate_launch_has_no_connection_action(self):
        fake=Mock()
        with patch.object(desktop_app.win32event,'CreateMutex',return_value=fake),patch.object(desktop_app.win32api,'GetLastError',return_value=183):
            instance=desktop_app.SingleInstance()
            self.assertFalse(instance.is_first)
            instance.close();instance.close()
            fake.Close.assert_called_once()

    def test_removed_update_actions_cannot_be_requested(self):
        from web_ui import Bridge
        bridge=Bridge()
        for action in ('check_update','download_update','open_download'):
            self.assertFalse(bridge.command(action))
        self.assertTrue(bridge._actions.empty())

    def test_window_state_roundtrip_and_corruption(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            placement=window_placement.WindowPlacement('Fixture',str(Path(folder)/'window.json'))
            window=SimpleNamespace(native=SimpleNamespace(Handle=123))
            bounds=(-1500,100,-320,920)
            with patch.object(window_placement.win32gui,'GetWindowPlacement',return_value=(0,2,(-1,-1),(-1,-1),bounds)),patch.object(window_placement.win32gui,'SetWindowPlacement') as setter:
                placement.save(window)
                self.assertTrue(placement.restore(window))
                self.assertEqual(setter.call_args.args[1][-1],bounds)
                Path(placement.path).write_text('{broken',encoding='utf-8')
                self.assertFalse(placement.restore(window))

    def test_new_packaging_has_no_auto_connect_or_elevated_launch(self):
        script=(BASE/'release/installer.iss').read_text(encoding='utf-8') if (BASE/'release/installer.iss').exists() else (BASE/'installer.iss').read_text(encoding='utf-8')
        self.assertIn('PrivilegesRequired=lowest',script)
        self.assertIn('CloseApplications=no',script)
        self.assertIn('skipifsilent unchecked',script)
        self.assertNotIn('[UninstallDelete]',script)

if __name__=='__main__':unittest.main()
