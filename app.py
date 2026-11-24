"""Painel local do DigiRail Connect DMIX pela Ethernet (Modbus TCP)."""
from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HOST_HTTP = "127.0.0.1"
PORTA_HTTP = 8090
ID_DIGIRAIL = 0x0300

EXCECOES = {
    1: "função Modbus não suportada",
    2: "endereço de registrador inválido",
    3: "valor inválido",
    4: "falha interna do módulo",
}


class ErroModbus(Exception):
    pass


def texto_erro(exc: Exception) -> str:
    if isinstance(exc, ErroModbus):
        return str(exc)
    if isinstance(exc, socket.timeout):
        return "Tempo esgotado. O módulo não respondeu nesse IP e porta."
    if isinstance(exc, ConnectionRefusedError):
        return "A porta TCP recusou a conexão. Confira se o Modbus TCP está ativo."
    if isinstance(exc, OSError):
        return f"Falha de rede: {exc.strerror or exc}"
    return str(exc)


class ClienteModbus:
    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.tid = 0
        self.unidade = 1

    def fechar(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def conectar(self, host: str, porta: int, unidade: int) -> None:
        self.fechar()
        sock = socket.create_connection((host, porta), timeout=2.5)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(2.5)
        self.sock = sock
        self.unidade = unidade

    def _receber(self, tamanho: int) -> bytes:
        if self.sock is None:
            raise ErroModbus("Sem conexão com o módulo.")
        partes = []
        faltam = tamanho
        while faltam:
            bloco = self.sock.recv(faltam)
            if not bloco:
                raise ErroModbus("O módulo fechou a conexão.")
            partes.append(bloco)
            faltam -= len(bloco)
        return b"".join(partes)

    def _transacao(self, pdu: bytes) -> bytes:
        if self.sock is None:
            raise ErroModbus("Sem conexão com o módulo.")
        self.tid = (self.tid + 1) & 0xFFFF
        cabecalho = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unidade)
        self.sock.sendall(cabecalho + pdu)
        resposta = self._receber(7)
        tid, proto, comprimento, _unidade = struct.unpack(">HHHB", resposta)
        if proto != 0 or tid != self.tid:
            raise ErroModbus("Resposta Modbus fora de ordem.")
        corpo = self._receber(comprimento - 1)
        if corpo and corpo[0] & 0x80:
            codigo = corpo[1] if len(corpo) > 1 else 0
            detalhe = EXCECOES.get(codigo, f"código {codigo}")
            raise ErroModbus(f"O módulo recusou o comando ({detalhe}).")
        return corpo

    def ler_holding(self, endereco: int, quantidade: int) -> list[int]:
        corpo = self._transacao(struct.pack(">BHH", 0x03, endereco, quantidade))
        if len(corpo) < 2 or corpo[0] != 0x03:
            raise ErroModbus("Resposta de leitura inesperada.")
        dados = corpo[2 : 2 + corpo[1]]
        if len(dados) != quantidade * 2:
            raise ErroModbus("O módulo devolveu um tamanho de leitura diferente do pedido.")
        return [struct.unpack(">H", dados[i : i + 2])[0] for i in range(0, len(dados), 2)]

    def escrever(self, endereco: int, valor: int) -> None:
        corpo = self._transacao(struct.pack(">BHH", 0x06, endereco, valor))
        if len(corpo) < 5 or corpo[0] != 0x06:
            raise ErroModbus("Resposta de escrita inesperada.")


def carregar_config() -> dict:
    padrao = {"host": "", "porta": 502, "unidade": 1}
    if not CONFIG_PATH.exists():
        return padrao
    try:
        dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return padrao
    return {
        "host": str(dados.get("host") or "").strip(),
        "porta": int(dados.get("porta") or 502),
        "unidade": int(dados.get("unidade") or 1),
    }


def salvar_config(host: str, porta: int, unidade: int) -> None:
    CONFIG_PATH.write_text(
        json.dumps({"host": host, "porta": porta, "unidade": unidade}, indent=2),
        encoding="utf-8",
    )


_cache_interfaces = {"quando": 0.0, "lista": []}


def interfaces_locais() -> list[dict]:
    agora = time.time()
    if agora - _cache_interfaces["quando"] < 15:
        return list(_cache_interfaces["lista"])
    comando = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -ne '127.0.0.1' } | "
        "Select-Object InterfaceAlias, IPAddress | ConvertTo-Json -Compress"
    )
    try:
        bruto = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", comando],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return list(_cache_interfaces["lista"])
    bruto = bruto.strip()
    if not bruto:
        return []
    dados = json.loads(bruto)
    if isinstance(dados, dict):
        dados = [dados]
    lista = [
        {"nome": item.get("InterfaceAlias", ""), "ip": item.get("IPAddress", "")}
        for item in dados
        if item.get("IPAddress")
    ]
    _cache_interfaces["quando"] = agora
    _cache_interfaces["lista"] = lista
    return list(lista)


def nome_modelo(lo: int, hi: int) -> str:
    entradas = bin(lo & 0xFF).count("1")
    transistores = bin((lo >> 8) & 0xFF).count("1")
    reles = bin(hi & 0xFF).count("1")
    familia = "DMIX" if entradas > 4 else "RAMIX"
    if transistores:
        return f"{familia} DO ETH"
    if reles:
        return f"{familia} RL ETH"
    return "DigiRail Connect"


def canais_de_hardware(lo: int, hi: int) -> tuple[list[int], list[str]]:
    entradas = [i for i in range(8) if lo & (1 << i)]
    transistores = [i for i in range(8) if lo & (1 << (8 + i))]
    reles = [i for i in range(8) if hi & (1 << i)]
    if not entradas:
        entradas = list(range(8))
    if transistores:
        return entradas, [f"K{i + 1}" for i in transistores]
    if reles:
        return entradas, [f"R{i + 1}" for i in reles]
    return entradas, [f"K{i + 1}" for i in range(8)]


def formatar_mac(reg4: int, reg5: int, reg6: int) -> str:
    octetos = [(reg6 >> 8) & 0xFF, reg6 & 0xFF, (reg5 >> 8) & 0xFF, reg5 & 0xFF, (reg4 >> 8) & 0xFF, reg4 & 0xFF]
    return ":".join(f"{b:02X}" for b in octetos)


def formatar_firmware(valor: int) -> str:
    if 0 < valor < 1000:
        return f"{valor // 100}.{valor % 100:02d}"
    return str(valor)


# Pulso físico na entrada D2. Estágios nas saídas K1, K2 e K3.
INDICE_PULSO = 1
DEBOUNCE_PULSO = 0.25
ESPERA_RECOMECAR = 2.0
TITULOS = (
    "Aguardando pulso",
    "Primeiro processo",
    "Segundo processo",
    "Terceiro processo",
    "Processo finalizado",
)


def saidas_da_etapa(etapa: int) -> tuple[int, int, int]:
    return {
        0: (0, 0, 0),
        1: (1, 0, 0),
        2: (0, 1, 0),
        3: (0, 0, 1),
        4: (0, 0, 0),
    }[etapa]


def avancar_por_pulso(etapa: int, anterior, nivel: int, agora: float, ultimo_avanco: float):
    if anterior is None or nivel != 1 or anterior != 0:
        return etapa, ultimo_avanco, False
    if agora - ultimo_avanco < DEBOUNCE_PULSO or etapa >= 4:
        return etapa, ultimo_avanco, False
    return etapa + 1, agora, True


cliente = ClienteModbus()
trava = threading.Lock()
io_lock = threading.Lock()
config = carregar_config()
estado = {
    "conectado": False,
    "erro": "Informe o IP do DigiRail e conecte.",
    "host": config["host"],
    "porta": config["porta"],
    "unidade": config["unidade"],
    "dispositivo": None,
    "entradas": [0] * 8,
    "saidas": [0] * 8,
    "indices_entrada": list(range(8)),
    "nomes_saidas": [f"K{i}" for i in range(1, 9)],
    "atualizado": None,
}
processo = {
    "etapa": 0,
    "anterior": None,
    "ultimo_avanco": 0.0,
    "finalizado_em": None,
}


def ler_modulo() -> None:
    with io_lock:
        ident = cliente.ler_holding(0, 13)
        io = cliente.ler_holding(70, 16)
    serie = ident[0] | (ident[1] << 16)
    entradas_idx, nomes = canais_de_hardware(ident[2], ident[3])
    entradas = [1 if io[i] else 0 for i in range(8)]
    saidas = [1 if io[8 + i] else 0 for i in range(8)]
    compativel = ident[12] == ID_DIGIRAIL
    dispositivo = {
        "serie": serie,
        "firmware": formatar_firmware(ident[11]),
        "mac": formatar_mac(ident[4], ident[5], ident[6]),
        "id": f"0x{ident[12]:04X}",
        "compativel": compativel,
        "modelo": nome_modelo(ident[2], ident[3]),
    }
    with trava:
        estado["erro"] = None if compativel else "Conectado, mas o código de identificação não é 0x0300."
        estado["conectado"] = True
        estado["dispositivo"] = dispositivo
        estado["entradas"] = entradas
        estado["saidas"] = saidas
        estado["indices_entrada"] = entradas_idx
        estado["nomes_saidas"] = nomes
        estado["atualizado"] = time.time()
    observar_pulso(entradas)


def aplicar_saidas(padrao: tuple[int, int, int]) -> None:
    with io_lock:
        for indice, valor in enumerate(padrao):
            cliente.escrever(500 + indice, valor)
    with trava:
        saidas = list(estado["saidas"])
        for indice, valor in enumerate(padrao):
            if indice < len(saidas):
                saidas[indice] = valor
        estado["saidas"] = saidas


def observar_pulso(entradas: list[int]) -> None:
    nivel = entradas[INDICE_PULSO] if len(entradas) > INDICE_PULSO else 0
    with trava:
        anterior = processo["anterior"]
        etapa = processo["etapa"]
        ultimo = processo["ultimo_avanco"]
        processo["anterior"] = nivel
    agora = time.time()
    nova, ultimo_novo, mudou = avancar_por_pulso(etapa, anterior, nivel, agora, ultimo)
    if not mudou:
        return
    aplicar_saidas(saidas_da_etapa(nova))
    with trava:
        processo["etapa"] = nova
        processo["ultimo_avanco"] = ultimo_novo
        processo["finalizado_em"] = agora if nova == 4 else None


def reiniciar_processo() -> None:
    with trava:
        processo["etapa"] = 0
        processo["anterior"] = None
        processo["ultimo_avanco"] = 0.0
        processo["finalizado_em"] = None


def vista_processo() -> dict:
    agora = time.time()
    with trava:
        etapa = processo["etapa"]
        finalizado_em = processo["finalizado_em"]
        pulso = estado["entradas"][INDICE_PULSO] if len(estado["entradas"]) > INDICE_PULSO else 0
    recomecar = (
        etapa == 4
        and finalizado_em is not None
        and agora - finalizado_em >= ESPERA_RECOMECAR
    )
    return {
        "etapa": etapa,
        "titulo": TITULOS[etapa],
        "recomecar": recomecar,
        "pulso": pulso,
    }


def conectar(host: str, porta: int, unidade: int) -> None:
    if not host:
        raise ErroModbus("Informe o IP do DigiRail.")
    if not (1 <= porta <= 65535):
        raise ErroModbus("A porta TCP precisa ficar entre 1 e 65535.")
    if not (0 <= unidade <= 255):
        raise ErroModbus("O endereço Modbus precisa ficar entre 0 e 255.")
    with io_lock:
        cliente.conectar(host, porta, unidade)
    with trava:
        estado["host"] = host
        estado["porta"] = porta
        estado["unidade"] = unidade
    reiniciar_processo()
    try:
        ler_modulo()
        aplicar_saidas((0, 0, 0))
    except Exception:
        with io_lock:
            cliente.fechar()
        with trava:
            estado["conectado"] = False
            estado["dispositivo"] = None
        raise
    salvar_config(host, porta, unidade)


def desconectar() -> None:
    with trava:
        ativo = estado["conectado"]
    if ativo:
        try:
            aplicar_saidas((0, 0, 0))
        except Exception:
            pass
    with io_lock:
        cliente.fechar()
    reiniciar_processo()
    with trava:
        estado["conectado"] = False
        estado["dispositivo"] = None
        estado["erro"] = "Desconectado."


def recomecar() -> None:
    with trava:
        if not estado["conectado"]:
            raise ErroModbus("Conecte ao módulo antes de recomeçar.")
        nivel = estado["entradas"][INDICE_PULSO] if len(estado["entradas"]) > INDICE_PULSO else 0
    aplicar_saidas((0, 0, 0))
    with trava:
        processo["etapa"] = 0
        processo["finalizado_em"] = None
        processo["ultimo_avanco"] = time.time()
        processo["anterior"] = nivel


def escrever_saida(canal: int, valor: int) -> None:
    if valor not in (0, 1):
        raise ErroModbus("A saída aceita só 0 ou 1.")
    with trava:
        if not estado["conectado"]:
            raise ErroModbus("Conecte ao módulo antes de acionar uma saída.")
        nomes = list(estado["nomes_saidas"])
    if canal < 1 or canal > len(nomes):
        raise ErroModbus("Esse canal de saída não existe neste módulo.")
    with io_lock:
        cliente.escrever(499 + canal, valor)
    ler_modulo()


def copia_estado() -> dict:
    with trava:
        dados = dict(estado)
        dados["dispositivo"] = dict(estado["dispositivo"]) if estado["dispositivo"] else None
        dados["entradas"] = list(estado["entradas"])
        dados["saidas"] = list(estado["saidas"])
        dados["indices_entrada"] = list(estado["indices_entrada"])
        dados["nomes_saidas"] = list(estado["nomes_saidas"])
    dados["interfaces"] = interfaces_locais()
    dados["processo"] = vista_processo()
    return dados


def ciclo() -> None:
    while True:
        with trava:
            ativo = estado["conectado"]
        if not ativo:
            time.sleep(0.3)
            continue
        try:
            ler_modulo()
        except Exception as exc:
            with io_lock:
                cliente.fechar()
            with trava:
                estado["conectado"] = False
                estado["erro"] = texto_erro(exc)
        time.sleep(0.1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: dict) -> None:
        bruto = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(bruto)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(bruto)

    def _ler_json(self) -> dict:
        tamanho = int(self.headers.get("Content-Length") or 0)
        if tamanho > 8192:
            raise ErroModbus("Pedido grande demais.")
        bruto = self.rfile.read(tamanho) if tamanho else b"{}"
        try:
            dados = json.loads(bruto.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ErroModbus("JSON inválido.") from exc
        if not isinstance(dados, dict):
            raise ErroModbus("O pedido precisa ser um objeto JSON.")
        return dados

    def do_GET(self) -> None:
        caminho = urlparse(self.path).path
        if caminho == "/api/estado":
            self._json(200, copia_estado())
            return
        if caminho in ("/", "/index.html"):
            pagina = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(pagina)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(pagina)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        caminho = urlparse(self.path).path
        try:
            dados = self._ler_json()
            if caminho == "/api/conectar":
                conectar(
                    str(dados.get("host") or "").strip(),
                    int(dados.get("porta") or 502),
                    int(dados.get("unidade") or 1),
                )
            elif caminho == "/api/desconectar":
                desconectar()
            elif caminho == "/api/saida":
                escrever_saida(int(dados["canal"]), int(dados["valor"]))
            elif caminho == "/api/recomecar":
                recomecar()
            else:
                self.send_error(404)
                return
            self._json(200, copia_estado())
        except (ErroModbus, KeyError, TypeError, ValueError) as exc:
            mensagem = str(exc) if isinstance(exc, ErroModbus) else "Pedido inválido."
            self._json(400, {"erro": mensagem})
        except Exception as exc:
            self._json(502, {"erro": texto_erro(exc)})


def iniciar_conexao_salva() -> None:
    if not config["host"]:
        return
    try:
        conectar(config["host"], int(config["porta"]), int(config["unidade"]))
    except Exception as exc:
        with trava:
            estado["erro"] = texto_erro(exc)


def main() -> None:
    threading.Thread(target=iniciar_conexao_salva, name="conexao", daemon=True).start()
    threading.Thread(target=ciclo, name="leitura", daemon=True).start()
    servidor = ThreadingHTTPServer((HOST_HTTP, PORTA_HTTP), Handler)
    print(f"Painel em http://{HOST_HTTP}:{PORTA_HTTP}", flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("Encerrando.")
    finally:
        cliente.fechar()
        servidor.server_close()


def _receber_exato(sock: socket.socket, tamanho: int) -> bytes:
    partes = []
    faltam = tamanho
    while faltam:
        bloco = sock.recv(faltam)
        if not bloco:
            return b""
        partes.append(bloco)
        faltam -= len(bloco)
    return b"".join(partes)


def self_test() -> None:
    memoria = {12: ID_DIGIRAIL, 11: 104, 2: 0xFFFF, 3: 0, 0: 0x1234, 1: 0x0001}
    memoria.update({4: 0x9A78, 5: 0x5634, 6: 0x12AB})
    for i in range(8):
        memoria[70 + i] = 1 if i == 2 else 0
        memoria[78 + i] = 0

    pronto = threading.Event()

    def atender(conexao: socket.socket) -> None:
        with conexao:
            while True:
                cabecalho = _receber_exato(conexao, 7)
                if len(cabecalho) < 7:
                    return
                tid, _proto, comprimento, unidade = struct.unpack(">HHHB", cabecalho)
                pdu = _receber_exato(conexao, comprimento - 1)
                if not pdu:
                    return
                funcao = pdu[0]
                if funcao == 0x03:
                    endereco, quantidade = struct.unpack(">HH", pdu[1:5])
                    valores = [memoria.get(endereco + i, 0) for i in range(quantidade)]
                    dados = b"".join(struct.pack(">H", valor) for valor in valores)
                    corpo = bytes([0x03, len(dados)]) + dados
                elif funcao == 0x06:
                    endereco, valor = struct.unpack(">HH", pdu[1:5])
                    memoria[endereco] = valor
                    if 500 <= endereco <= 507:
                        memoria[78 + (endereco - 500)] = valor
                    corpo = pdu
                else:
                    corpo = bytes([funcao | 0x80, 1])
                conexao.sendall(struct.pack(">HHHB", tid, 0, len(corpo) + 1, unidade) + corpo)

    def servir() -> None:
        with socket.socket() as escuta:
            escuta.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            escuta.bind(("127.0.0.1", 0))
            porta_local = escuta.getsockname()[1]
            servir.porta = porta_local
            escuta.listen(1)
            escuta.settimeout(5)
            pronto.set()
            conexao, _ = escuta.accept()
            atender(conexao)

    thread = threading.Thread(target=servir, daemon=True)
    thread.start()
    if not pronto.wait(2):
        raise SystemExit("O servidor de teste não abriu.")
    prova = ClienteModbus()
    prova.conectar("127.0.0.1", servir.porta, 1)
    lidos = prova.ler_holding(0, 13)
    assert lidos[12] == ID_DIGIRAIL, lidos
    assert formatar_mac(lidos[4], lidos[5], lidos[6]) == "12:AB:56:34:9A:78"
    assert formatar_firmware(lidos[11]) == "1.04"
    prova.escrever(500, 1)
    io = prova.ler_holding(70, 16)
    assert io[2] == 1
    assert io[8] == 1
    prova.fechar()
    assert saidas_da_etapa(1) == (1, 0, 0)
    assert saidas_da_etapa(2) == (0, 1, 0)
    assert saidas_da_etapa(3) == (0, 0, 1)
    assert saidas_da_etapa(4) == (0, 0, 0)
    assert avancar_por_pulso(0, None, 1, 10, 0)[2] is False
    etapa, _, mudou = avancar_por_pulso(0, 0, 1, 10, 0)
    assert (etapa, mudou) == (1, True)
    etapa, _, mudou = avancar_por_pulso(1, 0, 1, 10.1, 10)
    assert mudou is False
    etapa, _, mudou = avancar_por_pulso(1, 0, 1, 10.4, 10)
    assert (etapa, mudou) == (2, True)
    etapa, _, mudou = avancar_por_pulso(3, 0, 1, 20, 0)
    assert (etapa, mudou) == (4, True)
    assert avancar_por_pulso(4, 0, 1, 30, 0)[2] is False
    print("self-test ok")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        main()
