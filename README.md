# stocksim

Backtesting utilities for configurable swing-trading strategies on Korean equities.

## Seed sweep

실험용 seed sweep는 `seed_sweep.py` 하나만 사용한다.

- 기본값은 1~100 seed.
- 가격/시총/등락률/거래대금/이평/Envelope 조건은 명령어에 넣은 것만 적용된다.
- Envelope 조건은 `below`, `recent-low-cross`, `recent-close-cross` 중 선택할 수 있다.
- 목표가는 Envelope 중앙선, 중앙~상단 중간, 고정 수익률 중 선택할 수 있다.
- Envelope 목표는 신호일 값(`signal-day`) 또는 매수일 시가 시점 값(`entry-day-open`)을 선택할 수 있다.
- `entry-day-open`은 미래 종가를 보지 않도록 직전 N-1개 종가와 매수일 시가로 Envelope SMA를 계산한다.
- 목표수익률 범위 필터는 실제 다음날 진입가 기준으로 적용한다.
- 보유기간은 고정일수 또는 과거 목표가 최근 터치 시점 기반으로 계산할 수 있고 배수/최대일수를 줄 수 있다.
- 손절은 목표거리 비율 또는 고정 % 중 선택할 수 있다.
- `--compare-fixed-stops 0.03,0.04`처럼 여러 고정 손절을 한 번에 비교할 수도 있다.
- 자금배분은 기본 `equal-cash`이며, `--daily-buy-count`를 최대 동시보유 슬롯 수로 사용한다. 빈 슬롯 수로 보유현금을 균등분배해 매수하고, 매도대금은 다음 매수부터 다시 사용할 수 있다.
- 이전처럼 종목당 고정 금액을 쓰려면 `--allocation-mode fixed-position --position-size ...`를 사용한다.
- 기본 수수료는 매수/매도 각각 0.015%, 매도세는 거래일 기준 역사적 키움 세율을 적용한다.
- `seed_results.csv`, `seed_summary.json`, `trade_diagnostics.csv`를 출력한다.

### 현재 실험 예시

최근 3영업일 내 **저가 기준 Envelope 하단 하향돌파**, **매수일 시가 시점 Envelope 중앙선**을 목표가로 사용, 목표수익률 8~16%, 과거 목표가 터치기간의 2배를 보유하되 최대 20영업일, 보유현금 균등분배, 고정 -3%/-4% 손절 비교:

```bash
python seed_sweep.py \
  --start 2022-01-01 \
  --end 2025-07-31 \
  --workers 4 \
  --daily-buy-count 5 \
  --price-min 0 \
  --price-max 100000 \
  --market-cap-min 300000000000 \
  --daily-return-min -7 \
  --daily-return-max -1 \
  --trading-value-min 300000000 \
  --trading-value-max 100000000000 \
  --ma-long 240 \
  --ma-mid 120 \
  --ma-short 60 \
  --envelope-period 20 \
  --envelope-percent 6 \
  --envelope-filter recent-low-cross \
  --envelope-cross-lookback-days 3 \
  --target-mode envelope-center \
  --target-basis entry-day-open \
  --planned-target-return-min 8 \
  --planned-target-return-max 16 \
  --hold-mode recent-target-touch \
  --hold-period-ratio 2 \
  --max-hold-days 20 \
  --allocation-mode equal-cash \
  --compare-fixed-stops 0.03,0.04 \
  --initial-capital 1000000 \
  --no-reentry
```

설정을 바꾸면 같은 파일로 다른 실험을 수행할 수 있다. 앞으로는 실험마다 새 Python 파일을 만들지 않고 같은 `seed_sweep.py`에 명령어만 바꿔서 사용한다.

## Legacy single-run backtest

기존 `backtest.py`는 단일 seed/기존 전략 재현용으로 유지한다.