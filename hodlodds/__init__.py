"""HodlOdds: pay-per-answer crash odds for Solana memecoins over x402 (USDC)."""
from .client import NETWORKS, Answer, HodlOdds, HodlOddsError

__all__ = ["HodlOdds", "HodlOddsError", "Answer", "NETWORKS"]
__version__ = "0.1.0"
