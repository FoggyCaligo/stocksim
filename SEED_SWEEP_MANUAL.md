# seed_sweep.py CLI manual

`seed_sweep.py`는 stocksim의 통합 seed-sweep 백테스트 실행기다. 실험마다 Python 파일을 새로 만들지 않고, 아래 옵션을 조합해 조건식·목표가·보유기간·손절·자금배분을 바꾼다.

가장 빠른 내장 도움말은 다음 명령으로 볼 수 있다.

```bash
python seed_sweep.py -h
```

## 1. 실행 범위 / seed / 병렬처리

| 옵션 | 의미 | 기본값 |
|---|---|---:|
| `--start YYYY-MM-DD` | 백테스트 신호 시작일. 필수 | 없음 |
| `--end YYYY-MM-DD` | 백테스트 신호 종료일. 필수 | 없음 |
| `--seed-start N` | 첫 random seed | `1` |
| `--seed-end N` | 마지막 random seed | `100` |
| `--workers N` | 병렬 worker 수 | CPU 기준 최대 4 |
| `--markets KOSPI,KOSDAQ` | 대상 시장 | `KOSPI,KOSDAQ` |

## 2. 자금 / 포지션

| 옵션 | 의미 | 기본값 |
|---|---|---:|
| `--initial-capital KRW` | 초기 자본 | `1000000` |
| `--daily-buy-count N` | `equal-cash`에서는 최대 동시보유 슬롯 수이자 하루 최대 대기 후보 수 | `5` |
| `--allocation-mode equal-cash` | 현재 현금을 빈 슬롯 수로 나눠 매수 | 기본 |
| `--allocation-mode fixed-position` | 종목당 고정 상한으로 매수 | 선택 |
| `--position-size KRW` | `fixed-position`에서 종목당 매수 상한 | 없음 |
| `--no-reentry` | 이미 보유 중인 종목의 중복 진입 금지. 매도 후 나중에 다시 진입하는 것은 가능 | 꺼짐 |

`equal-cash` 예: 현금 1,000,000원, 빈 슬롯 5개면 첫 슬롯 예산은 약 200,000원이다. 매도대금은 현금으로 돌아오며 이후 새 매수에 다시 사용된다.

## 3. 기본 종목 필터

지정하지 않은 필터는 적용하지 않는다.

| 옵션 | 의미 |
|---|---|
| `--price-min X` / `--price-max X` | 신호일 종가 범위 |
| `--market-cap-min X` / `--market-cap-max X` | 시가총액 범위(원) |
| `--daily-return-min X` / `--daily-return-max X` | 신호일 등락률 범위(%) |
| `--trading-value-min X` / `--trading-value-max X` | 신호일 거래대금 범위(원) |

예:

```bash
--price-min 0 \
--price-max 100000 \
--market-cap-min 300000000000 \
--daily-return-min -7 \
--daily-return-max -1 \
--trading-value-min 300000000 \
--trading-value-max 100000000000
```

## 4. 이동평균 배열

```bash
--ma-long 240 \
--ma-mid 120 \
--ma-short 60
```

세 옵션은 함께 사용해야 하며 기간은 `long > mid > short`여야 한다. 실제 통과 조건은 다음과 같다.

```text
MA240 < MA120 < MA60
```

즉 장기선보다 중기선이, 중기선보다 단기선이 높은 정배열을 요구한다.

## 5. Envelope 기본 설정

```bash
--envelope-period 20 \
--envelope-percent 6
```

의미:

```text
중앙선 = 20일 이동평균
상단선 = 중앙선 × 1.06
하단선 = 중앙선 × 0.94
```

`--envelope-filter` 또는 Envelope 기반 목표가를 사용하려면 period와 percent를 함께 지정해야 한다.

## 6. Envelope 진입 필터

### `below`

```bash
--envelope-filter below
```

신호일 **종가가 Envelope 하단선 이하**인 종목을 통과시킨다.

```text
close <= lower band
```

### `below-lower-range`

```bash
--envelope-filter below-lower-range \
--envelope-below-percent 6
```

신호일 **저가가 하단선 아래에 있으면서, 하단선보다 6% 이상 더 멀리 내려가지는 않은 종목**을 통과시킨다.

```text
lower band × 0.94 <= low <= lower band
```

예: 하단선 10,000원이면 저가 9,400~10,000원.

### `recent-low-cross`

```bash
--envelope-filter recent-low-cross \
--envelope-cross-lookback-days 3
```

최근 지정 영업일 안에 **저가 기준 Envelope 하단 하향돌파**가 발생한 종목을 통과시킨다. 하루의 하향돌파 판정은 다음과 같다.

```text
전일 저가 > 전일 Envelope 하단선
AND
당일 저가 <= 당일 Envelope 하단선
```

따라서 예전에 사용하던 “최근 3일 내 하단선 하향돌파” 설정은 바로 이 두 줄이다.

### `recent-close-cross`

```bash
--envelope-filter recent-close-cross \
--envelope-cross-lookback-days 3
```

`recent-low-cross`와 같지만 저가 대신 종가로 하향돌파를 판정한다.

```text
전일 종가 > 전일 Envelope 하단선
AND
당일 종가 <= 당일 Envelope 하단선
```

## 7. 목표가

### `envelope-center`

```bash
--target-mode envelope-center
```

Envelope 중앙선을 목표가로 사용한다.

### `envelope-mid-upper`

```bash
--target-mode envelope-mid-upper
```

Envelope 중앙선과 상단선의 중간 가격을 목표가로 사용한다.

### `fixed-return`

```bash
--target-mode fixed-return \
--fixed-take-profit 0.05
```

실제 진입가에서 +5%를 목표가로 사용한다.

### `none`

```bash
--target-mode none
```

목표가를 사용하지 않는다.

## 8. Envelope 목표가 기준 시점

```bash
--target-basis signal-day
```

신호일 Envelope 값을 사용한다.

```bash
--target-basis entry-day-open
```

다음 거래일 시가 시점에 알 수 있는 Envelope 값을 사용한다. 미래 종가 look-ahead를 피하기 위해 Envelope 기간이 N이면 다음과 같이 계산한다.

```text
(직전 N-1개 종가의 합 + 매수일 시가) / N
```

현재 실험에서 `--envelope-period 20`이면 직전 19개 종가 + 매수일 시가를 사용한다.

## 9. 목표수익률 범위 필터

```bash
--planned-target-return-min 8 \
--planned-target-return-max 16
```

실제 다음날 진입가 기준 목표가 거리가 8~16%인 경우만 진입한다.

```text
planned target % = (target / actual entry - 1) × 100
```

## 10. 보유기간

### 최근 목표가 터치 기반

```bash
--hold-mode recent-target-touch \
--hold-period-ratio 1.5 \
--max-hold-days 20
```

과거에 현재 목표가를 가장 최근 터치한 시점까지의 영업일 수를 구하고, 그 기간에 배수를 곱해 계획 보유기간으로 사용한다.

예: 최근 터치가 6영업일 전이고 ratio가 1.5면 계획 보유기간은 `ceil(6 × 1.5) = 9일`이다. `--max-hold-days 20`이면 20영업일을 넘지 않는다.

`--touch-lookback-days N`을 추가하면 최근 터치 탐색 범위를 제한할 수 있다.

### 고정 보유

```bash
--hold-mode fixed \
--fixed-hold-days 10
```

최대 10영업일 보유한다.

### 보유기간 종료 없음

```bash
--hold-mode none
```

보유기간 만료 청산을 사용하지 않는다.

## 11. 손절

### 고정 손절

```bash
--stop-mode fixed-pct \
--fixed-stop-loss-pct 0.03
```

진입가 대비 -3% 손절이다. 손절률은 퍼센트 숫자 `3`이 아니라 소수 `0.03`으로 입력한다.

### 여러 고정 손절 비교

```bash
--compare-fixed-stops 0.02,0.03,0.04,0.08
```

-2%, -3%, -4%, -8%를 한 번에 각각 실행하고 `comparison.csv` / `comparison.json`을 만든다. 이 옵션을 쓰면 개별 `--stop-mode fixed-pct`를 직접 지정할 필요가 없다.

### 목표거리 기반 손절

```bash
--stop-mode target-gap \
--stop-gap-ratio 0.5
```

진입가에서 목표가까지 거리의 50%만큼 아래를 손절가로 사용한다.

```text
stop = entry - 0.5 × (target - entry)
```

### 손절 없음

```bash
--stop-mode none
```

## 12. 거래비용 / 슬리피지

| 옵션 | 의미 | 기본값 |
|---|---|---:|
| `--commission-rate X` | 매수·매도 각각 수수료율 | `0.00015` (0.015%) |
| `--sell-tax-rate X` | 역사적 기본 매도세에 추가로 더할 세율 | `0` |
| `--slippage-bps X` | 진입/청산 슬리피지(bps) | `0` |

기본 매도세는 거래일별 역사적 키움 세율을 사용한다.

## 13. 캐시 / 출력

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `--cache-dir PATH` | 준비 데이터 캐시 위치 | `.cache/stocksim` |
| `--output-dir PATH` | 결과 저장 위치 | `results/seed_sweep` |
| `--rebuild-prepared-cache` | 기존 준비 캐시를 무시하고 다시 계산 | 꺼짐 |
| `--no-trade-diagnostics` | `trade_diagnostics.csv` 생성 생략 | 꺼짐 |

일반 단일 실행 출력:

```text
seed_results.csv
seed_summary.json
trade_diagnostics.csv
```

`--compare-fixed-stops` 실행 시 각 손절별 하위 폴더와 함께 루트에 다음 파일도 생성된다.

```text
comparison.csv
comparison.json
```

## 14. 현재 스타일 예시: 최근 하향돌파

```bash
python seed_sweep.py \
  --start 2020-01-01 \
  --end 2025-12-31 \
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
  --hold-period-ratio 1.5 \
  --max-hold-days 20 \
  --allocation-mode equal-cash \
  --compare-fixed-stops 0.02,0.03,0.04,0.08 \
  --initial-capital 1000000 \
  --no-reentry
```

## 15. 현재 스타일 예시: 하단선 아래 6% 범위

위 명령에서 다음 부분만 바꾼다.

```bash
--envelope-filter below-lower-range \
--envelope-below-percent 6
```

`recent-low-cross`는 “최근에 하단선을 뚫었는가”를 보고, `below-lower-range`는 “현재 저가가 하단선 아래 0~6% 범위인가”를 본다는 차이가 있다.
