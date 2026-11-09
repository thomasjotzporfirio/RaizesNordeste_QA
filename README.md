# Raízes do Nordeste — Qualidade de Software

Projeto acadêmico desenvolvido para a trilha de Qualidade de Software.
O demonstrador utiliza Python e FastAPI para simular pedidos, controle de estoque, pagamentos, cancelamentos e permissões de usuários fictícios.

## Arquivos

- `app.py`: aplicação e API.
- `test_app.py`: testes automatizados.
- `carga.js`: testes de carga e estresse com k6.
- `requirements.txt`: dependências Python.
- `evidencias/`: registros dos testes.

## Execução

Instale as dependências:

```powershell
py -m pip install -r requirements.txt
```

Execute os testes:

```powershell
py -m pytest -v test_app.py
```

Inicie a aplicação:

```powershell
py -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Acesse `http://127.0.0.1:8000/demo` para utilizar a interface demonstrativa ou `/docs` para consultar a API.

## Limitações

O projeto utiliza dados fictícios, armazenamento em memória e pagamento simulado. Não representa um sistema comercial em produção.
