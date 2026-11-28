"""
BOT BINANCE v4.1 — DOGEUSDC
Fly.io — Sao Paulo
Estrategia: MA9xMA21 + RSI + Trailing Stop
Changelog v4.1:
  - RSI faixa ampliada (68/32 em vez de 65/35)
  - Bollinger Bands removido como filtro obrigatorio
  - Corrigida logica de venda sem posicao aberta
  - TIMEFRAME configuravel via variavel de ambiente
  - Logs detalhados mostrando motivo de nao gerar sinal
"""

import os, time, math, logging, threading, urllib.request
from datetime import datetime, time as dtime

API_KEY             = os.environ.get("BINANCE_API_KEY", "")
API_SECRET          = os.environ.get("BINANCE_API_SECRET", "")
PAR                 = os.environ.get("TRADING_PAR", "DOGEUSDC")
VALOR_OPERACAO_USDT = float(os.environ.get("VALOR_OPERACAO", "20"))
STOP_LOSS_PCT       = 0.0025
TRAILING_INICIO_PCT = 0.002
TRAILING_DIST_PCT   = 0.0015
PERDA_MAX_DIA_USDT  = float(os.environ.get("PERDA_MAX_DIA", "18"))
META_DIA_USDT       = float(os.environ.get("META_DIA", "999"))
RSI_PERIODO         = 14
RSI_COMPRA_MAX      = 68   # era 65
RSI_VENDA_MIN       = 32   # era 35
MA_RAPIDA           = 9
MA_LENTA            = 21
TIMEFRAME           = os.environ.get("TIMEFRAME", "1m")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

step_size_cache = {}
posicao_aberta  = False
preco_entrada   = 0.0
max_preco       = 0.0
trailing_ativo  = False
lucro_dia       = 0.0
perda_dia       = 0.0
total_ordens    = 0

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

def calcular_rsi(precos, periodo=14):
    if len(precos) < periodo + 1:
        return 50
    deltas = [precos[i] - precos[i-1] for i in range(1, len(precos))]
    ganhos = [d if d > 0 else 0 for d in deltas]
    perdas = [-d if d < 0 else 0 for d in deltas]
    media_ganho = sum(ganhos[-periodo:]) / periodo
    media_perda = sum(perdas[-periodo:]) / periodo
    if media_perda == 0:
        return 100
    rs = media_ganho / media_perda
    return 100 - (100 / (1 + rs))

def calcular_ma(precos, periodo):
    if len(precos) < periodo:
        return None
    return sum(precos[-periodo:]) / periodo

def obter_saldo_base(cliente):
    """Retorna saldo disponivel da moeda cotacao (USDC ou USDT)"""
    try:
        moeda_cotacao = PAR.replace("DOGE", "").replace("BTC", "").replace("ETH", "").replace("BNB", "")
        conta = cliente.get_account()
        for ativo in conta["balances"]:
            if ativo["asset"] == moeda_cotacao:
                return float(ativo["free"])
    except Exception as e:
        log.error(f"Erro ao obter saldo: {e}")
    return 0.0

def obter_saldo_doge(cliente):
    """Retorna saldo disponivel de DOGE"""
    try:
        moeda_base = "DOGE"
        conta = cliente.get_account()
        for ativo in conta["balances"]:
            if ativo["asset"] == moeda_base:
                return float(ativo["free"])
    except Exception as e:
        log.error(f"Erro ao obter saldo DOGE: {e}")
    return 0.0

def abrir_ordem(cliente, sinal, preco):
    global posicao_aberta, total_ordens
    try:
        step = obter_step_size(cliente, PAR)

        if sinal == "compra":
            qtd_raw = VALOR_OPERACAO_USDT / preco
            quantidade = arredondar_quantidade(qtd_raw, step)
            log.info(f"Calculando ordem: ${VALOR_OPERACAO_USDT} / {preco} = {qtd_raw:.4f} | step={step} | qtd final={quantidade}")
            if quantidade <= 0:
                log.error(f"Quantidade invalida: {quantidade}")
                return False
            ordem = cliente.order_market_buy(symbol=PAR, quantity=quantidade)

        elif sinal == "venda":
            # Verifica se tem DOGE para vender
            saldo_doge = obter_saldo_doge(cliente)
            quantidade = arredondar_quantidade(saldo_doge, step)
            log.info(f"Venda: saldo DOGE={saldo_doge:.2f} | qtd final={quantidade}")
            if quantidade <= 0:
                log.warning("Sem DOGE para vender — ignorando sinal de venda")
                return False
            ordem = cliente.order_market_sell(symbol=PAR, quantity=quantidade)

        else:
            return False

        log.info(f"Ordem {sinal.upper()} executada | {PAR} | Qtd: {quantidade} | Preco: {preco}")
        total_ordens += 1
        posicao_aberta = (sinal == "compra")
        return True

    except Exception as e:
        log.error(f"Erro ao abrir ordem | Sinal: {sinal} | Preco: {preco} | Erro: {e}")
        return False

def verificar_trailing_stop(cliente, preco_atual):
    global posicao_aberta, preco_entrada, max_preco, trailing_ativo, perda_dia

    if not posicao_aberta:
        return

    # Stop loss fixo
    stop_fixo = preco_entrada * (1 - STOP_LOSS_PCT)
    if preco_atual <= stop_fixo:
        log.warning(f"STOP LOSS ativado | Entrada: {preco_entrada} | Atual: {preco_atual}")
        if abrir_ordem(cliente, "venda", preco_atual):
            perda = (preco_atual - preco_entrada) / preco_entrada * VALOR_OPERACAO_USDT
            perda_dia += abs(perda)
            posicao_aberta = False
            trailing_ativo = False
        return

    # Trailing stop
    if preco_atual > max_preco:
        max_preco = preco_atual

    ganho_pct = (max_preco - preco_entrada) / preco_entrada
    if ganho_pct >= TRAILING_INICIO_PCT:
        trailing_ativo = True

    if trailing_ativo:
        trailing_stop = max_preco * (1 - TRAILING_DIST_PCT)
        if preco_atual <= trailing_stop:
            log.info(f"TRAILING STOP ativado | Max: {max_preco} | Atual: {preco_atual}")
            if abrir_ordem(cliente, "venda", preco_atual):
                lucro = (preco_atual - preco_entrada) / preco_entrada * VALOR_OPERACAO_USDT
                if lucro > 0:
                    global lucro_dia
                    lucro_dia += lucro
                else:
                    perda_dia += abs(lucro)
                posicao_aberta = False
                trailing_ativo = False

def resetar_dia():
    global lucro_dia, perda_dia, total_ordens
    lucro_dia = 0.0
    perda_dia = 0.0
    total_ordens = 0
    log.info("Novo dia — contadores resetados")

def loop_principal(cliente):
    global posicao_aberta, preco_entrada, max_preco, trailing_ativo

    ultimo_dia = datetime.now().date()
    log.info(f"Loop iniciado | Par: {PAR} | Timeframe: {TIMEFRAME}")

    while True:
        try:
            agora = datetime.now()

            # Reset diario
            if agora.date() != ultimo_dia:
                resetar_dia()
                ultimo_dia = agora.date()

            # Verificar limites do dia
            if perda_dia >= PERDA_MAX_DIA_USDT:
                log.warning(f"Stop diario atingido: perda=${perda_dia:.2f} | Aguardando proximo dia...")
                time.sleep(300)
                continue

            if lucro_dia >= META_DIA_USDT:
                log.info(f"Meta diaria atingida: lucro=${lucro_dia:.2f} | Aguardando proximo dia...")
                time.sleep(300)
                continue

            # Obter candles
            klines = cliente.get_klines(symbol=PAR, interval=TIMEFRAME, limit=50)
            fechamentos = [float(k[4]) for k in klines]
            preco_atual = fechamentos[-1]

            # Calcular indicadores
            ma_rapida = calcular_ma(fechamentos, MA_RAPIDA)
            ma_lenta  = calcular_ma(fechamentos, MA_LENTA)
            rsi       = calcular_rsi(fechamentos, RSI_PERIODO)

            if ma_rapida is None or ma_lenta is None:
                time.sleep(10)
                continue

            # Verificar trailing stop se tem posicao
            if posicao_aberta:
                verificar_trailing_stop(cliente, preco_atual)

            # Gerar sinal
            sinal = None
            motivo_bloqueio = []

            if ma_rapida > ma_lenta:
                # Condicao de compra: MA rapida acima da lenta
                if rsi <= RSI_COMPRA_MAX:
                    if not posicao_aberta:
                        sinal = "compra"
                    else:
                        motivo_bloqueio.append("posicao ja aberta")
                else:
                    motivo_bloqueio.append(f"RSI alto demais para compra: {rsi:.1f} > {RSI_COMPRA_MAX}")
            elif ma_rapida < ma_lenta:
                # Condicao de venda: MA rapida abaixo da lenta
                if rsi >= RSI_VENDA_MIN:
                    if posicao_aberta:
                        sinal = "venda"
                    else:
                        motivo_bloqueio.append("sem posicao aberta para vender")
                else:
                    motivo_bloqueio.append(f"RSI baixo demais para venda: {rsi:.1f} < {RSI_VENDA_MIN}")

            if sinal:
                log.info(f"SINAL: {sinal.upper()} | RSI: {rsi:.1f} | MA9: {ma_rapida:.6f} | MA21: {ma_lenta:.6f} | Preco: {preco_atual:.5f}")
                if abrir_ordem(cliente, sinal, preco_atual):
                    if sinal == "compra":
                        preco_entrada = preco_atual
                        max_preco = preco_atual
                        trailing_ativo = False
            else:
                # Log a cada 10 minutos para nao encher os logs
                if agora.minute % 10 == 0 and agora.second < 30:
                    status = "POSICAO ABERTA" if posicao_aberta else "aguardando sinal"
                    bloqueio = " | ".join(motivo_bloqueio) if motivo_bloqueio else "sem cruzamento de MA"
                    log.info(f"Status: {status} | RSI: {rsi:.1f} | MA9: {ma_rapida:.6f} | MA21: {ma_lenta:.6f} | Motivo: {bloqueio}")

            time.sleep(30)

        except Exception as e:
            log.error(f"Erro no loop: {e}")
            time.sleep(30)

def iniciar_servidor_web():
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            status = (
                f"BOT BINANCE v4.1 | Par: {PAR} | "
                f"Posicao: {'ABERTA' if posicao_aberta else 'fechada'} | "
                f"Lucro dia: ${lucro_dia:.2f} | Perda dia: ${perda_dia:.2f} | "
                f"Ordens: {total_ordens}"
            )
            self.wfile.write(status.encode())

        def log_message(self, format, *args):
            pass

    porta = int(os.environ.get("PORT", 8080))
    log.info(f"Servidor web porta {porta}")
    HTTPServer(("0.0.0.0", porta), Handler).serve_forever()

def obter_ip_publico():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            ip = r.read().decode()
            log.info(f"IP publico: {ip}")
    except:
        log.warning("Nao foi possivel obter IP publico")

if __name__ == "__main__":
    from binance.client import Client

    log.info("=" * 50)
    log.info(f"BOT BINANCE v4.1 INICIADO | Par: {PAR}")
    log.info(f"Valor por operacao: ${VALOR_OPERACAO_USDT}")
    log.info(f"Stop dia: ${PERDA_MAX_DIA_USDT} | Meta dia: ${META_DIA_USDT}")
    log.info("=" * 50)

    threading.Thread(target=iniciar_servidor_web, daemon=True).start()
    obter_ip_publico()

    cliente = Client(API_KEY, API_SECRET)
    info = cliente.get_account()
    log.info(f"Binance conectada | Conta: {info['accountType']}")

    loop_principal(cliente)
