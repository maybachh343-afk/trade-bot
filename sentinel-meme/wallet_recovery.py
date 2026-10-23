"""Run ONLY in your own hosting provider's private interactive shell."""
import os, sys
from pathlib import Path
from cloud import provision_wallet
if __name__=='__main__':
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit('Recovery requires a private interactive terminal. Never run as a deployment/start command.')
    root=Path(os.environ.get('DATA_DIR','/var/data'))
    if not (root/'wallet.enc').exists():raise SystemExit('No existing vault. Recovery cannot create one.')
    if input('Private key will be shown in this terminal. Type RECOVER to continue: ')!='RECOVER':raise SystemExit(0)
    key=provision_wallet(root,os.environ.get('WALLET_VAULT_KEY',''))
    print('Public address:',key.pubkey())
    print('Solana private key (base58); never paste into Telegram or ChatGPT:')
    print(str(key))
