from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from app import Falha, Loja, assinar_evento, create_app, pontos_do_pedido, total_em_centavos

@pytest.fixture
def ambiente():
    tempo = [0.0]
    api = create_app(relogio=lambda: tempo[0])
    with TestClient(api) as cliente:
        yield cliente, api.state.loja, tempo

def auth(token="cliente-01"):
    return {"Authorization": f"Bearer {token}"}

def criar(cliente, unidade="REC", produto="CUS", quantidade=1, canal="WEB", chave="pedido-1", token="cliente-01"):
    return cliente.post(
        "/pedidos",
        headers={**auth(token), "Idempotency-Key": chave},
        json={"unidade": unidade, "produto": produto, "quantidade": quantidade, "canal": canal},
    )

def novo(cliente, **kwargs):
    resposta = criar(cliente, **kwargs)
    assert resposta.status_code == 201, resposta.text
    return resposta.json()

def evento(cliente, pedido, resultado="APROVADO", evento_id="ev-1", valor=None, assinatura_correta=True):
    dados = {
        "evento_id": evento_id,
        "pedido_id": pedido["id"],
        "resultado": resultado,
        "valor_centavos": pedido["total_centavos"] if valor is None else valor,
    }
    assinatura = assinar_evento(dados) if assinatura_correta else "assinatura-errada"
    return cliente.post("/gateway/eventos", headers={"X-Mock-Signature": assinatura}, json=dados)

def pagar(cliente, pedido, resultado="APROVADO", token="cliente-01"):
    return cliente.post(
        f"/pedidos/{pedido['id']}/simular-pagamento",
        headers=auth(token),
        json={"resultado": resultado},
    )

def test_unidade_total_em_centavos():
    assert total_em_centavos(1200, 2) == 2400
    assert total_em_centavos(1300, 2) == 2600

@pytest.mark.parametrize("quantidade", [0, -1, 21, 1.5, True])
def test_unidade_quantidade_invalida(quantidade):
    with pytest.raises(ValueError, match="QUANTIDADE_INVALIDA"):
        total_em_centavos(1200, quantidade)

def test_unidade_pontos():
    assert pontos_do_pedido(1250) == 12

def test_ST01_criar_pedido_valido(ambiente):
    cliente, _, _ = ambiente
    resposta = criar(cliente)
    assert resposta.status_code == 201
    assert resposta.json()["status"] == "PENDENTE"
    assert resposta.json()["total_centavos"] == 1200
    assert resposta.headers["X-Correlation-ID"] == resposta.json()["correlation_id"]

def test_ST02_preco_e_estoque_por_unidade(ambiente):
    cliente, loja, _ = ambiente
    rec = novo(cliente, chave="rec")
    jpa = novo(cliente, unidade="JPA", chave="jpa")
    assert (rec["total_centavos"], jpa["total_centavos"]) == (1200, 1300)
    assert loja.estoque["REC"]["CUS"] == 49
    assert loja.estoque["JPA"]["CUS"] == 39

@pytest.mark.parametrize("quantidade", [0, 21])
def test_ST03_rejeitar_quantidade(ambiente, quantidade):
    cliente, loja, _ = ambiente
    resposta = criar(cliente, quantidade=quantidade)
    assert resposta.status_code == 422
    assert loja.estoque["REC"]["CUS"] == 50

def test_ST04_produto_indisponivel(ambiente):
    cliente, _, _ = ambiente
    resposta = criar(cliente, unidade="JPA", produto="TAP")
    assert resposta.status_code == 422
    assert resposta.json()["detail"] == "PRODUTO_INDISPONIVEL_NA_UNIDADE"

def test_ST05_canal_invalido(ambiente):
    cliente, _, _ = ambiente
    resposta = criar(cliente, canal="INVALIDO")
    assert resposta.status_code == 422

def test_ST06_idempotencia_mesmopedido(ambiente):
    cliente, loja, _ = ambiente
    primeira = novo(cliente)
    segunda = novo(cliente)
    assert primeira["id"] == segunda["id"]
    assert len(loja.pedidos) == 1
    assert loja.estoque["REC"]["CUS"] == 49

def test_ST07_idempotencia_conflitante(ambiente):
    cliente, loja, _ = ambiente
    novo(cliente)
    resposta = criar(cliente, quantidade=2)
    assert resposta.status_code == 409
    assert resposta.json()["detail"] == "CONFLITO_CHAVE_IDEMPOTENCIA"
    assert loja.estoque["REC"]["CUS"] == 49

def test_ST08_estoque_insuficiente(ambiente):
    cliente, loja, _ = ambiente
    loja.estoque["REC"]["CUS"] = 1
    resposta = criar(cliente, quantidade=2)
    assert resposta.status_code == 409
    assert loja.estoque["REC"]["CUS"] == 1

def test_ST09_consultar_pedido(ambiente):
    cliente, _, _ = ambiente
    pedido = novo(cliente)
    resposta = cliente.get(f"/pedidos/{pedido['id']}", headers=auth())
    assert resposta.status_code == 200
    assert resposta.json()["id"] == pedido["id"]

def test_ST10_pedido_inexistente(ambiente):
    cliente, _, _ = ambiente
    resposta = cliente.get("/pedidos/INEXISTENTE", headers=auth())
    assert resposta.status_code == 404

def test_ST11_pagamento_aprovado(ambiente):
    cliente, loja, _ = ambiente
    pedido = novo(cliente)
    resposta = evento(cliente, pedido)
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "PAGO"
    assert loja.estoque["REC"]["CUS"] == 49

def test_ST12_pagamento_recusado(ambiente):
    cliente, loja, _ = ambiente
    pedido = novo(cliente)
    resposta = evento(cliente, pedido, resultado="RECUSADO")
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "CANCELADO"
    assert loja.estoque["REC"]["CUS"] == 50

def test_ST13_timeout_e_recuperacao(ambiente):
    cliente, loja, tempo = ambiente
    pedido = novo(cliente)
    resposta = evento(cliente, pedido, resultado="TIMEOUT", evento_id="ev-timeout")
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "PENDENTE"
    assert loja.estoque["REC"]["CUS"] == 49
    tempo[0] = 601
    resposta = evento(cliente, pedido, resultado="NAO_COBRADO", evento_id="ev-reconciliado")
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "CANCELADO"
    assert loja.estoque["REC"]["CUS"] == 50

def test_ST14_expiracao_sem_reconciliacao_nao_libera_estoque(ambiente):
    cliente, loja, tempo = ambiente
    pedido = novo(cliente)
    tempo[0] = 1000
    assert loja.pedidos[pedido["id"]]["status"] == "PENDENTE"
    assert loja.estoque["REC"]["CUS"] == 49

def test_ST15_evento_duplicado_sem_efeito(ambiente):
    cliente, loja, _ = ambiente
    pedido = novo(cliente)
    a = evento(cliente, pedido)
    b = evento(cliente, pedido)
    assert a.status_code == b.status_code == 200
    assert a.json()["id"] == b.json()["id"]
    assert len(loja.eventos) == 1
    assert loja.estoque["REC"]["CUS"] == 49

def test_ST16_evento_com_dados_conflitantes(ambiente):
    cliente, _, _ = ambiente
    pedido = novo(cliente)
    evento(cliente, pedido)
    resposta = evento(cliente, pedido, resultado="RECUSADO")
    assert resposta.status_code == 409
    assert resposta.json()["detail"] == "CONFLITO_EVENTO"

def test_ST17_evento_assinatura_invalida(ambiente):
    cliente, loja, _ = ambiente
    pedido = novo(cliente)
    resposta = evento(cliente, pedido, assinatura_correta=False)
    assert resposta.status_code == 401
    assert loja.pedidos[pedido["id"]]["status"] == "PENDENTE"

def test_ST18_evento_valor_divergente(ambiente):
    cliente, loja, _ = ambiente
    pedido = novo(cliente)
    resposta = evento(cliente, pedido, valor=1)
    assert resposta.status_code == 422
    assert loja.pedidos[pedido["id"]]["status"] == "PENDENTE"

def test_ST19_acesso_sem_credenciais(ambiente):
    cliente, _, _ = ambiente
    pedido = novo(cliente)
    resposta = cliente.get(f"/pedidos/{pedido['id']}")
    assert resposta.status_code == 401

def test_ST20_acesso_outro_cliente(ambiente):
    cliente, _, _ = ambiente
    pedido = novo(cliente)
    resposta = cliente.get(f"/pedidos/{pedido['id']}", headers=auth("cliente-02"))
    assert resposta.status_code == 403

def test_ST21_gerente_outra_unidade(ambiente):
    cliente, _, _ = ambiente
    pedido = novo(cliente, unidade="JPA")
    resposta = cliente.get(f"/pedidos/{pedido['id']}", headers=auth("gerente-rec"))
    assert resposta.status_code == 403
    assert cliente.get(f"/pedidos/{pedido['id']}", headers=auth("matriz")).status_code == 200

def test_ST22_fluxo_preparo_e_transicao_invalida(ambiente):
    cliente, _, _ = ambiente
    pedido = novo(cliente)
    url = f"/pedidos/{pedido['id']}/status"
    tentativa = cliente.patch(url, headers=auth("cozinha-rec"), json={"status": "PRONTO"})
    assert tentativa.status_code == 409
    assert pagar(cliente, pedido).json()["status"] == "PAGO"
    for status in ("EM_PREPARO", "PRONTO", "ENTREGUE"):
        resposta = cliente.patch(url, headers=auth("cozinha-rec"), json={"status": status})
        assert resposta.status_code == 200
        assert resposta.json()["status"] == status
    assert cliente.patch(url, headers=auth("cozinha-rec"), json={"status": "EM_PREPARO"}).status_code == 409

def test_ST23_canal_simulado(ambiente):
    cliente, _, _ = ambiente
    for i, canal in enumerate(("APP", "WEB", "TOTEM", "BALCAO", "PICKUP")):
        pedido = novo(cliente, canal=canal, chave=f"c-{i}")
        assert pedido["canal"] == canal

def test_ST24_preferencias_opt_in_e_revogacao(ambiente):
    cliente, loja, _ = ambiente
    assert cliente.get("/preferencias", headers=auth()).json() == {"marketing": False, "fidelidade": False}
    dados = {"marketing": True, "fidelidade": True}
    assert cliente.put("/preferencias", headers=auth(), json=dados).json() == dados
    assert cliente.put("/preferencias", headers=auth(), json={"marketing": False, "fidelidade": False}).json() == {"marketing": False, "fidelidade": False}
    historico = [x for x in loja.auditoria if x["evento"] == "preferencias_atualizadas"]
    assert len(historico) == 2
    assert historico[-1]["marketing"] is False
    assert historico[-1]["fidelidade"] is False

def test_ST25_compra_sem_consentimento(ambiente):
    cliente, loja, _ = ambiente
    pedido = novo(cliente)
    resposta = pagar(cliente, pedido)
    assert resposta.status_code == 200
    assert loja.pontos.get("C01", 0) == 0

def test_ST26_fidelidade_com_consentimento(ambiente):
    cliente, loja, _ = ambiente
    cliente.put("/preferencias", headers=auth(), json={"marketing": False, "fidelidade": True})
    pedido = novo(cliente, quantidade=2)
    assert pagar(cliente, pedido).status_code == 200
    assert loja.pontos["C01"] == 24

def test_ST27_cancelar_estorno_e_pontos(ambiente):
    cliente, loja, _ = ambiente
    cliente.put("/preferencias", headers=auth(), json={"marketing": False, "fidelidade": True})
    pedido = novo(cliente)
    pagar(cliente, pedido)
    url = f"/pedidos/{pedido['id']}/cancelar"
    assert cliente.post(url, headers=auth("cliente-01")).status_code == 403
    primeiro = cliente.post(url, headers=auth("gerente-rec"))
    segundo = cliente.post(url, headers=auth("gerente-rec"))
    assert primeiro.status_code == segundo.status_code == 200
    assert segundo.json()["status"] == "CANCELADO"
    assert segundo.json()["reembolso"] == "ESTORNO_SIMULADO"
    assert loja.pontos["C01"] == 0
    assert loja.estoque["REC"]["CUS"] == 50

def test_ST28_confirmacao_tardia_apos_cancelamento(ambiente):
    cliente, loja, _ = ambiente
    pedido = novo(cliente)
    evento(cliente, pedido, resultado="RECUSADO", evento_id="recusa")
    resposta = evento(cliente, pedido, resultado="APROVADO", evento_id="tardia")
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "CANCELADO"
    assert resposta.json()["reembolso"] == "ESTORNO_PENDENTE"
    assert loja.estoque["REC"]["CUS"] == 50

def test_ST29_relatorio_gerente_e_matriz(ambiente):
    cliente, _, _ = ambiente
    novo(cliente, unidade="REC", chave="r")
    novo(cliente, unidade="JPA", chave="j")
    assert cliente.get("/relatorios", headers=auth()).status_code == 403
    assert cliente.get("/relatorios", headers=auth("gerente-rec")).json()["pedidos"] == 1
    assert cliente.get("/relatorios", headers=auth("matriz")).json()["pedidos"] == 2

def test_CT06_cem_tentativas_concorrentes():
    loja = Loja()
    loja.estoque["REC"]["CUS"] = 10

    def tentar(i):
        try:
            return loja.criar("C01", "REC", "CUS", 1, "WEB", f"c-{i}")["id"]
        except Falha as erro:
            assert erro.mensagem == "ESTOQUE_INSUFICIENTE"
            return None

    with ThreadPoolExecutor(max_workers=20) as executor:
        respostas = list(executor.map(tentar, range(100)))
    aceitos = [r for r in respostas if r is not None]
    assert len(aceitos) == 10
    assert len(set(aceitos)) == 10
    assert respostas.count(None) == 90
    assert loja.estoque["REC"]["CUS"] == 0

def test_CT17_cem_pedidos_rastreaveis(ambiente):
    cliente, loja, _ = ambiente
    loja.estoque["REC"]["CUS"] = 150
    ids = set()
    for i in range(100):
        pedido = novo(cliente, chave=f"l-{i}")
        ids.add(pedido["correlation_id"])
        assert pagar(cliente, pedido).status_code == 200
    assert len(ids) == 100
    for identificador in ids:
        tipos = {e["evento"] for e in loja.auditoria if e["correlation_id"] == identificador}
        assert {"pedido_criado", "reserva_realizada", "pagamento_aprovado"} <= tipos
    assert cliente.get("/auditoria", headers=auth("cliente-01")).status_code == 403
    assert cliente.get("/auditoria", headers=auth("matriz")).status_code == 200
    assert all("senha" not in str(x).lower() for x in loja.auditoria)

def test_CT19_interface_disponivel(ambiente):
    cliente, _, _ = ambiente
    resposta = cliente.get("/demo")
    assert resposta.status_code == 200
    assert 'name="viewport"' in resposta.text
    assert 'id="marketing"' in resposta.text
    assert 'id="fidelidade"' in resposta.text

def test_CT12_reconciliacao_local_de_cem_pedidos(ambiente):
    cliente, loja, _ = ambiente
    loja.estoque["REC"]["CUS"] = 150
    pedidos = [novo(cliente, chave=f"rec-{i}") for i in range(100)]
    for i, pedido in enumerate(pedidos):
        assert evento(cliente, pedido, resultado="TIMEOUT", evento_id=f"t-{i}").status_code == 200
        assert evento(cliente, pedido, resultado="APROVADO", evento_id=f"a-{i}").status_code == 200
    assert all(loja.pedidos[p["id"]]["status"] == "PAGO" for p in pedidos)
    assert loja.estoque["REC"]["CUS"] == 50
    assert len(loja.eventos) == 200
