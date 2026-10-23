"""Windows desktop host. Secrets use current-user DPAPI, never command arguments.

Run setup through SETUP-WINDOWS.cmd. No inbound network listener is created.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

APP = Path(__file__).resolve().parent


def data_root():
    if os.name != 'nt':
        raise RuntimeError('This launcher requires Windows')
    return Path(os.environ['LOCALAPPDATA']) / 'SentinelTradingBot'


def protect(payload, decrypt=False):
    """DPAPI tied to the signed-in Windows account; no machine-wide protection."""
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = ctypes.create_string_buffer(payload)
    src = Blob(len(payload), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    dst = Blob()
    api = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dst)):
        raise RuntimeError('Windows credential protection failed')
    try:
        return ctypes.string_at(dst.data, dst.size)
    finally:
        kernel.LocalFree(ctypes.cast(dst.data, ctypes.c_void_p))


def read_settings(root):
    return json.loads(protect((root / 'credentials.dpapi').read_bytes(), decrypt=True))


def validate(c):
    if ':' not in c.get('TELEGRAM_BOT_TOKEN', ''):
        raise ValueError('Enter the token from your own Telegram BotFather bot.')
    for key, prefix in [('SOLANA_RPC_URL', 'https://'), ('SOLANA_WS_URL', 'wss://')]:
        if c.get(key) and not c[key].startswith(prefix):
            raise ValueError(key + ' must start with ' + prefix)
    if c.get('BOT_MODE') not in ('paper', 'live'):
        raise ValueError('Invalid trading mode')
    if c['BOT_MODE'] == 'live':
        from solders.pubkey import Pubkey
        try:
            Pubkey.from_string(c.get('EXPECTED_WALLET', ''))
        except ValueError:
            raise ValueError('Enter the dedicated wallet address shown by /wallet.') from None
        if not all(c.get(k) for k in ('SOLANA_RPC_URL', 'SOLANA_WS_URL', 'JUPITER_API_KEY')):
            raise ValueError('Live mode requires both Solana endpoints and a Jupiter API key.')


def child_environment(c, root):
    validate(c)
    env = os.environ.copy()
    for key in ('TELEGRAM_BOT_TOKEN', 'SOLANA_RPC_URL', 'SOLANA_WS_URL',
                'JUPITER_API_KEY', 'PAIRING_CODE', 'WALLET_VAULT_KEY', 'EXPECTED_WALLET'):
        env[key] = c.get(key, '')
    env.update(DATA_DIR=str(root), BOT_MODE=c['BOT_MODE'],
               ENABLE_LIVE_TRADING='YES' if c['BOT_MODE'] == 'live' else 'NO')
    return env


def startup_shortcut(enabled):
    # Pass paths as environment values, not interpolated PowerShell source.
    target = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup/Sentinel.lnk'
    if not enabled:
        target.unlink(missing_ok=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(SENTINEL_LINK=str(target), SENTINEL_PY=str(Path(sys.executable).with_name('pythonw.exe')),
               SENTINEL_SCRIPT=str(APP / 'windows_host.py'), SENTINEL_APP=str(APP))
    script = '''$ErrorActionPreference='Stop'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SENTINEL_LINK); $s.TargetPath=$env:SENTINEL_PY; $s.Arguments='"'+$env:SENTINEL_SCRIPT+'" run'; $s.WorkingDirectory=$env:SENTINEL_APP; $s.Save()'''
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                   env=env, check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)


def setup(root):
    import tkinter as tk
    from tkinter import messagebox
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'credentials.dpapi'
    c = read_settings(root) if path.exists() else {
        'PAIRING_CODE': secrets.token_urlsafe(24),
        'WALLET_VAULT_KEY': secrets.token_urlsafe(48), 'BOT_MODE': 'paper'}
    window = tk.Tk()
    window.title('SENTINEL - Windows host')
    window.geometry('740x650')
    tk.Label(window, text='Control this PC from Telegram, anywhere.', font=('', 16)).pack(pady=12)
    tk.Label(window, text='Starts paused. Leave PC powered on and signed in. Locking the screen is OK.').pack()
    fields = {}
    for key, title in [('TELEGRAM_BOT_TOKEN', 'Telegram bot token (from @BotFather)'),
                       ('SOLANA_RPC_URL', 'Solana HTTPS RPC URL (can add later)'),
                       ('SOLANA_WS_URL', 'Solana WSS URL (can add later)'),
                       ('JUPITER_API_KEY', 'Jupiter API key (can add later)'),
                       ('EXPECTED_WALLET', 'Live only: dedicated public address from /wallet')]:
        tk.Label(window, text=title).pack(anchor='w', padx=24, pady=(8, 0))
        v = tk.StringVar(value=c.get(key, ''))
        tk.Entry(window, textvariable=v, width=95, show='' if key == 'EXPECTED_WALLET' else '*').pack(padx=24)
        fields[key] = v
    auto = tk.BooleanVar(value=c.get('autostart', True))
    live = tk.BooleanVar(value=c['BOT_MODE'] == 'live')
    tk.Checkbutton(window, text='Start after I sign into Windows', variable=auto).pack(anchor='w', padx=24, pady=8)
    tk.Checkbutton(window, text='LIVE trading: real funds at risk (leave off for setup/testing)', variable=live).pack(anchor='w', padx=24)
    tk.Label(window, text='Pair in a private chat with YOUR bot using this command:').pack(pady=(10, 0))
    pairing = tk.Entry(window, width=75)
    pairing.insert(0, '/pair ' + c['PAIRING_CODE'])
    pairing.configure(state='readonly')
    pairing.pack()
    tk.Label(window, text='Credentials are encrypted for this Windows account. Never send tokens or wallet keys in chat.\nAn empty watchlist blocks trades. No profit or trade-count guarantee.').pack(pady=10)

    def save():
        next_c = {**c, **{k: v.get().strip() for k, v in fields.items()},
                  'BOT_MODE': 'live' if live.get() else 'paper', 'autostart': auto.get()}
        try:
            validate(next_c)
            if live.get() and not messagebox.askyesno('Enable real-money execution?',
                    'Only proceed after paper testing and reviewing the wallet and limits. '
                    'The bot can lose funds and exits can fail. Enable live mode?'):
                return
            tmp = path.with_suffix('.tmp')
            tmp.write_bytes(protect(json.dumps(next_c).encode()))
            tmp.replace(path)
            startup_shortcut(auto.get())
            (root / 'STOP_HOST').unlink(missing_ok=True)
            # Existing supervisor reloads credentials only after a child exits. Do not kill positions.
            subprocess.Popen([str(Path(sys.executable).with_name('pythonw.exe')),
                              str(APP / 'windows_host.py'), 'run'], cwd=APP)
            c.update(next_c)
            messagebox.showinfo('Saved', 'Host started if not already running. Pair in Telegram and send /status. '
                                'Changes to a running host take effect after stopping and starting it. '
                                'Do not stop while positions remain open.')
        except Exception as e:
            messagebox.showerror('Setup blocked', str(e) if isinstance(e, ValueError) else
                                 'Could not save or start. Check Python installation and Windows folder permissions.')

    def stop():
        if messagebox.askyesno('Stop host?', 'This stops ALL monitoring and protective exits. '
                              'Close positions and check confirmations first. Stop now?'):
            (root / 'STOP_HOST').touch()
            messagebox.showinfo('Stop requested', 'Host will stop within a few seconds. '
                                'Use Save and start to restart it. Startup remains blocked until then.')
    tk.Button(window, text='Save and start', command=save, width=28).pack(pady=6)
    tk.Button(window, text='Stop host', command=stop, width=28).pack()
    window.mainloop()


def run(root):
    from bot import worker_lock
    root.mkdir(parents=True, exist_ok=True)
    with worker_lock(root / 'windows-host.lock'):
        if (root / 'STOP_HOST').exists():
            return
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        kernel.SetThreadExecutionState.restype = wintypes.DWORD
        if not kernel.SetThreadExecutionState(0x80000001):
            raise RuntimeError('Could not inhibit automatic sleep')
        child = None
        try:
            while not (root / 'STOP_HOST').exists():
                env = child_environment(read_settings(root), root)
                # Runtime messages can contain identifiers. Retain only a small rotating local log.
                log = root / 'host.log'
                if log.exists() and log.stat().st_size > 1_000_000:
                    log.replace(root / 'host.previous.log')
                with log.open('ab') as output:
                    child = subprocess.Popen([sys.executable, '-u', str(APP / 'cloud.py')],
                                             cwd=APP, env=env, stdout=output, stderr=output,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                    while child.poll() is None and not (root / 'STOP_HOST').exists():
                        time.sleep(1)
                if (root / 'STOP_HOST').exists():
                    break
                for _ in range(15):
                    if (root / 'STOP_HOST').exists():
                        break
                    time.sleep(1)
        finally:
            if child is not None and child.poll() is None:
                # Windows termination is abrupt; pending transaction ledger survives restart.
                child.terminate()
                child.wait(timeout=20)
            kernel.SetThreadExecutionState(0x80000000)


if __name__ == '__main__':
    try:
        root = data_root()
        if len(sys.argv) != 2 or sys.argv[1] not in ('setup', 'run'):
            raise ValueError('Use setup or run')
        setup(root) if sys.argv[1] == 'setup' else run(root)
    except Exception:
        # Never include credentials, provider URLs, or raw exception text in unattended logs.
        if os.name == 'nt':
            root = data_root()
            root.mkdir(parents=True, exist_ok=True)
            (root / 'launcher-error.txt').write_text('Host could not start. It may already be running. '
                'Open setup and check Telegram /status. If offline, verify Python, credentials, '
                'and that this is the original Windows account.\n')
        raise SystemExit(1)
