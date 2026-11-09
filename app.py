import hashlib
import hmac
import time
from threading import RLock
from uuid import uuid4
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

SEGREDO_MOCK = b"segredo-exclusivo-do-laboratorio"
USUARIOS = {
    "cliente-01": ("CLIENTE", "C01", None),
    "cliente-02": ("CLIENTE", "C02", None),
    "cozinha-rec": ("COZINHA", "K01", "REC"),
    "cozinha-jpa": ("COZINHA", "K02", "JPA"),
    "gerente-rec": ("GERENTE", "G01", "REC"),
    "gerente-jpa": ("GERENTE", "G02", "JPA"),
    "matriz": ("MATRIZ", "M01", None),
}
SEGURANCA = HTTPBearer(auto_error=False)

class Falha(Exception):
    def __init__(self, status, mensagem):
        self.status = status
        self.mensagem = mensagem

def total_em_centavos(preco, quantidade):
    if type(quantidade) is not int or not 1 <= quantidade <= 20:
        raise ValueError("QUANTIDADE_INVALIDA")
    return preco * quantidade

def pontos_do_pedido(valor_centavos):
    return valor_centavos // 100

class PedidoEntrada(BaseModel):
    unidade: str
    produto: str
    quantidade: int = Field(strict=True, ge=1, le=20)
    canal: Literal["APP", "WEB", "TOTEM", "BALCAO", "PICKUP"]

class PreferenciasEntrada(BaseModel):
    marketing: bool
    fidelidade: bool

class EventoEntrada(BaseModel):
    evento_id: str = Field(min_length=1)
    pedido_id: str
    resultado: Literal["APROVADO", "RECUSADO", "TIMEOUT", "NAO_COBRADO"]
    valor_centavos: int

class SimulacaoEntrada(BaseModel):
    resultado: Literal["APROVADO", "RECUSADO", "TIMEOUT"]

class StatusEntrada(BaseModel):
    status: Literal["EM_PREPARO", "PRONTO", "ENTREGUE"]

def assinar_evento(evento):
    campos = ("evento_id", "pedido_id", "resultado", "valor_centavos")
    conteudo = "|".join(str(evento[campo]) for campo in campos)
    return hmac.new(SEGREDO_MOCK, conteudo.encode(), hashlib.sha256).hexdigest()

class Loja:
    def __init__(self, relogio=None):
        self.agora = relogio or time.monotonic
        self.trava = RLock()
        self.catalogo = {
            "REC": {"CUS": 1200, "TAP": 1800},
            "JPA": {"CUS": 1300},
            "PERF": {"CUS": 1200},
        }
        self.estoque = {
            "REC": {"CUS": 50, "TAP": 20},
            "JPA": {"CUS": 40},
            "PERF": {"CUS": 200000},
        }
        self.pedidos = {}
        self.chaves = {}
        self.eventos = {}
        self.preferencias = {}
        self.pontos = {}
        self.auditoria = []

    def registrar(self, pedido, tipo):
        if pedido["unidade"] != "PERF":
            self.auditoria.append({
                "correlation_id": pedido["correlation_id"],
                "pedido_id": pedido["id"],
                "evento": tipo,
            })

    def criar(self, cliente, unidade, produto, quantidade, canal, chave):
        with self.trava:
            if not chave or not chave.strip():
                raise Falha(422, "CHAVE_OBRIGATORIA")
            total_em_centavos(1200, quantidade)
            dados = (unidade, produto, quantidade, canal)
            indice = (cliente, chave)
            if indice in self.chaves:
                anterior, pedido_id = self.chaves[indice]
                if anterior != dados:
                    raise Falha(409, "CONFLITO_CHAVE_IDEMPOTENCIA")
                return dict(self.pedidos[pedido_id])
            if unidade not in self.catalogo:
                raise Falha(422, "UNIDADE_INVALIDA")
            if produto not in self.catalogo[unidade]:
                raise Falha(422, "PRODUTO_INDISPONIVEL_NA_UNIDADE")
            if self.estoque[unidade][produto] < quantidade:
                raise Falha(409, "ESTOQUE_INSUFICIENTE")
            self.estoque[unidade][produto] -= quantidade
            pedido_id = f"P{len(self.pedidos) + 1:04d}"
            pedido = {
                "id": pedido_id,
                "cliente_id": cliente,
                "unidade": unidade,
                "produto": produto,
                "quantidade": quantidade,
                "canal": canal,
                "total_centavos": total_em_centavos(self.catalogo[unidade][produto], quantidade),
                "status": "PENDENTE",
                "correlation_id": str(uuid4()),
                "pontos_creditados": 0,
                "reembolso": None,
                "reserva_expira_em": self.agora() + 600,
            }
            self.pedidos[pedido_id] = pedido
            self.chaves[indice] = (dados, pedido_id)
            self.registrar(pedido, "pedido_criado")
            self.registrar(pedido, "reserva_realizada")
            return dict(pedido)

    def buscar(self, pedido_id):
        pedido = self.pedidos.get(pedido_id)
        if pedido is None:
            raise Falha(404, "PEDIDO_INEXISTENTE")
        return pedido

    def acesso(self, pedido, usuario):
        perfil, ident, unidade = usuario
        if perfil == "MATRIZ":
            return
        if perfil == "CLIENTE" and ident == pedido["cliente_id"]:
            return
        if perfil in {"GERENTE", "COZINHA"} and unidade == pedido["unidade"]:
            return
        raise Falha(403, "ACESSO_NEGADO")

    def estornar_estoque(self, pedido):
        self.estoque[pedido["unidade"]][pedido["produto"]] += pedido["quantidade"]

    def processar_evento(self, evento):
        with self.trava:
            pedido = self.buscar(evento["pedido_id"])
            if evento["valor_centavos"] != pedido["total_centavos"]:
                raise Falha(422, "VALOR_DIVERGENTE")
            assinatura = (evento["pedido_id"], evento["resultado"], evento["valor_centavos"])
            if evento["evento_id"] in self.eventos:
                if self.eventos[evento["evento_id"]] != assinatura:
                    raise Falha(409, "CONFLITO_EVENTO")
                return dict(pedido)
            resultado = evento["resultado"]
            if resultado == "NAO_COBRADO":
                if self.agora() < pedido["reserva_expira_em"]:
                    raise Falha(409, "RESERVA_NAO_EXPIRADA")
                resultado = "RECUSADO"
            if resultado == "TIMEOUT":
                self.registrar(pedido, "pagamento_pendente")
            elif resultado == "APROVADO" and pedido["status"] == "CANCELADO":
                pedido["reembolso"] = "ESTORNO_PENDENTE"
                self.registrar(pedido, "confirmacao_tardia")
            elif pedido["status"] != "PENDENTE":
                raise Falha(409, "TRANSICAO_INVALIDA")
            elif resultado == "APROVADO":
                pedido["status"] = "PAGO"
                if self.preferencias.get(pedido["cliente_id"], {}).get("fidelidade", False):
                    pontos = pontos_do_pedido(pedido["total_centavos"])
                    pedido["pontos_creditados"] = pontos
                    self.pontos[pedido["cliente_id"]] = self.pontos.get(pedido["cliente_id"], 0) + pontos
                self.registrar(pedido, "pagamento_aprovado")
            elif resultado == "RECUSADO":
                pedido["status"] = "CANCELADO"
                self.estornar_estoque(pedido)
                self.registrar(pedido, "pagamento_recusado")
            self.eventos[evento["evento_id"]] = assinatura
            return dict(pedido)

    def cancelar(self, pedido_id):
        with self.trava:
            pedido = self.buscar(pedido_id)
            if pedido["status"] == "CANCELADO":
                return dict(pedido)
            if pedido["status"] == "ENTREGUE":
                raise Falha(409, "PEDIDO_JA_ENTREGUE")
            pago = pedido["status"] in {"PAGO", "EM_PREPARO", "PRONTO"}
            self.estornar_estoque(pedido)
            if pago:
                pedido["reembolso"] = "ESTORNO_SIMULADO"
                if pedido["pontos_creditados"]:
                    self.pontos[pedido["cliente_id"]] -= pedido["pontos_creditados"]
                    pedido["pontos_creditados"] = 0
            pedido["status"] = "CANCELADO"
            self.registrar(pedido, "cancelamento_administrativo")
            return dict(pedido)

PAGINA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Raízes do Nordeste - laboratório</title>
<style>
*{box-sizing:border-box}body{font:16px system-ui;margin:0;padding:20px;background:#f2f0e8;color:#163a2d}
main{max-width:540px;margin:auto;background:white;border-radius:12px;padding:24px}
label{display:block;margin:12px 0 5px}select,input,button{font:inherit;padding:10px;width:100%;max-width:100%}
input[type=checkbox]{width:auto}button{margin-top:12px;background:#205b44;color:white;border:0;border-radius:6px;cursor:pointer}
button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #d78f21}
small{display:block;line-height:1.5}#resultado{white-space:pre-wrap;overflow-wrap:anywhere}
</style>
</head>
<body>
<main>
<h1>Raízes do Nordeste</h1>
<p>Ambiente acadêmico. Pedidos e pagamentos são fictícios. Os dados ficam somente na memória enquanto a aplicação está aberta; não informe dados reais.</p>
<h2>Preferências opcionais</h2>
<label><input id="marketing" type="checkbox"> Quero receber ofertas de marketing</label>
<label><input id="fidelidade" type="checkbox"> Quero participar do programa de fidelidade</label>
<small>As opções vêm desmarcadas e podem ser alteradas ou revogadas. Nenhuma é necessária para comprar.</small>
<button type="button" id="salvar">Salvar ou revogar preferências</button>
<h2>Compra demonstrativa</h2>
<label for="unidade">Unidade</label><select id="unidade"><option value="REC">Recife</option><option value="JPA">João Pessoa</option></select>
<label for="produto">Produto</label><select id="produto"><option value="CUS">Cuscuz</option><option value="TAP">Tapioca (somente Recife)</option></select>
<label for="quantidade">Quantidade (1 a 20)</label><input id="quantidade" type="number" min="1" max="20" value="1">
<label for="canal">Canal representado</label><select id="canal"><option value="WEB">Web</option><option value="APP">Aplicativo</option><option value="TOTEM">Totem</option><option value="BALCAO">Balcão</option><option value="PICKUP">Retirada</option></select>
<label for="resultado-pagamento">Resposta simulada do pagamento</label><select id="resultado-pagamento"><option value="APROVADO">Aprovado</option><option value="RECUSADO">Recusado</option><option value="TIMEOUT">Sem resposta (timeout)</option></select>
<button type="button" id="comprar">Realizar pedido fictício</button>
<p id="resultado" role="status" aria-live="polite"></p>
</main>
<script>
const auth={Authorization:'Bearer cliente-01'};
const json={...auth,'Content-Type':'application/json'};
const el=id=>document.getElementById(id);
async function requisitar(url,metodo,corpo,extras={}){
 const resposta=await fetch(url,{method:metodo,headers:{...json,...extras},body:corpo?JSON.stringify(corpo):undefined});
 const dados=await resposta.json();
 if(!resposta.ok)throw Error(typeof dados.detail==='string'?dados.detail:JSON.stringify(dados.detail));
 return dados;
}
async function carregar(){
 try{const p=await requisitar('/preferencias','GET');el('marketing').checked=p.marketing;el('fidelidade').checked=p.fidelidade;}
 catch(e){el('resultado').textContent=e.message;}
}
el('salvar').addEventListener('click',async()=>{
 try{const p=await requisitar('/preferencias','PUT',{marketing:el('marketing').checked,fidelidade:el('fidelidade').checked});
 el('resultado').textContent='Preferências registradas: marketing='+p.marketing+', fidelidade='+p.fidelidade;}
 catch(e){el('resultado').textContent=e.message;}
});
el('comprar').addEventListener('click',async()=>{
 try{
 const pedido=await requisitar('/pedidos','POST',{unidade:el('unidade').value,produto:el('produto').value,quantidade:Number(el('quantidade').value),canal:el('canal').value},{'Idempotency-Key':crypto.randomUUID()});
 const final=await requisitar('/pedidos/'+pedido.id+'/simular-pagamento','POST',{resultado:el('resultado-pagamento').value});
 el('resultado').textContent='Pedido '+final.id+' | Total: R$ '+(final.total_centavos/100).toFixed(2).replace('.',',')+' | Estado: '+final.status;
 }catch(e){el('resultado').textContent='Operação rejeitada: '+e.message;}
});
carregar();
</script>
</body></html>"""

def create_app(relogio=None):
    api = FastAPI(title="Raízes do Nordeste - demonstração de QA")
    api.state.loja = Loja(relogio)

    def usuario(credenciais: HTTPAuthorizationCredentials | None = Depends(SEGURANCA)):
        token = credenciais.credentials if credenciais else ""
        if token not in USUARIOS:
            raise HTTPException(401, "AUTENTICACAO_INVALIDA")
        return USUARIOS[token]

    def tratar(funcao, *args):
        try:
            return funcao(*args)
        except Falha as erro:
            raise HTTPException(erro.status, erro.mensagem) from erro

    @api.get("/health")
    def health():
        return {"status": "ok"}

    @api.get("/demo", response_class=HTMLResponse)
    def demo():
        return PAGINA

    @api.get("/cardapio/{unidade}")
    def cardapio(unidade: str):
        loja = api.state.loja
        if unidade not in loja.catalogo:
            raise HTTPException(404, "UNIDADE_INVALIDA")
        return {"unidade": unidade, "produtos": [
            {"codigo": prod, "preco_centavos": preco, "estoque": loja.estoque[unidade][prod]}
            for prod, preco in loja.catalogo[unidade].items()
        ]}

    @api.get("/preferencias")
    def ver_preferencias(ident=Depends(usuario)):
        if ident[0] != "CLIENTE":
            raise HTTPException(403, "ACESSO_NEGADO")
        return dict(api.state.loja.preferencias.get(ident[1], {"marketing": False, "fidelidade": False}))

    @api.put("/preferencias")
    def salvar_preferencias(dados: PreferenciasEntrada, ident=Depends(usuario)):
        if ident[0] != "CLIENTE":
            raise HTTPException(403, "ACESSO_NEGADO")
        loja = api.state.loja
        with loja.trava:
            loja.preferencias[ident[1]] = dados.model_dump()
            loja.auditoria.append({
                "correlation_id": str(uuid4()),
                "cliente_id": ident[1],
                "evento": "preferencias_atualizadas",
                "marketing": dados.marketing,
                "fidelidade": dados.fidelidade,
            })
            return dict(loja.preferencias[ident[1]])

    @api.post("/pedidos", status_code=201)
    def criar_pedido(dados: PedidoEntrada, request: Request, idempotency_key: str = Header(...), ident=Depends(usuario)):
        if ident[0] != "CLIENTE":
            raise HTTPException(403, "ACESSO_NEGADO")
        pedido = tratar(api.state.loja.criar, ident[1], dados.unidade, dados.produto, dados.quantidade, dados.canal, idempotency_key)
        request.state.correlation_id = pedido["correlation_id"]
        return pedido

    @api.get("/pedidos/{pedido_id}")
    def consultar_pedido(pedido_id: str, request: Request, ident=Depends(usuario)):
        loja = api.state.loja
        with loja.trava:
            pedido = tratar(loja.buscar, pedido_id)
            tratar(loja.acesso, pedido, ident)
            request.state.correlation_id = pedido["correlation_id"]
            return dict(pedido)

    @api.post("/gateway/eventos")
    def evento_gateway(dados: EventoEntrada, request: Request, x_mock_signature: str = Header(default="")):
        evento = dados.model_dump()
        if not hmac.compare_digest(x_mock_signature, assinar_evento(evento)):
            raise HTTPException(401, "ASSINATURA_INVALIDA")
        resposta = tratar(api.state.loja.processar_evento, evento)
        request.state.correlation_id = resposta["correlation_id"]
        return resposta

    @api.post("/pedidos/{pedido_id}/simular-pagamento")
    def simular_pagamento(pedido_id: str, dados: SimulacaoEntrada, request: Request, ident=Depends(usuario)):
        loja = api.state.loja
        with loja.trava:
            pedido = tratar(loja.buscar, pedido_id)
            if ident[0] != "CLIENTE" or ident[1] != pedido["cliente_id"]:
                raise HTTPException(403, "ACESSO_NEGADO")
            evento = {"evento_id": str(uuid4()), "pedido_id": pedido_id, "resultado": dados.resultado, "valor_centavos": pedido["total_centavos"]}
            resposta = tratar(loja.processar_evento, evento)
            request.state.correlation_id = resposta["correlation_id"]
            return resposta

    @api.patch("/pedidos/{pedido_id}/status")
    def mudar_status(pedido_id: str, dados: StatusEntrada, request: Request, ident=Depends(usuario)):
        loja = api.state.loja
        with loja.trava:
            pedido = tratar(loja.buscar, pedido_id)
            if ident[0] != "COZINHA" or ident[2] != pedido["unidade"]:
                raise HTTPException(403, "ACESSO_NEGADO")
            sequencia = {"PAGO": "EM_PREPARO", "EM_PREPARO": "PRONTO", "PRONTO": "ENTREGUE"}
            if sequencia.get(pedido["status"]) != dados.status:
                raise HTTPException(409, "TRANSICAO_INVALIDA")
            pedido["status"] = dados.status
            loja.registrar(pedido, "status_atualizado")
            request.state.correlation_id = pedido["correlation_id"]
            return dict(pedido)

    @api.post("/pedidos/{pedido_id}/cancelar")
    def cancelar_pedido(pedido_id: str, request: Request, ident=Depends(usuario)):
        loja = api.state.loja
        with loja.trava:
            pedido = tratar(loja.buscar, pedido_id)
            if ident[0] != "MATRIZ" and not (ident[0] == "GERENTE" and ident[2] == pedido["unidade"]):
                raise HTTPException(403, "ACESSO_NEGADO")
            resposta = tratar(loja.cancelar, pedido_id)
            request.state.correlation_id = pedido["correlation_id"]
            return resposta

    @api.get("/relatorios")
    def relatorios(ident=Depends(usuario)):
        if ident[0] not in {"MATRIZ", "GERENTE"}:
            raise HTTPException(403, "ACESSO_NEGADO")
        loja = api.state.loja
        with loja.trava:
            pedidos = [p for p in loja.pedidos.values() if ident[0] == "MATRIZ" or p["unidade"] == ident[2]]
            return {"pedidos": len(pedidos), "pagos": sum(p["status"] in {"PAGO", "EM_PREPARO", "PRONTO", "ENTREGUE"} for p in pedidos), "unidade": ident[2] if ident[0] == "GERENTE" else "TODAS"}

    @api.get("/auditoria")
    def auditoria(ident=Depends(usuario)):
        if ident[0] != "MATRIZ":
            raise HTTPException(403, "ACESSO_NEGADO")
        return {"eventos": api.state.loja.auditoria[-1500:]}

    @api.middleware("http")
    async def correlacao(request: Request, call_next):
        request.state.correlation_id = str(uuid4())
        resposta = await call_next(request)
        resposta.headers["X-Correlation-ID"] = request.state.correlation_id
        return resposta

    return api

app = create_app()