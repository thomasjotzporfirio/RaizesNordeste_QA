import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';

const estresse = __ENV.MODO === 'estresse';

export const options = {
  ...(estresse ? {
    stages: [
      { duration: '20s', target: 50 },
      { duration: '20s', target: 100 },
      { duration: '20s', target: 200 },
      { duration: '20s', target: 0 },
    ],
  } : {}),
  thresholds: {
    http_req_duration: ['avg<1000', 'p(95)<2000'],
    http_req_failed: ['rate<0.01'],
  },
};

export default function () {
  const chave = `carga-${Date.now()}-${exec.vu.idInTest}-${exec.scenario.iterationInTest}`;
  const resposta = http.post(
    'http://127.0.0.1:8000/pedidos',
    JSON.stringify({ unidade: 'PERF', produto: 'CUS', quantidade: 1, canal: 'WEB' }),
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer cliente-01',
        'Idempotency-Key': chave,
      },
      timeout: '10s',
    },
  );
  check(resposta, {
    'pedido HTTP 201': r => r.status === 201,
    'pedido pendente': r => {
      if (r.status !== 201) return false;
      try { return r.json('status') === 'PENDENTE'; }
      catch (e) { return false; }
    },
  });
  sleep(5);
}