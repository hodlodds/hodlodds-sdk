"""HodlOdds: pay-per-answer crash odds for Solana memecoins over x402 (USDC on Solana or Base)."""
from .client import NETWORKS, Answer, HodlOdds, HodlOddsError, PendingPayment, Watch, load_pending, save_pending

__all__ = ["HodlOdds", "HodlOddsError", "Answer", "NETWORKS", "PendingPayment", "save_pending", "load_pending", "Watch"]
__version__ = "0.3.1"
