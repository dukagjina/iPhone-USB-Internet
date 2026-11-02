"""PC Center rendering shell around the existing connection controller.

All connection actions stay serialized on the controller's Tk event thread.
The WebView bridge exposes only fixed actions and presentation snapshots.
"""
import copy
import json
import math
import queue
import threading
import uuid

import backend as b
import win_support as win
import phone_upgrade
import credential_cleanup
import presentation
import pairing
import app_info
from app import App


class View:
    def __init__(self, bridge, name, **initial):
        self.bridge, self.name = bridge, name
        with bridge._lock:
            bridge._state[name] = initial

    def configure(self, **values):
        with self.bridge._lock:
            self.bridge._state[self.name].update(values)


class Dialogs:
    def __init__(self, bridge):
        self.bridge = bridge

    def _show(self, title, message, confirm):
        bridge = self.bridge
        event = threading.Event()
        identifier = uuid.uuid4().hex
        with bridge._lock:
            bridge._answer = False
            bridge._dialog_answered = False
            bridge._dialog_event = event
            bridge._state['dialog'] = dict(id=identifier, title=title, message=message, confirm=confirm)
        # A lost renderer must not leave consent waiting forever.
        event.wait(180 if confirm else 600)
        with bridge._lock:
            answer = bridge._answer
            bridge._state['dialog'] = None
            bridge._dialog_event = None
        return answer

    def askokcancel(self, title, message, **_):
        return self._show(title, message, True)

    def showinfo(self, title, message, **_):
        with self.bridge._lock:
            self.bridge._state['notice'] = dict(title=title, message=message)

    showerror = showinfo


class Controller(App):
    def __init__(self, root, bridge):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root, self.bridge = root, bridge
        self.messagebox = Dialogs(bridge)
        self.events = queue.Queue()
        self.process = self.session = self.last_status = None
        self.busy = self.closing = self.stopping = self.check_passed = False
        self.display_state = 'stopped'
        self._receiver = None
        self._maintenance_replies = queue.Queue()
        self._pair_replies = queue.Queue()
        with bridge._lock:
            bridge._state['maintenance'] = dict(receiver='', update_result='', keys_result='')
            bridge._state['diagnostics'] = presentation.snapshot()
            bridge._state['app'] = dict(version=app_info.VERSION, publisher=app_info.PUBLISHER,
                paired=pairing.path(b.ROOT, b.DATA).exists())
        for name in ('status', 'message', 'stats', 'health_label', 'power_button'):
            setattr(self, name, View(bridge, name))
        self.metric_values = {name: View(bridge, name, text='0.0 MB' if name != 'sessions' else '0 / —')
                              for name in ('download', 'upload', 'sessions')}
        self.stats.configure(text='Waiting 0 · Rejected 0 · Failed requests 0')
        self.health_label.configure(text='USB and internet checks start when you connect.')
        self.set_status('stopped', 'Your normal internet is unchanged.')
        self.root.after(200, self.poll)
        self.root.after(50, self._dispatch)

    def _dispatch(self):
        try:
            action = self.bridge._actions.get_nowait()
        except queue.Empty:
            pass
        else:
            handlers = {'power': self.power_action, 'prepare_update': self.prepare_update,
                        'send_update': self.send_update, 'copy_receiver': self.copy_receiver,
                        'retire_keys': self.retire_keys, 'copy': self.copy_command,
                        'close': self.on_close, 'dismiss_notice': self.dismiss_notice,
                        'pair_phone': self.pair_phone}
            try:
                handlers[action]()
            except Exception as error:
                if action == 'power':
                    self.set_status('error', b.error_detail(error))
                else:
                    self.messagebox.showerror('Action could not finish', b.error_detail(error))
            finally:
                with self.bridge._lock:
                    self.bridge._pending_actions.discard(action)
            if action == 'close' and not self.process and not self.busy:
                return
        self.root.after(50, self._dispatch)

    def set_status(self, state, message):
        super().set_status(state, message)
        if state == 'ready':
            message = ('USB and HTTPS checks passed. Connect PC changes Windows routing after '
                       'administrator approval. Other internet connections remain available.')
        # One message beside the primary action; retain the original internally.
        if state == 'error' and message.startswith('Phone check did not pass: '):
            message = message.removeprefix('Phone check did not pass: ').removesuffix(
                '\nKeep the paired phone unlocked with iSH running. PC internet is unchanged.')
            if message == ('Connect exactly one iPhone by USB, unlock it, and tap Trust. '
                           'If already paired here, reconnect that same iPhone.'):
                message = 'Connect your paired iPhone by USB, unlock it, and tap Trust if asked.'
            elif len(message) > 400:
                message = 'Could not check the phone. Keep it unlocked with iSH open, then try again.'
        self.message.configure(text=message)
        with self.bridge._lock:
            self.bridge._state['connection_state'] = state

    def confirm_connection(self):
        # The explicitly labelled Connect PC action is consent. The connection
        # consequence is shown inline beside it; Windows UAC remains mandatory.
        return self.check_passed and not self.busy and not self.process

    def poll(self):
        previous = self.last_status
        super().poll()
        # Publish allowlisted counters, never the raw worker/session object.
        observed = self.last_status is not previous and bool(self.process)
        diagnostics = presentation.snapshot(self.last_status, self.display_state, observed)
        with self.bridge._lock:
            self.bridge._state['diagnostics'] = diagnostics

    def update_buttons(self):
        super().update_buttons()
        free = not self.busy and not self.process
        with self.bridge._lock:
            self.bridge._state['maintenance'].update(
                can_prepare=free, can_send=free and bool(self._receiver),
                can_copy=bool(self._receiver), can_retire=free and self.check_passed)
            if 'app' in self.bridge._state:
                paired = pairing.path(b.ROOT, b.DATA).exists()
                self.bridge._state['app'].update(paired=paired, can_pair=free and not paired)

    def pair_phone(self):
        if not self._maintenance_allowed('update_result'):
            return
        self.busy = True
        self.update_buttons()
        self._maintenance_result('update_result', 'Finding your USB-connected iPhone…')
        def create():
            try:
                pairing.create(b.ROOT, b.DATA)
                result = 'Pairing saved for this Windows account. Prepare the receiver command below.'
            except Exception as error:
                result = 'Pairing could not finish: ' + b.error_detail(error)
            self._pair_replies.put(result)
        threading.Thread(target=create, daemon=True).start()
        self.root.after(100, self._poll_pairing)

    def _poll_pairing(self):
        try:
            message = self._pair_replies.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_pairing)
            return
        self.busy = False
        self._maintenance_result('update_result', message)
        self.update_buttons()

    def dismiss_notice(self):
        with self.bridge._lock:
            self.bridge._state['notice'] = None

    def _maintenance_result(self, key, message):
        with self.bridge._lock:
            self.bridge._state['maintenance'][key] = message

    def _maintenance_allowed(self, result_key):
        if self.busy or self.process or win.connection_active():
            self._maintenance_result(result_key, 'A connection or check is running. Disconnect it yourself before continuing. Nothing was changed.')
            return False
        return True

    def prepare_update(self):
        if not self._maintenance_allowed('update_result'):
            return
        try:
            self._receiver = phone_upgrade.receiver_command(b.RESOURCES, b.load_pairing())
            self._maintenance_result('receiver', self._receiver)
            self._maintenance_result('update_result', 'Receiver command prepared. Nothing has been sent to the phone.')
        except Exception as error:
            self._maintenance_result('update_result', 'Could not prepare the receiver: ' + b.error_detail(error))
        self.update_buttons()

    def copy_receiver(self):
        if self._receiver:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._receiver)

    def send_update(self):
        if not self._maintenance_allowed('update_result'):
            return
        if not self._receiver:
            self._maintenance_result('update_result', 'Prepare the receiver command first.')
            return
        if not self.messagebox.askokcancel('Send the phone update?',
                'Does iSH show READY FOR PHONE UPDATE?\n\nThis backs up and replaces /root/phone_network.py and its pairing key. It does not start the relay or connect this PC.', default='cancel'):
            self._maintenance_result('update_result', 'Update canceled. Nothing was sent.')
            return
        # A different app could have connected while confirmation was open.
        if not self._maintenance_allowed('update_result'):
            return
        self.busy, self.check_passed = True, False
        self.set_status('stopped', 'Phone update in progress. Start the relay and check the phone again after it finishes.')
        self._maintenance_result('update_result', 'Sending over USB…')
        def transfer():
            try:
                result = phone_upgrade.send(b.load_pairing(), b.RESOURCES)
            except Exception as error:
                result = 'Update not confirmed: ' + b.error_detail(error)
            self._maintenance_replies.put(result)
        threading.Thread(target=transfer, daemon=True).start()
        self.root.after(100, self._poll_transfer)

    def _poll_transfer(self):
        try:
            result = self._maintenance_replies.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll_transfer)
            return
        self.busy = False
        self._maintenance_result('update_result', result)
        self.update_buttons()

    def retire_keys(self):
        if not self._maintenance_allowed('keys_result'):
            return
        if not self.check_passed:
            self._maintenance_result('keys_result', 'Go to Connection and pass Check phone first, without connecting. No keys were removed.')
            return
        if not self.messagebox.askokcancel('Retire old prototype keys?',
                'This encrypts recovery copies and removes the known plaintext key files from the old browser/VM tests. Those launchers may need their keys restored to run again. Your current pairing is kept. Continue?', default='cancel'):
            self._maintenance_result('keys_result', 'Canceled. No keys were removed.')
            return
        if not self._maintenance_allowed('keys_result'):
            return
        try:
            removed = credential_cleanup.retire(b.ROOT / 'legacy-keys.json', b.DATA / 'retired-keys')
            self._maintenance_result('keys_result', f'{len(removed)} plaintext key files removed. Encrypted recovery copies are kept in:\n{b.DATA / "retired-keys"}')
        except Exception as error:
            self._maintenance_result('keys_result', 'Cleanup needs attention: ' + b.error_detail(error))


class Bridge:
    ACTIONS = frozenset(('power', 'prepare_update', 'send_update', 'copy_receiver', 'retire_keys', 'copy', 'close', 'dismiss_notice',
                         'pair_phone'))

    def __init__(self):
        self._lock = threading.RLock()
        self._state = {'dialog': None, 'notice': None, 'power_button': {'state': 'disabled', 'text': 'Starting…'}}
        self._actions = queue.Queue(maxsize=8)
        self._pending_actions = set()
        self._window = None
        self._dialog_event = None
        self._answer = False
        self._dialog_answered = False
        self._allow_close = False

    def get_state(self):
        with self._lock:
            state = copy.deepcopy(self._state)
            if 'power' in self._pending_actions:
                state['power_button']['state'] = 'disabled'
            return state

    def command(self, action):
        if not isinstance(action, str) or action not in self.ACTIONS:
            return False
        with self._lock:
            if self._state.get('dialog'):
                return False
            if action in self._pending_actions:
                return False
            if action == 'power' and self._state.get('power_button', {}).get('state') == 'disabled':
                return False
            try:
                self._actions.put_nowait(action)
                self._pending_actions.add(action)
            except queue.Full:
                return False
        return True

    def answer_dialog(self, identifier, accepted):
        with self._lock:
            dialog = self._state.get('dialog')
            if not dialog or identifier != dialog['id'] or type(accepted) is not bool or self._dialog_answered:
                return False
            self._answer = accepted
            self._dialog_answered = True
            self._dialog_event.set()
        return True

    def minimize(self):
        import window_chrome
        if self._window and not window_chrome.minimize_window(self._window):
            self._window.minimize()

    def move_window(self, x, y):
        import window_chrome
        try:
            x, y = float(x), float(y)
            if self._window and math.isfinite(x) and math.isfinite(y):
                return window_chrome.move_window(self._window, x, y)
        except (TypeError, ValueError, OverflowError):
            pass
        return False

    def _closing(self):
        if self._allow_close:
            return True
        self.command('close')
        return False

    def _controller_loop(self):
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.withdraw()
        # Tk is only the existing controller scheduler and clipboard owner.
        # The visible app and maintenance pages are entirely in WebView2.
        for pattern, value in (('*Background', '#181818'), ('*Foreground', '#f1f1f1'),
                               ('*Font', ('Segoe UI', 10)), ('*Text.background', '#202020'),
                               ('*Text.foreground', '#f1f1f1')):
            root.option_add(pattern, value)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TButton', background='#202020', foreground='#f1f1f1', padding=(12, 8))
        controller = Controller(root, self)
        try:
            root.mainloop()
        finally:
            if controller.process and controller.session:
                (b.session_dir(controller.session) / 'stop').touch()
            self._allow_close = True
            self._window.destroy()


def run():
    import webview
    import window_chrome
    import desktop_app
    from window_placement import WindowPlacement
    instance = desktop_app.SingleInstance()
    if not instance.is_first:
        instance.activate_existing()
        instance.close()
        return
    try:
        pairing.preserve_for_installed_app(b.ROOT, b.DATA)
    except (OSError, ValueError, RuntimeError):
        pass  # Legacy pairing still loads in place; a failed migration never rotates it.
    window_chrome.configure_process()
    bridge = Bridge()
    window = webview.create_window('iPhone USB Internet', str(b.RESOURCES / 'ui/index.html'),
                                  js_api=bridge, width=900, height=740, min_size=(820, 600),
                                  background_color='#0f0f0f', frameless=True, easy_drag=False)
    bridge._window = window
    placement = WindowPlacement(app_info.NAME)
    def before_show():
        restored = placement.restore(window, before_show=True)
        placement.fit_to_work_area(window, center=not restored)
    window.events.before_show += before_show
    window.events.shown += lambda: window_chrome.configure_window(window)
    window.events.shown += lambda: window_chrome.install_restore_refresh(window)
    window.events.restored += lambda: window_chrome.restore_window(window)
    def closing():
        allowed = bridge._closing()
        if allowed:
            placement.save(window)
        return allowed
    window.events.closing += closing
    try:
        webview.start(bridge._controller_loop, gui='edgechromium', debug=False)
    finally:
        instance.close()
