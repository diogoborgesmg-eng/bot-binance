"""
BOT BINANCE v3 — BTCUSDT
Roda 100% em Linux — deploy no Fly.io
"""

import os, time, logging, threading, urllib.request
from datetime import datetime, time as dtime

API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
PAR = os.environ.get("TRADING_PAR", "BTCUSDT")
VALOR_OPERACAO_USDT = float(os.environ.get("VALOR_OPERACAO", "10"))
STOP_LOSS_PCT       = 0.0025
TRAILING_INICIO_PCT = 0.002
TRAILING_DIST_PCT   = 0.0015
PERDA_MAX_DIA_USDT  = float(os.environ.get("PERDA_MAX_DIA", "18"))
META_DIA_USDT       = float(os.environ.get("META_DIA", "55"))
HORARIO_INICIO = dtime(0, 0)
HORARIO_FIM    = dtime(23, 59)
RSI_PERIODO    = 14
RSI_COMPRA_MAX = 65
RSI_VENDA_MIN  = 35
BB_PERIODO     = 20
BB_DE
