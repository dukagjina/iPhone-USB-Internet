"""Dark presentation only. No networking, subprocesses, or connection side effects."""
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import ttk

BG = '#0f0f0f'
SURFACE = '#181818'
RAISED = '#202020'
BORDER = '#303030'
TEXT = '#f1f1f1'
MUTED = '#aaaaaa'
ACCENT = '#ff9500'
GREEN = '#65d68e'
RED = '#f07178'
BUILD = 'Dark dashboard · 17 Oct 2026'


def dark_titlebar(window):
    """Best effort: keep native keyboard, resize, minimize, and close behavior."""
    try:
        window.update_idletasks()
        get_parent = ctypes.windll.user32.GetParent
        get_parent.argtypes = [wintypes.HWND]
        get_parent.restype = wintypes.HWND
        hwnd = get_parent(window.winfo_id())
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        enabled = ctypes.c_int(1)
        set_attribute(hwnd, 20, ctypes.byref(enabled), 4)
        for attribute, color in ((35, 0x0f0f0f), (36, 0xf1f1f1)):
            value = wintypes.DWORD(color)
            set_attribute(hwnd, attribute, ctypes.byref(value), 4)
    except (AttributeError, OSError):
        pass


def label(parent, text='', **options):
    return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=TEXT,
                    font=('Segoe UI', 10), **options)


def install(app):
    root = app.root
    root.title('iPhone USB Internet — Dark dashboard')
    root.geometry('920x800')
    root.minsize(860, 780)
    root.configure(bg=BG)
    for pattern, value in [('*Font', ('Segoe UI', 10)), ('*Background', BG),
                           ('*Foreground', TEXT), ('*Text.background', RAISED),
                           ('*Text.foreground', TEXT), ('*Text.insertBackground', TEXT),
                           ('*Text.selectBackground', '#684111')]:
        root.option_add(pattern, value)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('TButton', background=RAISED, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=(12, 8),
                    focuscolor=ACCENT, font=('Segoe UI', 10))
    style.map('TButton', background=[('disabled', SURFACE), ('active', '#272727')],
              foreground=[('disabled', '#717171')])
    style.configure('Power.TButton', background=ACCENT, foreground='#211400',
                    borderwidth=0, padding=(20, 16), font=('Segoe UI', 16, 'bold'))
    style.map('Power.TButton', background=[('disabled', RAISED), ('active', '#e98600')],
              foreground=[('disabled', MUTED)])
    style.configure('Stop.TButton', background=RAISED, foreground=GREEN,
                    padding=(20, 16), font=('Segoe UI', 16, 'bold'), bordercolor=GREEN)
    style.map('Stop.TButton', background=[('disabled', SURFACE), ('active', '#272727')],
              foreground=[('disabled', MUTED)])
    style.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor=RAISED,
                    borderwidth=0, bordercolor=RAISED, thickness=3,
                    lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure('Vertical.TScrollbar', background=RAISED, troughcolor=BG,
                    arrowcolor=MUTED, bordercolor=BG)

    header = tk.Frame(root, bg=BG, padx=26, pady=8)
    header.pack(fill='x')
    logo = label(header, '⏻')
    logo.configure(fg=ACCENT, font=('Segoe UI', 22))
    logo.pack(side='left', padx=(0, 12))
    brand = label(header, 'iPhone USB Internet')
    brand.configure(font=('Segoe UI', 12, 'bold'))
    brand.pack(side='left')
    build = label(header, BUILD)
    build.configure(fg=MUTED, font=('Segoe UI', 9))
    build.pack(side='right')
    tk.Frame(root, bg=BORDER, height=1).pack(fill='x')
    content = tk.Frame(root, bg=BG, padx=26, pady=16)
    content.pack(fill='both', expand=True)
    eyebrow = label(content, 'YOUR CONNECTION')
    eyebrow.configure(fg=ACCENT, font=('Segoe UI', 9, 'bold'))
    eyebrow.pack(anchor='w')
    title = label(content, 'Phone internet, one cable.')
    title.configure(font=('Segoe UI', 22, 'bold'))
    title.pack(anchor='w', pady=(4, 10))

    hero = tk.Frame(content, bg=SURFACE, padx=22, pady=16,
                    highlightbackground=BORDER, highlightthickness=1)
    hero.pack(fill='x')
    app.power_button = ttk.Button(hero, text='⏻  Check phone', style='Power.TButton',
                                 command=app.power_action, width=16)
    app.power_button.pack(side='right', padx=(20, 0))
    info = tk.Frame(hero, bg=SURFACE)
    info.pack(side='left', fill='both', expand=True)
    app.status = label(info, 'Disconnected')
    app.status.configure(font=('Segoe UI', 21, 'bold'))
    app.status.pack(anchor='w')
    app.message = label(info, 'Your normal internet is unchanged. Check your phone to get started.',
                        justify='left', wraplength=430)
    app.message.configure(fg=MUTED, font=('Segoe UI', 10))
    app.message.pack(anchor='w', pady=(8, 0))
    app.progress = ttk.Progressbar(content, mode='determinate')
    app.progress.pack(fill='x', pady=(6, 10))

    metrics = tk.Frame(content, bg=BG)
    metrics.pack(fill='x')
    app.metric_values = {}
    for index, (key, caption, value) in enumerate([
            ('download', 'DOWNLOADED', '0.0 MB'), ('upload', 'UPLOADED', '0.0 MB'),
            ('sessions', 'ACTIVE SESSIONS', '0 / —')]):
        metrics.columnconfigure(index, weight=1, uniform='metric')
        card = tk.Frame(metrics, bg=SURFACE, padx=18, pady=10,
                        highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=0, column=index, sticky='nsew', padx=(0 if index == 0 else 5, 0 if index == 2 else 5))
        heading = label(card, caption)
        heading.configure(fg=MUTED, font=('Segoe UI', 9, 'bold'))
        heading.pack(anchor='w')
        reading = label(card, value)
        reading.configure(font=('Segoe UI', 19, 'bold'))
        reading.pack(anchor='w', pady=(4, 2))
        app.metric_values[key] = reading
        note = label(card, 'This connection · total' if key != 'sessions' else 'In use / phone limit')
        note.configure(fg=MUTED, font=('Segoe UI', 9))
        note.pack(anchor='w')

    health = tk.Frame(content, bg=SURFACE, padx=18, pady=12,
                      highlightbackground=BORDER, highlightthickness=1)
    health.pack(fill='x', pady=(12, 0))
    app.stats = label(health, 'Waiting 0   ·   Rejected 0   ·   Failed requests 0', justify='left')
    app.stats.pack(anchor='w')
    app.health_label = label(health, 'USB and internet health checks start when you connect.',
                             justify='left', wraplength=770)
    app.health_label.configure(fg=MUTED, font=('Segoe UI', 9))
    app.health_label.pack(anchor='w', pady=(5, 0))

    setup = tk.Frame(content, bg=BG)
    setup.pack(fill='x', pady=(12, 0))
    caption = label(setup, 'PHONE SETUP')
    caption.configure(fg=MUTED, font=('Segoe UI', 9, 'bold'))
    caption.pack(anchor='w')
    label(setup, '1  Plug in & unlock    →    2  Cellular on, Wi-Fi & hotspot off    →    3  Keep iSH visible').pack(anchor='w', pady=(8, 10))
    command = tk.Frame(setup, bg=RAISED, padx=12, pady=7)
    command.pack(fill='x')
    cmd = label(command, 'python3 /root/phone_network.py')
    cmd.configure(font=('Consolas', 10), fg=MUTED)
    cmd.pack(side='left')
    ttk.Button(command, text='Copy command', command=app.copy_command).pack(side='right')

    footer = tk.Frame(content, bg=BG)
    footer.pack(side='bottom', fill='x', pady=(12, 0))
    note = label(footer, 'Never auto-connects. Confirmation + Windows approval required.\nNormal internet fallback allowed. Not a VPN or kill switch.', justify='left')
    note.configure(fg=MUTED, font=('Segoe UI', 9))
    note.pack(anchor='w', pady=(0, 6))
    row = tk.Frame(footer, bg=BG)
    row.pack(fill='x')
    app.check_button = ttk.Button(row, text='Recheck phone', command=app.check)
    app.check_button.pack(side='left')
    ttk.Button(row, text='Setup & safety', command=app.help).pack(side='left', padx=8)
    ttk.Button(row, text='Details', command=app.error_details).pack(side='left')
    menu = tk.Menu(root, tearoff=False, bg=RAISED, fg=TEXT, activebackground='#3c2b15',
                   activeforeground=TEXT, relief='flat')
    menu.add_command(label='Phone update…', command=app.phone_update)
    menu.add_command(label='Retire old prototype keys…', command=app.retire_keys)
    more = ttk.Menubutton(row, text='Maintenance', menu=menu)
    more.pack(side='right')
    style.configure('TMenubutton', background=RAISED, foreground=TEXT, padding=(12, 8))
    root.after(250, lambda: dark_titlebar(root))


def update_metrics(app, stats):
    for key in ('download', 'upload'):
        value = stats.get(key, 0) / 1048576
        unit = 'MB'
        if value >= 1024:
            value /= 1024
            unit = 'GB'
        app.metric_values[key].configure(text=f'{value:.1f} {unit}')
    app.metric_values['sessions'].configure(text=f"{stats.get('active', 0)} / {stats.get('capacity', 0)}")
    app.stats.configure(text=f"Waiting {stats.get('queued', 0) + stats.get('dial_waiting', 0)}   ·   "
                        f"Rejected {stats.get('rejected', 0)}   ·   Failed requests {stats.get('failures', 0)}")
