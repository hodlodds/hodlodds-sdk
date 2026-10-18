"""HodlOdds: pay-per-answer crash odds for Solana memecoins over x402 (USDC on Solana or Base)."""
from .client import NETWORKS, Answer, HodlOdds, HodlOddsError, PendingPayment, load_pending, save_pending

__all__ = ["HodlOdds", "HodlOddsError", "Answer", "NETWORKS", "PendingPayment", "save_pending", "load_pending"]
__version__ = "0.2.0"
