# Q2 — Qualidade mensurável, consumo e verificação real

Esta etapa não confunde **controles locais** com garantias que só o provedor pode dar.

## 1. Consumo e teto financeiro

A CLI do Codex não fornece ao controlador uma confirmação de orçamento rígido,
reservada no servidor ANTES de cada requisição interna. O processo pode realizar
várias solicitações e o relatório de tokens pode chegar depois da cobrança.

- `budgets.mode = "observed"`: reservas locais atômicas, custos/tokens reportados,
  interrupção de processos ao detectar estouro, retenção de reserva quando falta
  telemetria. Um excesso remoto em voo ainda pode ser cobrado.
- `budgets.mode = "strict"`: **recusa** inferência pelo adaptador `codex exec`.
  Trata-se de proteção fail-closed: não finge um limite monetário inexistente.
- `run_cost_usd` e `task_cost_usd` precisam de tarifas explícitas por modelo.
  Com modelos/contas de assinatura, tokens reportados não equivalem a preço de API.
- Se uma garantia financeira externa for requisito, use um provedor/gateway com
  quota transacional verificável e mediação de TODAS as requisições, ou uma
  autorização financeira externa; não tente prometer isso com hooks locais.

A própria documentação do faturamento pré-pago da API afirma que a interrupção
pode não ser imediata após o esgotamento de créditos. Limites de recarga não são
limites de consumo. Referência:
https://help.openai.com/en/articles/8264644-setting-up-and-managing-prepaid-api-billing

## 2. Qualidade: provar sensibilidade do oráculo, não presumir equivalência

A versão 0.3 adiciona *mutation probes* ao contrato pré-aprovado pelo Astra.
Cada mutação declara arquivo, substituição exata, check e uma assinatura
identificável da falha esperada. Após os testes verdes, o controlador:

1. cria uma Git worktree descartável a partir da revisão corrente;
2. copia apenas o diff do candidato e aplica UMA mutação determinada pela Spec;
3. executa o check de aceite protegido sem credenciais de modelo;
4. exige uma falha **com a mensagem específica** que demonstra a regressão;
5. guarda logs verificáveis vinculados ao snapshot e destrói a worktree mutante.

Mutante que sobreviver, timeout, check indisponível ou falha por motivo errado
bloqueiam a entrega. `verification.mutation_policy = "critical"` exige ao menos
uma mutação contratada para cada alteração crítica; `"all"` exige para todas;
`"optional"` permite exceção explícita. Os arquivos de testes continuam selados.

Exemplo adicionado ao `test_contract` de uma tarefa:

```json
"mutations": [{
  "file": "src/textutil.py",
  "find": "return value.strip()",
  "replace": "return value",
  "check": "acceptance",
  "failure_marker": "AssertionError"
}]
```

**O que isso NÃO prova:** totalidade dos bugs possíveis, ausência de falhas
semânticas, independência estatística de modelos da mesma família ou
qualidade idêntica entre Astra/Sol/Terra/Luna. Para casos críticos continua
necessária revisão externa humana assinada e testes adversariais.

## 3. Smoke autenticado e benchmark reproduzível

Um ambiente sem credenciais não pode executar um teste autenticado: a versão
0.3 oferece o comando **opt-in**, mas não registra uma execução inexistente.

```bash
codex-orchestrator --repo /repo smoke-models --acknowledge-paid-requests
```

Ele realiza uma chamada **read-only** para cada modelo configurado, com saída
estruturada e telemetria obrigatórias. Verifica no instante a capacidade de
responder ao modelo solicitado. Não executa patches nem valida hooks ou
qualidade de programação. Para o fluxo completo, execute o plano demo em um
repositório de teste com Codex autenticado e confirme o gate resultante.

Compare N execuções de um MESMO plano e mesmo commit inicial com:

```bash
codex-orchestrator --repo /repo benchmark RUN_ID_1 RUN_ID_2 [RUN_ID_3 ...]
```

O relatório fornece: total de execuções, gates válidos, proporção observada de
aceitação, intervalo de Wilson 95%, falhas, revisões, mutantes detectados,
tokens/custo reportados e custo por entrega efetivamente aceita. Não atribui
nota de qualidade inventada e identifica amostras pequenas como exploratórias.
Comparações de eficácia exigem amostra significativa, mesmo oráculo/ambiente,
controle de variáveis, inspeção de defeitos e avaliação humana independente.

**Garantia observável versus não garantida:**

| Item | Verificado localmente | Requer ambiente/ator externo |
|---|---|---|
| Integridade do teste de aceite | Hash, baseline, mutantes | Qualidade do conjunto de casos escolhido |
| Revisão IA | Resposta e snapshot exatos | Independência estatística, descoberta de todo bug |
| Auditoria humana crítica | Assinatura Ed25519 verificada | Qualidade de julgamento humano |
| Consumo | Reserva, telemetria e cancelamento local | Teto financeiro garantido pelo provedor |
| Modelos | Smoke opt-in implementado e testado com fake | Login, permissões e execução autenticada real |

Nunca classifique uma simulação (`tests/fake_codex.py`) como teste com modelos reais.
