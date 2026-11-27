"""
BOT BINANCE v4.0 — DOGEUSDT
Fly.io — Sao Paulo
Estrategia: MA9xMA21 + RSI + Bollinger Bands + Trailing Stop
"""

import os, time, math, logging, threading, urllib.request
from datetime import datetime, time as dtime

API_KEY             = os.environ.get("BINANCE_API_KEY", "")
API_SECRET          = os.environ.get("BINANCE_API_SECRET", "")
PAR                 = os.environ.get("TRADING_PAR", "DOGEUSDT")
VALOR_OPERACAO_USDT = float(os.environ.get("VALOR_OPERACAO", "20"))
STOP_LOSS_PCT       = 0.0025
TRAILING_INICIO_PCT = 0.002
TRAILING_DIST_PCT   = 0.0015
PERDA_MAX_DIA_USDT  = float(os.environ.get("PERDA_MAX_DIA", "18"))
META_DIA_USDT       = float(os.environ.get("META_DIA", "55"))
RSI_PERIODO         = 14
RSI_COMPRA_MAX      = 65
RSI_VENDA_MIN       = 35
BB_PERIODO          = 20
BB_DESVIO           = 2.0
MA_RAPIDA           = 9
MA_LENTA            = 21
TIMEFRAME           = "1m"

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
total_ordens   = 0
total_ganhos   = 0
total_perdas   = 0
step_size_cache = {}

def descobrir_ip():
    global ip_publico
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip()
                ip_publico = ip
                log.info(f"IP publico: {ip}")
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
        log.info(f"Binance conectada | Conta: {info['accountType']}")
        return cliente
    except Exception as e:
        log.error(f"Erro ao conectar Binance: {e}")
        return None

def obter_step_size(cliente, symbol):
    if symbol in step_size_cache:
        return step_size_cache[symbol]
    try:
        info = cliente.get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step = float(f["stepSize"])
                step_size_cache[symbol] = step
                log.info(f"Step size {symbol}: {step}")
                return step
    except Exception as e:
        log.error(f"Erro ao obter step size: {e}")
    return 1.0

def arredondar_quantidade(quantidade, step_size):
    casas = int(round(-math.log10(step_size))) if step_size < 1 else 0
    qtd = math.floor(quantidade / step_size) * step_size
    return round(qtd, casas)

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
        log.error(f"Erro ao obter candles: {e}")
        return None

def calcular_indicadores(df):
    df = df.copy()
    df["ma9"]     = df["close"].rolling(MA_RAPIDA).mean()
    df["ma21"]    = df["close"].rolling(MA_LENTA).mean()
    delta = df["close"].diff()
    mg = delta.where(delta > 0, 0.0).rolling(RSI_PERIODO).mean()
    mp = (-delta.where(delta < 0, 0.0)).rolling(RSI_PERIODO).mean()
    df["rsi"]     = 100 - (100 / (1 + (mg / mp)))
    df["bb_mid"]  = df["close"].rolling(BB_PERIODO).mean()
    df["bb_std"]  = df["close"].rolling(BB_PERIODO).std()
    df["bb_upper"]= df["bb_mid"] + BB_DESVIO * df["bb_std"]
    df["bb_lower"]= df["bb_mid"] - BB_DESVIO * df["bb_std"]
    return df

def verificar_sinal(df):
    if len(df) < 25:
        return None
    c  = df.iloc[-2]
    c1 = df.iloc[-3]
    for col in ["ma9","ma21","rsi","bb_mid"]:
        if math.isnan(c[col]):
            return None
    cruzou_cima  = (c1["ma9"] <= c1["ma21"]) and (c["ma9"] > c["ma21"])
    cruzou_baixo = (c1["ma9"] >= c1["ma21"]) and (c["ma9"] < c["ma21"])
    if cruzou_cima and c["rsi"] < RSI_COMPRA_MAX and c["close"] <= c["bb_mid"]:
        return "compra"
    if cruzou_baixo and c["rsi"] > RSI_VENDA_MIN and c["close"] >= c["bb_mid"]:
        return "venda"
    return None

def abrir_ordem(cliente, sinal, preco):
    global posicao_aberta, total_ordens
    try:
        step = obter_step_size(cliente, PAR)
        qtd_raw = VALOR_OPERACAO_USDT / preco
        quantidade = arredondar_quantidade(qtd_raw, step)
        log.info(f"Calculando ordem: ${VALOR_OPERACAO_USDT} / {preco} = {qtd_raw:.4f} | step={step} | qtd final={quantidade}")
        if quantidade <= 0:
            log.error(f"Quantidade invalida: {quantidade} | Verifique VALOR_OPERACAO e preco do par")
            return False
        if sinal == "compra":
            ordem = cliente.order_market_buy(symbol=PAR, quantity=quantidade)
            sl = preco * (1 - STOP_LOSS_PCT)
        else:
            ordem = cliente.order_market_sell(symbol=PAR, quantity=quantidade)
            sl = preco * (1 + STOP_LOSS_PCT)
        posicao_aberta = {
            "tipo": sinal, "entrada": preco, "sl": sl,
            "pico": preco, "quantidade": quantidade,
            "ordem_id": ordem["orderId"],
            "hora": datetime.now().strftime("%H:%M:%S")
        }
        total_ordens += 1
        log.info(f"ORDEM ABERTA | {sinal.upper()} | Preco: {preco} | Qtd: {quantidade} | SL: {sl:.6f} | ID: {ordem['orderId']}")
        return True
    except Exception as e:
        log.error(f"Erro ao abrir ordem | Sinal: {sinal} | Preco: {preco} | Erro: {e}")
        return False

def gerenciar_posicao(cliente, preco_atual):
    global posicao_aberta, resultado_dia, total_ganhos, total_perdas
    if posicao_aberta is None:
        return
    p = posicao_aberta
    fechar = False
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
        if lucro_usdt >= 0:
            total_ganhos += 1
            log.info(f"FECHOU LUCRO | {p['tipo'].upper()} | Lucro: ${lucro_usdt:.4f} | Dia: ${resultado_dia:.4f}")
        else:
            total_perdas += 1
            log.info(f"FECHOU PERDA | {p['tipo'].upper()} | Perda: ${lucro_usdt:.4f} | Dia: ${resultado_dia:.4f}")
        try:
            if p["tipo"] == "compra":
                cliente.order_market_sell(symbol=PAR, quantity=p["quantidade"])
            else:
                cliente.order_market_buy(symbol=PAR, quantity=p["quantidade"])
        except Exception as e:
            log.error(f"Erro ao fechar ordem: {e}")
        posicao_aberta = None

def relatorio_dia():
    log.info("=" * 50)
    log.info(f"RELATORIO DO DIA | Par: {PAR}")
    log.info(f"Resultado: ${resultado_dia:.4f}")
    log.info(f"Ordens: {total_ordens} | Ganhos: {total_ganhos} | Perdas: {total_perdas}")
    log.info("=" * 50)

def loop_bot():
    global resultado_dia, posicao_aberta, status_bot, total_ordens, total_ganhos, total_perdas
    log.info("=" * 50)
    log.info(f"BOT BINANCE v4.0 INICIADO | Par: {PAR}")
    log.info(f"Valor por operacao: ${VALOR_OPERACAO_USDT}")
    log.info(f"Stop dia: ${PERDA_MAX_DIA_USDT} | Meta dia: ${META_DIA_USDT}")
    log.info("=" * 50)
    cliente = None
    ultimo_dia = datetime.now().date()
    while True:
        try:
            if cliente is None:
                status_bot = "conectando"
                cliente = criar_cliente()
                if not cliente:
                    status_bot = "erro_conexao"
                    time.sleep(60)
                    continue
                status_bot = "rodando"
                obter_step_size(cliente, PAR)

            agora = datetime.now()
            if agora.date() != ultimo_dia:
                relatorio_dia()
                resultado_dia = 0.0
                posicao_aberta = None
                total_ordens = 0
                total_ganhos = 0
                total_perdas = 0
                ultimo_dia = agora.date()

            if resultado_dia <= -PERDA_MAX_DIA_USDT:
                if status_bot != "stop_diario":
                    log.warning(f"STOP DIARIO atingido | Perda: ${resultado_dia:.4f}")
                status_bot = "stop_diario"
                time.sleep(300)
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
                    c = df.iloc[-2]
                    log.info(f"SINAL: {sinal.upper()} | RSI: {c['rsi']:.1f} | MA9: {c['ma9']:.6f} | MA21: {c['ma21']:.6f} | Preco: {preco_atual}")
                    abrir_ordem(cliente, sinal, preco_atual)

            time.sleep(30)

        except Exception as e:
            log.error(f"Erro no loop principal: {e}")
            cliente = None
            time.sleep(30)

def iniciar_servidor_web():
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            pos_info = "nenhuma"
            if posicao_aberta:
                p = posicao_aberta
                pos_info = f"{p['tipo']}@{p['entrada']}|sl:{p['sl']:.6f}|desde:{p['hora']}"
            html = (
                f"BOT BINANCE v4.0\n"
                f"Status: {status_bot}\n"
                f"Par: {PAR}\n"
                f"Resultado dia: ${resultado_dia:.4f}\n"
                f"Ordens: {total_ordens} | Ganhos: {total_ganhos} | Perdas: {total_perdas}\n"
                f"Posicao: {pos_info}\n"
                f"IP: {ip_publico}\n"
                f"Hora: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
            )
            msg = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg)
        def log_message(self, *args): pass
    porta = int(os.environ.get("PORT", 8080))
    log.info(f"Servidor web porta {porta}")
    HTTPServer(("0.0.0.0", porta), Handler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=descobrir_ip, daemon=True).start()
    threading.Thread(target=loop_bot, daemon=True).start()
    iniciar_servidor_web()
