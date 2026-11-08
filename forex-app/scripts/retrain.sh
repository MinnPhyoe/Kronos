#!/usr/bin/env bash
# Phase 10 retraining loop (monthly or quarterly). Run on the GPU machine, from forex-app/.
#   TRAIN_END=2026-06-30 VAL_END=2026-09-30 scripts/retrain.sh
# 1. add the newest candles  2. fine-tune again  3. walk-forward backtest
# 4. you deploy the new model only if it beats the current one (this script never does).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${TRAIN_END:?set TRAIN_END (YYYY-MM-DD)}"
: "${VAL_END:?set VAL_END (YYYY-MM-DD)}"
PAIRS="${PAIRS:-EURUSD,GBPUSD,USDJPY,AUDUSD,XAUUSD}"
KRONOS_REPO="${KRONOS_REPO:-$(cd .. && pwd)}"

echo "== 1. newest candles"
python -m forexapp.data.history download --pairs "$PAIRS" --years 0.1

echo "== 2. fine-tune data and configs (time split; the test period stays untouched)"
python -m forexapp.models.finetune.prepare_data --pairs "$PAIRS" --train-end "$TRAIN_END" --val-end "$VAL_END" --chain

for cfg in models/finetune/data/*_5m_config.yaml; do
  echo "== fine-tune $cfg"
  (cd "$KRONOS_REPO" && python finetune_csv/train_sequential.py --config "$OLDPWD/$cfg")
done

LAST=$(ls -td models/checkpoints/*/ | head -1)
echo "== 3. evaluate and backtest: base vs fine-tuned ($LAST)"
mkdir -p reports
KRONOS_CHECKPOINT="$LAST/basemodel/best_model" KRONOS_TOKENIZER_CHECKPOINT="$LAST/tokenizer/best_model" \
  python -m forexapp.models.evaluate --pair EURUSD --days 90 --out reports/eval-finetuned.json
python -m forexapp.models.evaluate --pair EURUSD --days 90 --out reports/eval-base.json
KRONOS_CHECKPOINT="$LAST/basemodel/best_model" KRONOS_TOKENIZER_CHECKPOINT="$LAST/tokenizer/best_model" \
  python -m forexapp.backtest --pairs "$PAIRS" --versions rules,base,finetuned --start "$VAL_END" \
  --train-days 30 --test-days 30 --out reports/

echo
echo "== 4. Decide: deploy only if 'finetuned' beats 'base' on expectancy AND calibration in reports/backtest.md."
echo "   To deploy, set KRONOS_CHECKPOINT and KRONOS_TOKENIZER_CHECKPOINT on the model service; keep the old one as a fallback."
