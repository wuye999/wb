"""Call the business handlers directly in legacy protocol regression fixtures."""
from unittest.mock import patch

from sellerops.cli import main


def run_business_cli(argv):
    # Legacy tests exercise transport and receipts. TestStep2 exercises the public plan gate.
    with patch.object(main, 'execute', main.dispatch):
        return main.main(argv)
