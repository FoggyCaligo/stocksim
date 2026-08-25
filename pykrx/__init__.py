"""Minimal local compatibility layer used by stocksim.

The upstream pykrx all-ticker endpoint now depends on KRX authentication.
stocksim only needs three stock-data functions, so they are provided locally
from the public FinanceData/marcap historical dataset.
"""

from . import stock

__all__ = ["stock"]
