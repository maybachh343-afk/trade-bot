# Run SENTINEL on your Windows PC

Your PC is the host; Telegram is your remote control. You can use mobile data or any Wi-Fi while away. No port forwarding, public IP, remote desktop, or paid hosting is required by this launcher. RPC/data services may have their own costs.

## PC checkpoint: first setup

1. Download this repository using **Code > Download ZIP**. Extract it to a permanent folder, such as `C:\Users\YOURNAME\Sentinel`. Do not run inside the ZIP or a temporary download preview.
2. Install **64-bit Python 3.12** from https://www.python.org/downloads/windows/ if it is not already installed. Keep the Python launcher and Tcl/Tk components selected. This launcher specifically uses 3.12 even if another Python version is present.
3. Open the extracted `sentinel-meme` folder and double-click **SETUP-WINDOWS.cmd**. It creates an isolated Python environment and installs the pinned dependencies, then opens setup.
4. Create a bot in Telegram using the verified **@BotFather**, `/newbot`, and your chosen name/username. Paste its token **only into the local setup window**, never into GitHub or a chat with the trading bot.
5. Enter your Solana HTTPS RPC endpoint, matching WSS endpoint, and Jupiter API key when available. These may be left blank to connect Telegram first; trading remains blocked until the required providers and configuration work.
6. Keep LIVE unchecked. Keep automatic startup checked. Click **Save and start**.
7. Copy the `/pair ...` command displayed by setup into a private chat with your own bot. Send `/status`. Keep Telegram notifications enabled on your phone.

Do not delete or move the application folder after setup: the startup shortcut points to it. Rerun setup after moving the application. Updating Python dependencies requires closing managed positions and stopping the host first; do not rerun the installer during live operation.

## Phone operation

- `/status`, `/positions`, `/pnl`, `/wallet`, `/settings`: inspect operation.
- `/watch PUBLIC_ADDRESS`: add buyer wallets; the default strategy requires at least three watched buyers. The operator supplies these addresses; this build does not discover profitable wallets for you.
- `/dev MINT PUBLIC_ADDRESS`: supply developer attribution. Unknown developers are blocked by default. Attribution is not independently verified.
- `/resume` then its confirmation command: allow entries after configuration and worker checks pass.
- `/pause`: stop new buys while continuing exits.
- `/exit_all`: request exits and review confirmations. This does not guarantee a successful sale.
- `/halt`: stops submissions INCLUDING protective exits; use only when that is intended.

Trade notifications identify PAPER or LIVE. Paper fills are simulations, not evidence of executable profitability. Twenty to fifty trades daily is not guaranteed and is not a minimum quota. No strategy or rug screen guarantees profit or prevents all losses.

## PC availability

The launcher starts **after Windows sign-in**, not at the login screen. It continues when you lock Windows. After a reboot you must sign in again. It requests that Windows avoid automatic idle sleep while the supervisor runs; the display can turn off. Manually selecting Sleep, closing a laptop lid, power loss, logout, or losing internet can interrupt monitoring. Do not rely on stop losses while the PC is offline. No offline Telegram alert can be sent by a disconnected PC.

The supervisor restarts a crashed worker after 15 seconds. It does not detect every possible stalled process. On worker restart, new entries are paused; existing position reconciliation and exits continue when providers are available. Review `/status` and explicitly `/resume` after a restart. Pending transactions remain in the local ledger.

Open setup again with `.venv\Scripts\python.exe windows_host.py setup` to change credentials or stop the host. Stop disables monitoring and exits, so first close managed positions and verify confirmations. Saved changes require a host restart. The Stop button also blocks automatic startup until **Save and start** is clicked again. Uncheck startup and save to remove the startup shortcut.

## Storage and live mode

Data lives under `%LOCALAPPDATA%\SentinelTradingBot`, outside the repository. Credentials and the wallet encryption password are protected with Windows current-user DPAPI. The trading wallet is separately encrypted by the existing vault. Processes running as your Windows user can access these credentials; this is not a hardware wallet or protection against malware.

Do not delete the data folder, credential file, wallet vault, or ledgers. Windows credential protection is tied to this account and computer; copying this folder to another PC is not a sufficient wallet recovery backup. Before funding, arrange an offline wallet recovery backup using the existing recovery utility and preserve the ledgers. Do not send recovery material through Telegram.

Initial operation is PAPER and PAUSED. After validating provider access, strategy configuration, simulated operation, recovery, and limits, live mode can be explicitly selected in local setup. It requires the dedicated public wallet address from `/wallet`, both RPC endpoints, and the Jupiter key. The existing engine independently checks that address and other risk gates. Close all positions and resolve pending orders before changing modes. The launcher does not import your main wallet or automatically fund the dedicated wallet.

## Validation status

Configuration/environment tests run on Linux. Windows DPAPI, the startup shortcut, idle-sleep handling, Telegram delivery, restart recovery, and funded swaps must still be tested on the actual Windows PC. This is an unaudited prototype, not a completed production trading service.

Windows references: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate and https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
