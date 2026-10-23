import os
from pathlib import Path
import unittest
from unittest.mock import patch
from windows_host import child_environment, validate


class WindowsHostConfigTests(unittest.TestCase):
    def settings(self):
        return dict(TELEGRAM_BOT_TOKEN='123:example', BOT_MODE='paper',
                    PAIRING_CODE='pair', WALLET_VAULT_KEY='vault')

    def test_inherited_live_gates_and_provider_secrets_are_overridden(self):
        with patch.dict(os.environ, {'BOT_MODE': 'live', 'ENABLE_LIVE_TRADING': 'YES',
                                     'JUPITER_API_KEY': 'wrong', 'SOLANA_RPC_URL': 'wrong'}):
            e = child_environment(self.settings(), Path('state'))
        self.assertEqual(e['BOT_MODE'], 'paper')
        self.assertEqual(e['ENABLE_LIVE_TRADING'], 'NO')
        self.assertEqual(e['JUPITER_API_KEY'], '')
        self.assertEqual(e['SOLANA_RPC_URL'], '')
        self.assertEqual(e['DATA_DIR'], 'state')

    def test_telegram_only_setup_is_allowed_but_stays_paper(self):
        validate(self.settings())

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            validate({**self.settings(), 'BOT_MODE': 'LIVE'})

    def test_insecure_endpoints_rejected(self):
        for key, value in [('SOLANA_RPC_URL', 'http://example.com'), ('SOLANA_WS_URL', 'ws://example.com')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**self.settings(), key: value})

    def test_live_requires_wallet_address(self):
        with self.assertRaises(ValueError):
            validate({**self.settings(), 'BOT_MODE': 'live'})

    def test_live_requires_all_providers(self):
        with self.assertRaises(ValueError):
            validate({**self.settings(), 'BOT_MODE': 'live',
                      'EXPECTED_WALLET': 'So11111111111111111111111111111111111111112'})


if __name__ == '__main__':
    unittest.main()
