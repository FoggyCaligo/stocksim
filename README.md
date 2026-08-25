# stocksim

Backtesting utilities for configurable swing-trading strategies on Korean equities.

조건식이 사진과 같을 때, 나온 종목들 중 랜덤하게 5종목을 매일 매수하고, +5%이상 수익이 7영업일 이내 나오면 익절, 4영업일 내 연속하락 2봉 발생 시 손절, 7일 이상 지난 종목 손절, 이렇게 거래한다면, 시작일과 종료일이 주어졌을 때, 매일 거래 시 나오는 수익률을 계산하는 파이썬 코드


python backtest.py \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --daily-buy-count 5 \
  --daily-return-min -7 \
  --daily-return-max -1 \
  --short-ma 20 \
  --long-ma 60 \
  --take-profit 0.05 \
  --early-stop-window 4 \
  --consecutive-down-days 2 \
  --max-hold-days 7 \
  --seed 42