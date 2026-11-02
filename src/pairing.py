"""Explicit first-time pairing. Existing identities are never silently replaced."""
import json
import re
import secrets
from pathlib import Path

import win_support as win


def validate(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid phone pairing.')
    serial, token = value.get('serial'), value.get('token')
    if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,128}', serial):
        raise ValueError('Invalid paired device identifier.')
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token):
        raise ValueError('Invalid phone pairing. Do not edit the pairing file.')
    return value


def path(root, data):
    # Existing portable installations keep their identity; new installs use
    # per-user data, outside the application's managed install directory.
    legacy = Path(root) / 'pairing.dpapi'
    return legacy if legacy.exists() else Path(data) / 'pairing.dpapi'


def load(root, data):
    try:
        return validate(json.loads(win.protect(path(root, data).read_bytes(), decrypt=True)))
    except FileNotFoundError as error:
        raise RuntimeError('Open Phone setup to pair your iPhone first.') from error


def create(root, data, devices=None):
    if win.connection_active():
        raise RuntimeError('Disconnect the phone connection yourself before pairing.')
    target = path(root, data)
    if target.exists():
        # A corrupt/inaccessible file is an error, never permission to rotate.
        return load(root, data)
    if devices is None:
        import pc_test
        devices = pc_test.list_devices()
    usb = [d for d in devices if isinstance(d, dict) and
           d.get('Properties', {}).get('ConnectionType') == 'USB']
    if len(usb) != 1:
        raise RuntimeError('Connect exactly one iPhone by USB, unlock it, and tap Trust if asked.')
    value = validate({'serial': usb[0]['Properties'].get('SerialNumber'),
                      'token': secrets.token_hex(32)})
    encrypted = win.protect(json.dumps(value).encode())
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents another app instance from rotating the key.
    try:
        with target.open('xb') as handle:
            handle.write(encrypted)
            handle.flush()
    except FileExistsError:
        return load(root, data)
    return value


def preserve_for_installed_app(root, data):
    """Copy a valid legacy DPAPI blob, without overwriting another pairing."""
    source, destination = Path(root) / 'pairing.dpapi', Path(data) / 'pairing.dpapi'
    if not source.is_file() or destination.exists():
        return
    blob = source.read_bytes()
    validate(json.loads(win.protect(blob, decrypt=True)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open('xb') as handle:
            handle.write(blob)
    except FileExistsError:
        pass

