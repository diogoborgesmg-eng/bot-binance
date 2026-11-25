"""
BOT BINANCE v2 — Mini-Dólar equivalente (USDTBRL)
Roda 100% em Linux — deploy no Fly.io
Sem MT5, sem Windows, sem PC

Estratégia: MA9×MA21 + RSI + Bollinger Bands + Trailing Stop
Capital: ~R$1.000 equivalente em USDT
Stop diário: equivalente a R$100
Meta simbólica: R$300 — bot NÃO para ao atingir
"""

import os, time, logging, threading
from datetime import datetime, time as dtime

# ============================================================
# CONFIGURAÇÕES
# ============================================================

API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
PAR = os.environ.get("TRADING_PAR", "USDTBRL")
VALOR_OPERACAO_USDT = float(os.environ.get("VALOR_OPERACAO", "10"))

STOP_LOSS_PCT       = 0.0025
TRAILING_INICIO_PCT = 0.002
TRAILING_DIST_PCT   = 0.0015
PERDA_MAX_DIA_USDT  = float(os.environ.get("PERDA_MAX_DIA", "18"))
META_DIA_USDT       = float(os.environ.get("META_DIA", "55"))

HORARIO_INICIO = dtime(9, 0)
HORARIO_FIM    = dtime(23, 0)

RSI_PERIODO    = 14
RSI_COMPRA_MAX = 55
RSI_VENDA_MIN  = 45
BB_PERIODO     = 20
BB_DESVIO      = 2.0
MA_RAPIDA      = 9
MA_LENTA       = 21
TIMEFRAME      = "1m"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("BotBinance")

# ============================================================
# ESTADO GLOBAL
# ============================================================
resultado_dia  = 0.0
posicao_aberta = None
status_bot     = "iniciando"

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
    df["range"]    = (df["high"] - df["low"]) / df["close"]
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
            "tipo": sinal, "entrada": preco, "sl": sl,
            "pico": preco, "quantidade": quantidade,
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
        if preco_atual > p["pico"]: p["pico"] = preco_atual
        if lucro_pct >= TRAILING_INICIO_PCT:
            novo_sl = p["pico"] * (1 - TRAILING_DIST_PCT)
            if novo_sl > p["sl"]: p["sl"] = novo_sl
        if preco_atual <= p["sl"]: fechar = True
    else:
        lucro_pct = (p["entrada"] - preco_atual) / p["entrada"]
        if preco_atual < p["pico"]: p["pico"] = preco_atual
        if lucro_pct >= TRAILING_INICIO_PCT:
            novo_sl = p["pico"] * (1 + TRAILING_DIST_PCT)
            if novo_sl < p["sl"]: p["sl"] = novo_sl
        if preco_atual >= p["sl"]: fechar = True
    if fechar:
        lucro_usdt = lucro_pct * VALOR_OPERACAO_USDT
        resultado_dia += lucro_usdt
        emoji = "✅" if lucro_usdt >= 0 else "❌"
        log.info(f"{emoji} FECHOU {p['tipo'].upper()} | Lucro: ${lucro_usdt:.4f} | Dia: ${resultado_dia:.4f}")
        if resultado_dia >= META_DIA_USDT:
            log.info(f"🎯 Meta atingida! ${resultado_dia:.2f} — CONTINUANDO!")
        try:
            if p["tipo"] == "compra":
                cliente.order_market_sell(symbol=PAR, quantity=p["quantidade"])
            else:
                cliente.order_market_buy(symbol=PAR, quantity=p["quantidade"])
        except Exception as e:
            log.error(f"❌ Erro ao fechar ordem: {e}")
        posicao_aberta = None

# ============================================================
# LOOP DO BOT (roda em thread separada)
# ============================================================
def dentro_do_horario():
    agora = datetime.now().time()
    return HORARIO_INICIO <= agora <= HORARIO_FIM

def loop_bot():
    global resultado_dia, posicao_aberta, status_bot

    log.info("=" * 60)
    log.info("🤖 BOT BINANCE v2 INICIADO")
    log.info(f"   Par      : {PAR}")
    log.info(f"   Valor op : ${VALOR_OPERACAO_USDT} USDT")
    log.info(f"   Stop dia : ${PERDA_MAX_DIA_USDT} USDT")
    log.info(f"   Meta dia : ${META_DIA_USDT} USDT (simbólica)")
    log.info("=" * 60)

    cliente = None
    ultimo_dia = datetime.now().date()

    while True:
        try:
            # Reconectar se necessário
            if cliente is None:
                status_bot = "conectando"
                cliente = criar_cliente()
                if not cliente:
                    status_bot = "erro_conexao"
                    log.error("❌ Sem conexão Binance. Tentando novamente em 60s...")
                    time.sleep(60)
                    continue
                status_bot = "rodando"

            agora = datetime.now()

            # Reset diário
            if agora.date() != ultimo_dia:
                log.info(f"📅 Novo dia | Resultado anterior: ${resultado_dia:.4f}")
                resultado_dia = 0.0
                posicao_aberta = None
                ultimo_dia = agora.date()

            # Parar se atingiu perda máxima
            if resultado_dia <= -PERDA_MAX_DIA_USDT:
                status_bot = "stop_diario"
                log.warning(f"🛑 PERDA MÁXIMA: ${resultado_dia:.4f} — Aguardando próximo dia")
                time.sleep(300)
                continue

            # Fora do horário
            if not dentro_do_horario():
                status_bot = "fora_horario"
                time.sleep(60)
                continue

            status_bot = "rodando"

            # Dados do mercado
            df = obter_candles(cliente, 50)
            if df is None:
                time.sleep(10)
                continue

            df = calcular_indicadores(df)
            preco_atual = float(df.iloc[-1]["close"])

            if posicao_aberta:
                gerenciar_posicao(cliente, preco_atual)
            else:
                sinal = verificar_sinal(df)
                if sinal:
                    log.info(f"📡 Sinal: {sinal.upper()} | RSI: {df.iloc[-2]['rsi']:.1f} | Preço: {preco_atual:.6f}")
                    abrir_ordem(cliente, sinal, preco_atual)

            time.sleep(30)

        except Exception as e:
            log.error(f"⚠️ Erro no loop: {e}")
            cliente = None  # forçar reconexão
            time.sleep(30)

# ============================================================
# SERVIDOR WEB — roda no thread principal (exigido pelo Fly.io)
# ============================================================
def iniciar_servidor_web():
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = f"""Bot Binance v2 | Status: {status_bot} | Par: {PAR} | Resultado dia: ${resultado_dia:.4f} USDT | Posicao: {'aberta' if posicao_aberta else 'nenhuma'} | Hora: {datetime.now().strftime('%H:%M:%S')}"""
            msg = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg)
        def log_message(self, *args): pass

    porta = int(os.environ.get("PORT", 8080))
    log.info(f"🌐 Servidor web na porta {porta}")
    HTTPServer(("0.0.0.0", porta), Handler).serve_forever()

if __name__ == "__main__":
    # Bot roda em thread daemon
    t = threading.Thread(target=loop_bot, daemon=True)
    t.start()
    # Servidor web roda no thread principal — mantém o processo vivo
    iniciar_servidor_web()
