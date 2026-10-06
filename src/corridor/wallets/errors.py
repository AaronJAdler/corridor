"""What the wallets module refuses."""

from corridor.platform.errors import NotFound


class WalletNotFound(NotFound):
    """The user has no wallet in a supported asset: they were never provisioned."""

    code = "wallet_not_found"
    title = "Wallet not found"

    def __init__(self, asset: str) -> None:
        super().__init__(f"There is no {asset} wallet.")
