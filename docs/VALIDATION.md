# Validação — v0.3.0 / Q2

Data de desenvolvimento: 04–05/11/2026 (UTC). Linux, Python 3.13, Git e OpenSSL.

## Resultado local

A execução completa da suíte de 141 testes em 489,131 segundos foi aprovada (`OK`), antes da última inclusão de um teste de isolamento de backend simulado. Após essa inclusão, os 5 testes específicos de smoke passaram. O fluxo `test_two_approved_repeated_trials_remain_exploratory` também passou isoladamente. A verificação definitiva da versão publicada é a matriz da CI (Python 3.11, 3.12 e 3.13).

```bash
python -m unittest discover -s tests -v
python -m compileall -q codex_orchestrator
python -m pip install --no-build-isolation --no-deps .
```

Os testes executam o controlador real, subprocessos, Git, worktrees descartáveis, verificações de código, contabilidade e assinaturas Ed25519 com OpenSSL. **O backend Codex é simulado explicitamente** por `tests/fake_codex.py`. Isso não comprova execução real dos modelos nem equivalência de qualidade entre eles.

## Q2: Cenários novos

- Mutação pré-aprovada precisa provocar falha específica do teste de aceite; mutante sobrevivente, timeout, erro inesperado ou evidência adulterada bloqueiam.
- Tarefa crítica exige oráculo com sensibilidade a mutação, além de testes independentes e aprovação humana externa.
- `smoke-models` exige consentimento explícito para chamadas pagas, recusa backends simulados por padrão, valida esquema, modelo e telemetria.
- Benchmark só compara execuções com a mesma Spec e mesmo commit inicial, com taxas observadas, intervalo de Wilson e amostra insuficiente sinalizada. Simulação é declarada como simulação.

## Limites externos não ocultados

Sem CLI Codex autenticado nem acesso real aos modelos nesse ambiente: smoke autenticado e E2E real com IA permanecem **não executados**. Um smoke opt-in com os modelos e o projeto consumidor é necessário antes de produção.

Em `observed`, limites de custo/tokens são medidos por telemetria reportada **após** a chamada; podem existir cobranças enquanto uma requisição está em voo. Em `strict`, a CLI é recusada quando não comprova quota transacional externa. Garantia absoluta de ausência de defeitos ou independência estatística entre modelos também não é tecnicamente demonstrável por testes finitos. Consulte [Q2-VERIFICATION.md](Q2-VERIFICATION.md).
