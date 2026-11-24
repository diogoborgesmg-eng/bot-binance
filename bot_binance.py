"""
BOT BINANCE v1 — Mini-Dólar equivalente (BTCUSDT ou USDTBRL)
Roda 100% em Linux — deploy no Render Free Tier
Sem MT5, sem Windows, sem PC

Estratégia: MA9×MA21 + RSI + Bollinger Bands + Trailing Stop
Capital: ~R$1.000 equivalente em USDT
Stop diário: equivalente a R$100
Meta simbólica: R$300 — bot NÃO para ao atingir

SETUP:
  1. Crie conta na Binance (binance.com)
  2. Gere API Key em: Conta → API Management
      - Habilitar: "Enable Spot & Margin Trading"
      - NÃO habilitar saque (segurança)
  3. Configure variáveis de ambiente no Render
  4. Deploy
"""

import os, time, logging, threading
from datetime import datetime, time as dtime
from pathlib import Path

# ============================================================
# CONFIGURAÇÕES
# ============================================================

# API Binance — configure no Render como variáveis de ambiente
API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# Par de trading (equivalente ao mini-dólar)
# USDTBRL = dólar direto em reais (mais próximo do WDO)
# BTCUSDT = bitcoin em dólar (mais liquidez)
PAR = os.environ.get("TRADING_PAR", "USDTBRL")

# Valor por operação em USDT (~R$1.000 ÷ cotação dólar)
VALOR_OPERACAO_USDT = float(os.environ.get("VALOR_OPERACAO", "10"))  # ~R$55 por operação inicial

# Gestão de risco (em USDT)
STOP_LOSS_PCT      = 0.0025   # 0.25% de stop loss
TRAILING_INICIO_PCT = 0.002   # ativa trailing após 0.2% de lucro
TRAILING_DIST_PCT   = 0.0015  # mantém 0.15% do pico
PERDA_MAX_DIA_USDT = float(os.environ.get("PERDA_MAX_DIA", "18"))   # ~R$100
META_DIA_USDT      = float(os.environ.get("META_DIA", "55"))         # ~R$300 simbólica

# Horário (UTC-3 = Brasil)
HORARIO_INICIO = dtime(9, 0)
HORARIO_FIM    = dtime(23, 0)   # Binance opera 24h, mas limitamos

# Indicadores
RSI_PERIODO    = 14
RSI_COMPRA_MAX = 55
RSI_VENDA_MIN  = 45
BB_PERIODO     = 20
BB_DESVIO      = 2.0
MA_RAPIDA      = 9
MA_LENTA       = 21
TIMEFRAME      = "1m"   # candles de 1 minuto

# Logs
Path("logs").mkdir(exist_ok=True)
LOG_FILE = f"logs/bot_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("BotBinance")

# ============================================================
# ESTADO GLOBAL
# ============================================================
resultado_dia = 0.0
posicao_aberta = None

# ============================================================
# CONEXÃO BINANCE
# ============================================================
def criar_cliente():
    try:
        from binance.client import Client
        cliente = Client(API_KEY, API_SECRET)
        info = cliente.get_account()
        log.info(f"✅ Binance conectada | Conta: {info['accountType']}")
        return cliente
    except Exception as e:
        log.error(f"❌ Erro ao conectar Binance: {e}")
        return None

# ============================================================
# DADOS DE MERCADO
# ============================================================
def obter_candles(cliente, n=50):
    import pandas as pd
    try:
        klines = cliente.get_klines(symbol=PAR, interval=TIMEFRAME, limit=n)
        df = pd.DataFrame(klines, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_vol","trades","taker_buy_base",
            "taker_buy_quote","ignore"
        ])
        for col in ["open","high","low","close"]:
            df[col] = df[col].astype(float)
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        return df
    except Exception as e:
        log.error(f"❌ Erro ao obter candles: {e}")
        return None

# ============================================================
# INDICADORES
# ============================================================
def calcular_indicadores(df):
    df = df.copy()
    df["ma9"]  = df["close"].rolling(MA_RAPIDA).mean()
    df["ma21"] = df["close"].rolling(MA_LENTA).mean()

    delta = df["close"].diff()
    mg = delta.where(delta > 0, 0.0).rolling(RSI_PERIODO).mean()
    mp = (-delta.where(delta < 0, 0.0)).rolling(RSI_PERIODO).mean()
    df["rsi"] = 100 - (100 / (1 + (mg / mp)))

    df["bb_mid"]   = df["close"].rolling(BB_PERIODO).mean()
    df["bb_std"]   = df["close"].rolling(BB_PERIODO).std()
    df["bb_upper"] = df["bb_mid"] + BB_DESVIO * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_DESVIO * df["bb_std"]

    df["range"] = (df["high"] - df["low"]) / df["close"]  # range percentual
    return df

# ============================================================
# VERIFICAR SINAL
# ============================================================
def verificar_sinal(df):
    if len(df) < 25:
        return None

    c  = df.iloc[-2]
    c1 = df.iloc[-3]

    import math
    for col in ["ma9","ma21","rsi","bb_mid"]:
        if math.isnan(c[col]):
            return None

    cruzou_cima  = (c1["ma9"] <= c1["ma21"]) and (c["ma9"] > c["ma21"])
    cruzou_baixo = (c1["ma9"] >= c1["ma21"]) and (c["ma9"] < c["ma21"])
    vol_ok       = 0.0003 <= c["range"] <= 0.005
    rsi_compra   = c["rsi"] < RSI_COMPRA_MAX
    rsi_venda    = c["rsi"] > RSI_VENDA_MIN
    bb_compra    = c["close"] <= c["bb_mid"]
    bb_venda     = c["close"] >= c["bb_mid"]

    if cruzou_cima and rsi_compra and bb_compra and vol_ok:
        return "compra"
    if cruzou_baixo and rsi_venda and bb_venda and vol_ok:
        return "venda"
    return None

# ============================================================
# ORDENS
# ============================================================
def abrir_ordem(cliente, sinal, preco):
    global posicao_aberta
    try:
        quantidade = round(VALOR_OPERACAO_USDT / preco, 6)

        if sinal == "compra":
            ordem = cliente.order_market_buy(symbol=PAR, quantity=quantidade)
            sl = preco * (1 - STOP_LOSS_PCT)
        else:
            ordem = cliente.order_market_sell(symbol=PAR, quantity=quantidade)
            sl = preco * (1 + STOP_LOSS_PCT)

        posicao_aberta = {
            "tipo": sinal,
            "entrada": preco,
            "sl": sl,
            "pico": preco,
            "quantidade": quantidade,
            "ordem_id": ordem["orderId"]
        }
        log.info(f"✅ {sinal.upper()} | Preço: {preco:.6f} | Qtd: {quantidade} | SL: {sl:.6f}")
        return True
    except Exception as e:
        log.error(f"❌ Erro ao abrir ordem: {e}")
        return False

def gerenciar_posicao(cliente, preco_atual):
    global posicao_aberta, resultado_dia
    if posicao_aberta is None:
        return

    p = posicao_aberta
    fechar = False
    lucro_pct = 0.0

    if p["tipo"] == "compra":
        lucro_pct = (preco_atual - p["entrada"]) / p["entrada"]
        if preco_atual > p["pico"]:
            p["pico"] = preco_atual
        if lucro_pct >= TRAILING_INICIO_PCT:
            novo_sl = p["pico"] * (1 - TRAILING_DIST_PCT)
            if novo_sl > p["sl"]:
                p["sl"] = novo_sl
        if preco_atual <= p["sl"]:
            fechar = True
    else:
        lucro_pct = (p["entrada"] - preco_atual) / p["entrada"]
        if preco_atual < p["pico"]:
            p["pico"] = preco_atual
        if lucro_pct >= TRAILING_INICIO_PCT:
            novo_sl = p["pico"] * (1 + TRAILING_DIST_PCT)
            if novo_sl < p["sl"]:
                p["sl"] = novo_sl
        if preco_atual >= p["sl"]:
            fechar = True

    if fechar:
        lucro_usdt = lucro_pct * VALOR_OPERACAO_USDT
        resultado_dia += lucro_usdt
        emoji = "✅" if lucro_usdt >= 0 else "❌"
        log.info(f"{emoji} FECHOU {p['tipo'].upper()} | Lucro: ${lucro_usdt:.4f} USDT | Dia: ${resultado_dia:.4f}")

        # Marco de meta (simbólico)
        if resultado_dia >= META_DIA_USDT * 3:
            log.info(f"🚀🚀🚀 META 300%! ${resultado_dia:.2f} — CONTINUANDO!")
        elif resultado_dia >= META_DIA_USDT * 2:
            log.info(f"🔥🔥 META 200%! ${resultado_dia:.2f} — CONTINUANDO!")
        elif resultado_dia >= META_DIA_USDT:
            log.info(f"🎯 Meta diária atingida! ${resultado_dia:.2f} — CONTINUANDO!")

        try:
            quantidade = p["quantidade"]
            if p["tipo"] == "compra":
                cliente.order_market_sell(symbol=PAR, quantity=quantidade)
            else:
                cliente.order_market_buy(symbol=PAR, quantity=quantidade)
        except Exception as e:
            log.error(f"❌ Erro ao fechar ordem: {e}")

        posicao_aberta = None

# ============================================================
# LOOP PRINCIPAL
# ============================================================
def dentro_do_horario():
    agora = datetime.now().time()
    return HORARIO_INICIO <= agora <= HORARIO_FIM

def loop_principal():
    global resultado_dia, posicao_aberta

    log.info("=" * 60)
    log.info("🤖 BOT BINANCE v1 INICIADO")
    log.info(f"   Par      : {PAR}")
    log.info(f"   Valor op : ${VALOR_OPERACAO_USDT} USDT")
    log.info(f"   Stop dia : ${PERDA_MAX_DIA_USDT} USDT")
    log.info(f"   Meta dia : ${META_DIA_USDT} USDT (simbólica)")
    log.info("=" * 60)

    cliente = criar_cliente()
    if not cliente:
        log.error("Sem conexão. Encerrando.")
        return

    ultimo_dia = datetime.now().date()

    while True:
        try:
            agora = datetime.now()

            # Reset diário
            if agora.date() != ultimo_dia:
                log.info(f"📅 Novo dia | Resultado anterior: ${resultado_dia:.4f}")
                resultado_dia = 0.0
                posicao_aberta = None
                ultimo_dia = agora.date()

            # Parar se atingiu perda máxima
            if resultado_dia <= -PERDA_MAX_DIA_USDT:
                log.warning(f"🛑 PERDA MÁXIMA: ${resultado_dia:.4f} — Aguardando próximo dia")
                time.sleep(300)
                continue

            # Fora do horário
            if not dentro_do_horario():
                time.sleep(60)
                continue

            # Dados do mercado
            df = obter_candles(cliente, 50)
            if df is None:
                time.sleep(10)
                continue

            df = calcular_indicadores(df)
            preco_atual = float(df.iloc[-1]["close"])

            # Gerenciar posição ou procurar entrada
            if posicao_aberta:
                gerenciar_posicao(cliente, preco_atual)
            else:
                sinal = verificar_sinal(df)
                if sinal:
                    log.info(f"📡 Sinal: {sinal.upper()} | RSI: {df.iloc[-2]['rsi']:.1f} | Preço: {preco_atual:.6f}")
                    abrir_ordem(cliente, sinal, preco_atual)

            time.sleep(30)

        except Exception as e:
            log.error(f"⚠️  Erro no loop: {e}")
            time.sleep(30)

# ============================================================
# SERVIDOR WEB (exigido pelo Render para manter serviço ativo)
# ============================================================
def iniciar_servidor_web():
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            msg = f"Bot Binance | Par: {PAR} | Resultado dia: ${resultado_dia:.4f} USDT | Posição: {'aberta' if posicao_aberta else 'nenhuma'}".encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(msg)
        def log_message(self, *args): pass

    porta = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", porta), Handler).serve_forever()

if __name__ == "__main__":
    t = threading.Thread(target=iniciar_servidor_web, daemon=True)
    t.start()
    loop_principal()
