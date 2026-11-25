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

resultado_dia  = 0.0
posicao_aberta = None
status_bot     = "iniciando"
ip_publico     = "descobrindo..."

def descobrir_ip():
    global ip_publico
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                ip_publico = ip
                log.info(f"🌐 IP público do servidor: {ip}")
                return ip
        except:
            continue
    ip_publico = "desconhecido"
    return "desconhecido"

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
        log.info(f"✅ {sinal.upper()} | Preço: {preco:.2f} | Qtd: {quantidade} | SL: {sl:.2f}")
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
        try:
            if p["tipo"] == "compra":
                cliente.order_market_sell(symbol=PAR, quantity=p["quantidade"])
            else:
                cliente.order_market_buy(symbol=PAR, quantity=p["quantidade"])
        except Exception as e:
            log.error(f"❌ Erro ao fechar ordem: {e}")
        posicao_aberta = None

def dentro_do_horario():
    agora = datetime.now().time()
    return HORARIO_INICIO <= agora <= HORARIO_FIM

def loop_bot():
    global resultado_dia, posicao_aberta, status_bot
    log.info("=" * 60)
    log.info("🤖 BOT BINANCE v3 INICIADO")
    log.info(f"   Par      : {PAR}")
    log.info(f"   Valor op : ${VALOR_OPERACAO_USDT} USDT")
    log.info(f"   Stop dia : ${PERDA_MAX_DIA_USDT} USDT")
    log.info(f"   Meta dia : ${META_DIA_USDT} USDT (simbólica)")
    log.info("=" * 60)
    cliente = None
    ultimo_dia = datetime.now().date()
    while True:
        try:
            if cliente is None:
                status_bot = "conectando"
                cliente = criar_cliente()
                if not cliente:
                    status_bot = "erro_conexao"
                    log.error("❌ Sem conexão. Tentando em 60s...")
                    time.sleep(60)
                    continue
                status_bot = "rodando"
            agora = datetime.now()
            if agora.date() != ultimo_dia:
                log.info(f"📅 Novo dia | Anterior: ${resultado_dia:.4f}")
                resultado_dia = 0.0
                posicao_aberta = None
                ultimo_dia = agora.date()
            if resultado_dia <= -PERDA_MAX_DIA_USDT:
                status_bot = "stop_diario"
                log.warning(f"🛑 PERDA MÁXIMA: ${resultado_dia:.4f}")
                time.sleep(300)
                continue
            if not dentro_do_horario():
                status_bot = "fora_horario"
                time.sleep(60)
                continue
            status_bot = "rodando"
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
                    log.info(f"📡 Sinal: {sinal.upper()} | RSI: {df.iloc[-2]['rsi']:.1f} | Preço: {preco_atual:.2f}")
                    abrir_ordem(cliente, sinal, preco_atual)
            time.sleep(30)
        except Exception as e:
            log.error(f"⚠️ Erro no loop: {e}")
            cliente = None
            time.sleep(30)

def iniciar_servidor_web():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = (
                f"Bot Binance v3 | {status_bot} | Par: {PAR} | "
                f"Dia: ${resultado_dia:.4f} | Posicao: {'aberta' if posicao_aberta else 'nenhuma'} | "
                f"IP: {ip_publico} | {datetime.now().strftime('%H:%M:%S')}"
            )
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
    threading.Thread(target=descobrir_ip, daemon=True).start()
    threading.Thread(target=loop_bot, daemon=True).start()
    iniciar_servidor_web()
