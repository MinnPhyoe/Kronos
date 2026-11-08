"""The Kronos wrapper against the real Kronos code (a tiny untrained model saved like a checkpoint).
Skipped when torch is not installed."""
import sys
from pathlib import Path

import pytest
from conftest import flat_candles, ts

torch = pytest.importorskip("torch")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # the Kronos repo: model/


def test_real_kronos_checkpoint_forecasts(tmp_path):
    from model import Kronos, KronosTokenizer

    from forexapp.config import Config
    from forexapp.models.kronos_service import KronosForecaster

    torch.manual_seed(0)
    KronosTokenizer(d_in=6, d_model=32, n_heads=2, ff_dim=64, n_enc_layers=1, n_dec_layers=1, ffn_dropout_p=0.0,
                    attn_dropout_p=0.0, resid_dropout_p=0.0, s1_bits=6, s2_bits=6, beta=0.05, gamma0=1.0, gamma=1.1,
                    zeta=0.05, group_size=4).save_pretrained(tmp_path / "tok")
    Kronos(s1_bits=6, s2_bits=6, n_layers=1, d_model=32, n_heads=2, ff_dim=64, ffn_dropout_p=0.0, attn_dropout_p=0.0,
           resid_dropout_p=0.0, token_dropout_p=0.0, learn_te=True).save_pretrained(tmp_path / "exp1" / "basemodel" / "best_model")
    cfg = Config()
    cfg.kronos_checkpoint = str(tmp_path / "exp1" / "basemodel" / "best_model")
    cfg.kronos_tokenizer_checkpoint = str(tmp_path / "tok")
    cfg.kronos_device = "cpu"
    cfg.lookback = 128
    k = KronosForecaster(cfg)
    assert k.name == "Kronos fine-tuned (exp1)"
    start = ts("2026-11-13 21:55") - 199 * 300                      # ends Friday 16:55 New York
    jobs = [(flat_candles("EURUSD", start, 200, 1.10), "EURUSD", "5m"),
            (flat_candles("USDJPY", start, 200, 150.0, wiggle=0.03), "USDJPY", "5m")]
    fs = k.forecast_many(jobs, 6, 8)
    for f in fs:
        assert f.paths.shape == (8, 6, 4)
        assert f.times[0] == ts("2026-11-15 22:00")                   # skips the weekend close
        assert (f.paths[:, :, 1] >= f.paths[:, :, 2]).all()
        assert len(f.band()) == 6 and 0.0 <= f.direction_prob() <= 1.0
    assert abs(fs[1].last_close - 150.0) < 5                          # each pair keeps its own scale
