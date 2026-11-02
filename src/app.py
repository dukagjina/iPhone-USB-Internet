"""User-launched Windows GUI; default action is always disconnected."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

import backend as b
import win_support as win
import phone_upgrade
import credential_cleanup
import dashboard
from health import describe


PHONE_COMMAND = 'python3 /root/phone_network.py'
HELP = 'Open Phone setup for numbered instructions and About for privacy, educational use and connection limits.'


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk, messagebox
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root = root
        self.events = queue.Queue()
        self.process = None
        self.session = None
        self.busy = False
        self.closing = False
        self.stopping = False
        self.last_status = None
        self.check_passed = False
        self.display_state = 'stopped'
        dashboard.install(self)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(200, self.poll)

    def set_status(self, state, message):
        self.display_state = state
        labels = {'checking': 'Checking phone...', 'ready': 'Phone ready', 'preparing': 'Preparing connection...',
                  'verifying': 'Verifying phone route...', 'connected': 'Connected',
                  'disconnecting': 'Disconnecting...', 'stopped': 'Disconnected',
                  'error': 'Not connected', 'cleanup-needed': 'Recovery needs attention'}
        color = dashboard.GREEN if state in ('connected', 'ready') else dashboard.TEXT
        if state in ('error', 'cleanup-needed'):
            color = dashboard.RED
        self.status.configure(text=labels.get(state, state), fg=color)
        self.detail_message = message
        # Long PowerShell errors must not push Disconnect off the bottom of the window.
        summary = message if len(message) <= 160 else message[:157] + '...'
        self.message.configure(text=summary.replace('\n\n', '\n'))
        self.update_buttons()

    def power_action(self):
        if self.busy or self.stopping:
            return
        if self.process:
            self.disconnect()
        elif self.check_passed:
            self.connect()
        else:
            self.check()

    def update_buttons(self):
        if self.stopping and self.process:
            text, style, enabled = 'Stopping…', 'Stop.TButton', False
        elif self.process:
            text, style, enabled = '⏻  Disconnect', 'Stop.TButton', True
        elif self.busy:
            text, style, enabled = 'Please wait…', 'Power.TButton', False
        elif self.check_passed:
            text, style, enabled = '⏻  Connect PC', 'Power.TButton', True
        else:
            text, style, enabled = '⏻  Check phone', 'Power.TButton', True
        self.power_button.configure(text=text, style=style, state='normal' if enabled else 'disabled')

    def copy_command(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(PHONE_COMMAND)

    def check(self):
        if self.busy or self.process:
            return
        self.busy, self.check_passed = True, False
        self.set_status('checking', 'Checking the paired phone through USB. Your PC internet stays unchanged.')
        self.update_buttons()
        def run():
            try:
                b.verify_bundle()
                self.events.put(('ready', b.check_phone()))
            except Exception as error:
                self.events.put(('error', 'Phone check did not pass: ' + b.error_detail(error) +
                                 '\nKeep the paired phone unlocked with iSH running. PC internet is unchanged.'))
        threading.Thread(target=run, daemon=True).start()

    def connect(self):
        if self.busy or self.process or not self.check_passed:
            return
        if not self.confirm_connection():
            return
        self.busy = True
        self.stopping = False
        self.session = str(uuid.uuid4())
        try:
            directory = b.session_dir(self.session)
            directory.mkdir(parents=True)
            request = dict(session=self.session, consent='connect-with-normal-fallback',
                           parent_pid=os.getpid(), created=time.time())
            (directory / 'request.dpapi').write_bytes(win.protect(json.dumps(request).encode()))
        except Exception as error:
            self.busy = False
            self.check_passed = False
            self.session = None
            self.set_status('error', 'Could not prepare the connection: ' + b.error_detail(error))
            return
        self.last_status = None
        dashboard.update_metrics(self, {})
        self.health_label.configure(text='Waiting for connection health checks.')
        self.set_status('preparing', 'Waiting for your Windows administrator approval...')
        self.update_buttons()
        def launch():
            try:
                self.events.put(('process', win.elevate(['--worker', self.session])))
            except Exception as error:
                self.events.put(('error', b.error_detail(error)))
        threading.Thread(target=launch, daemon=True).start()

    def confirm_connection(self):
        return self.messagebox.askokcancel('Connect this PC through your iPhone?',
            'Connect changes Windows routing using a temporary network adapter. '
            'Other internet connections remain available as fallback. '
            'Continue to Windows administrator approval?', parent=self.root, default='cancel')

    def disconnect(self):
        if self.process and self.session:
            self.stopping = True
            (b.session_dir(self.session) / 'stop').touch()
            self.set_status('disconnecting', 'Stopping the phone connection and removing its temporary routes...')
            self.update_buttons()

    def poll(self):
        while not self.events.empty():
            kind, value = self.events.get()
            self.busy = False
            if kind == 'process':
                self.process = value
                if self.closing:
                    self.disconnect()
            else:
                self.check_passed = kind == 'ready'
                self.set_status(kind, value)
            self.update_buttons()
        if self.process:
            status = None
            try:
                status = win.read_status(b.session_dir(self.session) / 'status.json')
                if not isinstance(status, dict) or not all(isinstance(status.get(k), str) for k in ('state', 'message')):
                    raise ValueError('Invalid connection status')
                self.set_status(status['state'], status['message'])
                stats = status.get('stats', {})
                dashboard.update_metrics(self, stats)
                observed = describe(status.get('health', {}), stats) if status['state'] == 'connected' else 'Health checks inactive — ' + status['state']
                if status['state'] == 'connected' and time.time() - status.get('updated_at', 0) > 5:
                    observed = 'Status updates are delayed — current connection health is unknown.'
                self.health_label.configure(text=observed +
                    f"\nPeaks: {stats.get('peak_active', 0)} active / {stats.get('peak_waiting', 0)} waiting. Failures: {stats.get('failures', 0)}. DNS slots freed: {stats.get('dns_parked', 0)}."
                    + (f"\nRecovered status-write errors: {status['status_write_failures']}" if status.get('status_write_failures') else ''))
                self.last_status = status
            except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
                status = None
                self.health_label.configure(text='Waiting for a readable connection status; health is not confirmed.')
            if not win.alive(self.process):
                win.close(self.process)
                self.process = None
                self.stopping = False
                self.check_passed = False
                self.health_label.configure(text='Health checks inactive — connection worker stopped.')
                if status is None or status.get('state') not in ('stopped', 'error', 'cleanup-needed'):
                    self.set_status('error', 'The connection worker exited unexpectedly. Its tunnel is stopped. If networking does not recover, restart Windows before reconnecting.')
                self.update_buttons()
        if self.closing and not self.process and not self.busy:
            self.root.destroy()
            return
        self.root.after(350, self.poll)

    def on_close(self):
        if self.process or self.busy:
            if self.busy and not self.process:
                self.messagebox.showinfo('Please wait', 'Finish or cancel the current check / Windows approval first.', parent=self.root)
                return
            if not self.messagebox.askokcancel('Disconnect and close?', 'Closing the app will stop the phone connection.', parent=self.root, default='cancel'):
                return
            self.closing = True
            self.disconnect()
        else:
            self.root.destroy()

    def help(self):
        dialog = self.tk.Toplevel(self.root)
        dialog.title('Setup & safety guide')
        dialog.geometry('780x630')
        dialog.after_idle(lambda: dashboard.dark_titlebar(dialog))
        text = self.tk.Text(dialog, wrap='word', padx=20, pady=16, font=('Segoe UI', 10))
        scroll = self.ttk.Scrollbar(dialog, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        text.insert('1.0', HELP)
        text.configure(state='disabled')

    def error_details(self):
        dialog = self.tk.Toplevel(self.root)
        dialog.title('Connection details - iPhone USB Internet')
        dialog.geometry('760x440')
        dialog.after_idle(lambda: dashboard.dark_titlebar(dialog))
        text = self.tk.Text(dialog, wrap='word', padx=16, pady=16, font=('Consolas', 10))
        scroll = self.ttk.Scrollbar(dialog, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        details = getattr(self, 'detail_message', 'No errors recorded in this window.')
        details += '\n\n' + dashboard.BUILD + '\nRunning from: ' + str(b.ROOT)
        if self.last_status:
            details += '\n\nLatest session status:\n' + json.dumps(self.last_status, indent=2)
        text.insert('1.0', details)
        text.configure(state='disabled')

    def phone_update(self):
        if self.process or self.busy or win.connection_active():
            self.messagebox.showinfo('Keep your current connection',
                'A connection is still running. Nothing has been changed. When you are ready, disconnect the old app yourself, then reopen Phone update.', parent=self.root)
            return
        dialog = self.tk.Toplevel(self.root)
        dialog.title('Phone update - does not connect the PC')
        dialog.geometry('790x570')
        dialog.after_idle(lambda: dashboard.dark_titlebar(dialog))
        self.tk.Label(dialog, justify='left', wraplength=740, padx=20, pady=16, text=
            'Do this only when you are ready to interrupt the old phone connection.\n\n'
            '1. Disconnect and close the old Windows app yourself.\n'
            '2. In iSH tap Ctrl, then C to stop the old phone relay.\n'
            '3. Copy the receiver command below to your iPhone, paste it into iSH, and press Return.\n'
            '4. Once iSH says READY FOR PHONE UPDATE, click Send update.\n'
            '5. After Saved appears, run: python3 /root/phone_network.py\n\n'
            'This replaces the default script and pairing key; the previous script is saved as phone_network.py.backup. No new iPhone app is needed.').pack(fill='x')
        try:
            command = phone_upgrade.receiver_command(b.RESOURCES, b.load_pairing())
        except Exception as error:
            dialog.destroy()
            self.messagebox.showerror('Update unavailable', b.error_detail(error), parent=self.root)
            return
        box = self.tk.Text(dialog, wrap='word', height=9, padx=12, pady=10, font=('Consolas', 9))
        box.pack(fill='both', expand=True, padx=20)
        box.insert('1.0', command)
        box.configure(state='disabled')
        result = self.tk.Label(dialog, text='No update sent. PC internet settings are not changed by this wizard.',
                               wraplength=740, justify='left', padx=20, pady=12)
        result.pack(fill='x')
        row = self.tk.Frame(dialog)
        row.pack(fill='x', padx=20, pady=(0, 18))
        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(command)
        self.ttk.Button(row, text='Copy receiver command', command=copy).pack(side='left', padx=(0, 10))
        def send():
            if win.connection_active() or self.busy or self.process:
                result.configure(text='A connection or check is running. Stop it yourself before sending the update.')
                return
            if not self.messagebox.askokcancel('Send the phone update?',
                    'Does iSH show READY FOR PHONE UPDATE? This backs up and replaces the default phone script and pairing key. It does not start it or connect the PC.',
                    parent=dialog, default='cancel'):
                return
            button.configure(state='disabled')
            self.busy = True
            self.check_passed = False
            self.update_buttons()
            replies = queue.Queue()
            result.configure(text='Sending over the trusted USB cable...')
            def transfer():
                try:
                    replies.put(phone_upgrade.send(b.load_pairing(), b.RESOURCES))
                except Exception as error:
                    replies.put('Update not confirmed: ' + b.error_detail(error))
            threading.Thread(target=transfer, daemon=True).start()
            def poll_transfer():
                if replies.empty():
                    self.root.after(200, poll_transfer)
                    return
                message = replies.get()
                self.busy = False
                self.update_buttons()
                if dialog.winfo_exists():
                    result.configure(text=message)
                    button.configure(state='normal')
            self.root.after(200, poll_transfer)
        button = self.ttk.Button(row, text='Send update', command=send)
        button.pack(side='left')

    def retire_keys(self):
        if self.process or self.busy or win.connection_active() or not self.check_passed:
            self.messagebox.showinfo('Check the new phone first',
                'After the phone update, click Check phone in this app while disconnected. Then you can retire the old prototype keys. Nothing has been removed.', parent=self.root)
            return
        if not self.messagebox.askokcancel('Retire old prototype keys?',
                'This encrypts recovery copies and removes three known plaintext key files from the old browser/VM tests. Those old test launchers may need their keys restored to run again. Your current internet is not changed. Continue?',
                parent=self.root, default='cancel'):
            return
        try:
            removed = credential_cleanup.retire(b.ROOT / 'legacy-keys.json', b.DATA / 'retired-keys')
            self.messagebox.showinfo('Old keys retired',
                f'{len(removed)} plaintext key files removed. Encrypted recovery copies are kept under:\n{b.DATA / "retired-keys"}\n\nThe new app does not need these files.', parent=self.root)
        except Exception as error:
            self.messagebox.showerror('Cleanup needs attention', b.error_detail(error), parent=self.root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', metavar='SESSION')
    parser.add_argument('--self-test', action='store_true', help='Offline package and DPAPI checks; never connects')
    parser.add_argument('--package-check', action='store_true', help='Offline integrity check for an unpaired release; never connects')
    args = parser.parse_args()
    if args.self_test or args.package_check:
        b.verify_bundle()
        if args.self_test:
            b.load_pairing()
        return
    if args.worker:
        b.worker(args.worker)
        return
    from web_ui import run
    run()


if __name__ == '__main__':
    main()
