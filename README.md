# Codex Orchestrator

**Skill, hooks e controlador determinístico para implementar com Luna/Terra/Sol, revisar no nível seguinte e validar a entrega com Astra.**

Versão **0.3.0 — Q1 hardening + Q2 verificação baseada em evidências**. Python 3.11+, biblioteca padrão no runtime. O roteamento é código, não uma sequência de modelos encaminhando mensagens. Uma tarefa Luna não atravessa Sol e Terra antes de começar.

> **Validação:** a suíte executa processos, Git, worktrees, checks, persistência e assinaturas criptográficas reais; somente o serviço Codex é substituído por um CLI de teste. Isso não é um benchmark de qualidade nem um smoke test com modelos autenticados. Veja [VALIDATION.md](docs/VALIDATION.md).

## Índice

- [Arquitetura e oito controles](#arquitetura-e-oito-controles)
- [Instalação](#instalação)
- [Configuração](#configuração)
- [Plano e contrato de testes](#plano-e-contrato-de-testes)
- [Uso pela skill](#uso-pela-skill)
- [Fluxos ponta a ponta](#fluxos-ponta-a-ponta)
- [Paralelismo e integração](#paralelismo-e-integração)
- [Orçamentos: o que é garantido](#orçamentos-o-que-é-garantido)
- [Aprovação humana assinada](#aprovação-humana-assinada)
- [Hooks, evidências e recuperação](#hooks-evidências-e-recuperação)
- [Migração da 0.1](#migração-da-01)
- [Testes e demonstração](#testes-e-demonstração)
- [Validação independente e comparação de qualidade](#validação-independente-e-comparação-de-qualidade)
- [Limites explícitos](#limites-explícitos)
- [Estrutura e documentação oficial](#estrutura-e-documentação-oficial)

## Arquitetura e oito controles

```text
Astra: plano + revisão prévia do contrato de testes
                         │
                Controlador determinístico
                         │
           risco + dependências + orçamento
             ┌───────────┼───────────┐
             ▼           ▼           ▼
           Luna        Terra        Sol
             │           │           │
        checks reais  checks reais  checks reais
             │           │           │
        Terra revisa  Sol revisa   Astra revisa
             └───────────┼───────────┘
                         │
             integração ordenada por tarefa
             + checkpoints intermediários
                         │
             Astra valida o conjunto final
                         │
          alteração crítica? revisão adversarial,
          checks negativos e assinatura externa
                         │
              gate → branch/commit aprovado
```

| Ponto auditado | Controle executável na v0.2 |
|---|---|
| 1. Classificação errada | Pisos de risco por domínio, caminhos, código e diff; reavaliação após edição e herança em filhos/reparos. |
| 2. Testes feitos para passar | Contrato de aceite anterior à implementação, checks reais de baseline, revisão prévia pelo Astra e arquivos de teste selados. |
| 3. Integração tardia | Checkpoints a cada N tarefas, após tarefas críticas e no fim de lote parcial; dependentes não avançam quando falham. |
| 4. Escritores concorrentes | Worktree por tarefa, conflitos de leitura/escrita serializados, ordem estável de integração e nova revisão quando a base muda. |
| 5. Escalonamento sem contexto | Handoff factual limitado, com tentativas, falhas, patch e referências às evidências; sem transcrições. |
| 6. Consumo sem teto | Reservas atômicas, limites de tentativas/chamadas/tokens/custo, cobrança por subárvore e interrupção por consumo observado. |
| 7. Pontos cegos correlacionados | Revisão sem narrativa do executor; para mudanças críticas, revisão adversarial separada, checks negativos e aprovação externa assinada. |
| 8. Revisão redundante | Cápsulas vinculadas à spec, arquivos lidos/alterados, base, snapshot, modelo, logs e resposta; reuso somente enquanto válidas. |

**Os limites do provedor não são escondidos nem falsamente declarados resolvidos:** modelos da mesma família não se tornam estatisticamente independentes por rodarem em contextos separados. E limites observados do CLI não são um teto absoluto da fatura do provedor. Há controles compensatórios e comportamento fail-closed, detalhados abaixo.

## Instalação

Requisitos: Linux/macOS ou WSL, Python **3.11+**, Git e Codex CLI instalado/autenticado. **OpenSSL 3+ com Ed25519** é necessário para aprovações humanas assinadas. O ambiente da CI também o utiliza. Windows nativo não foi validado para instalação dos hooks.

```bash
python3 -m venv ~/.venvs/codex-orchestrator
source ~/.venvs/codex-orchestrator/bin/activate
python -m pip install \
  "git+https://github.com/A1b3rt0M3rcad0/codex-orchestrator.git"

codex-orchestrator --repo /caminho/do/projeto install
```

O instalador adiciona a skill em `.agents/skills/codex-orchestrator`, um perfil em `.codex/config.toml` e handlers próprios em `.codex/hooks.json`. Preserva configurações, handlers e instruções existentes. Arquivos de skill editados manualmente não são sobrescritos. Não grava credenciais, não autoriza hooks e não executa modelos.

Na sessão do Codex, abra **`/hooks`**, revise os comandos e conceda confiança explicitamente. Os gates do controlador continuam obrigatórios mesmo sem hooks.

## Configuração

Edite `orchestrator.toml`. O instalador gera `version = 2`, modelos e limites, mas deixa os checks sem comandos ativos: **não existe teste fictício que sempre passa**.

```toml
version = 2

[runtime]
command = ["codex"]
parallel_workers = 1
checkpoint_every = 2
checkpoint_repairs = 1
max_calls = 80
max_tasks = 64
max_global_rounds = 2
model_timeout = 900
check_timeout = 300

[attempts]
luna = 2
terra = 2
sol = 2

[budgets]
mode = "observed"
run_tokens = 2000000
task_tokens = 500000
call_tokens = 100000
task_calls = 24

[policy]
minimum_tier = "luna"
extra_patterns = ["src/finance/*", "src/tenant_boundary/*"]

[verification]
require_human = "critical"
mutation_policy = "critical" # ou all / optional
trusted_signers = ""

[checks]
# Substitua pelos comandos reais do seu projeto:
# acceptance = ["{python}", "-m", "unittest", "discover", "-s", "tests/acceptance", "-v"]
# smoke = ["npm", "run", "test:integration"]
# e2e = ["npm", "run", "test:e2e"]
# adversarial = ["npm", "run", "test:negative"]
```

`{python}` significa o interpretador do controlador. Comandos são arrays de argumentos, sem shell implícito. Formatadores precisam estar em modo check. Dependências, bancos de teste e serviços devem existir nas worktrees; o controlador não instala nem inventa esses serviços. Para executar em paralelo, os próprios checks precisam isolar bancos, portas e arquivos externos — worktrees não isolam recursos fora do checkout.

Os papéis configuráveis padrão são:

| Papel | Model ID | Implementação | Revisão |
|---|---|---|---|
| Astra | `gpt-6-astra` | reservado ao planejamento externo | `max` |
| Sol | `gpt-6.1-sol` | `low` | `high` |
| Terra | `gpt-6.1-terra` | `medium` | `high` |
| Luna | `gpt-6-luna` | `max` | não é revisor |

Cada `[models.PAPEL]` permite `model`, `effort`, `review_effort` e `family`. Não há fallback silencioso. Disponibilidade e esforço suportado dependem da conta/versão do Codex; `doctor` verifica flags locais, não acesso remoto. O metadado `family` informa auditoria; renomeá-lo não prova independência nem remove a assinatura exigida para mudanças críticas.

### Política de risco

Auth/autorização/tenancy, pagamentos, migrations, concorrência, segredos e infraestrutura possuem detectores embutidos. A análise usa metadados, caminhos reais sob os escopos, conteúdo e diff, inclusive exclusões. Tags declaradas, regras adicionais e conteúdo binário/grande também podem elevar a classificação. O piso crítico é **Sol implementa → Astra revisa**, com verificação adversarial e assinatura externa.

Regras são monotônicas: o plano não desativa detectores e filhos não descartam o risco herdado. Uma descoberta após edição preserva o patch e exige reavaliação pelo executor superior. Regras de caminhos específicas do projeto devem complementar os detectores; nenhuma heurística lexical garante descobrir todo risco semântico.

## Plano e contrato de testes

Antes da implementação, o Astra/autor da Spec define critérios observáveis e **testes principais comprometidos no Git**. O executor pode acrescentar testes suplementares em seu escopo, mas não alterar os testes de aceite selados. O controlador executa o baseline e chama Astra, em contexto read-only, para revisar o contrato de testes antes de abrir qualquer tarefa.

Exemplo completo: [examples/demo.plan.json](examples/demo.plan.json).

```json
{
  "version": 2,
  "objective": "Normalizar rótulos conforme o contrato",
  "acceptance": ["A normalização atende aos casos de aceite"],
  "integration_checks": ["unit"],
  "checkpoint_checks": ["unit"],
  "tasks": [{
    "id": "TEXT-1",
    "tier": "luna",
    "objective": "Implementar normalize_label",
    "scope": ["textutil.py"],
    "reads": [],
    "acceptance": ["Espaços redundantes são normalizados", "Valores não textuais geram TypeError"],
    "checks": ["unit"],
    "depends_on": [],
    "risk_tags": [],
    "test_contract": {
      "files": ["tests/test_textutil.py"],
      "cases": [
        {"criterion": "Espaços redundantes são normalizados", "check": "unit", "baseline": "fail", "rationale": "A função está incompleta no baseline"},
        {"criterion": "Valores não textuais geram TypeError", "check": "unit", "baseline": "fail", "rationale": "O contrato de tipos ainda não foi implementado"}
      ]
    },
    "adversarial_checks": []
  }]
}
```

Cada critério precisa de mapeamento a pelo menos um check. `baseline` é `pass` ou `fail`, com justificativa; falha esperada deve ser um contraexemplo conhecido, não runner quebrado. Todos os campos são obrigatórios. Critérios e checks de filhos são subconjuntos do contrato já revisado: um executor não pode inventar um novo oráculo permissivo.

Para alterações críticas, `test_contract.mutations` é obrigatório por padrão:

```json
"mutations": [{"file":"src/textutil.py","find":"return value.strip()","replace":"return value","check":"acceptance","failure_marker":"AssertionError"}]
```

O controlador aplica cada mutação em uma worktree descartável **após os checks
verdes**, exige que o teste de aceite falhe com o marcador previsto e grava
prova vinculada ao snapshot. Um mutante sobrevivente, timeout ou falha por motivo
errado bloqueia o fluxo. Use `mutation_policy = "all"` para exigir o mecanismo
em toda tarefa, ou `"optional"` para permitir tarefas sem sondas. A mutação é
especificada antes de executar qualquer modelo, junto ao contrato revisado pelo
Astra. Veja [Q2-VERIFICATION.md](docs/Q2-VERIFICATION.md).

`scope` é permissão de escrita; `reads` declara dependências relevantes ao scheduler e ao reuso de revisões. Use arquivos exatos ou prefixos terminados em `/`, nunca globs. Arquivos de controle e todos os testes principais são protegidos contra escrita, inclusive por outras tarefas.

**Testes independentes não equivalem a testes perfeitos.** O controle garante proveniência temporal, imutabilidade e revisão do contrato. A suficiência dos casos exige julgamento; mudanças críticas mantêm o gate humano.

## Uso pela skill

```bash
cd /caminho/do/projeto
codex-orchestrator doctor
codex-orchestrator validate plano.json
# Revise e faça commit da configuração, dos testes principais e do plano.
codex --profile codex-orchestrator
```

Na sessão:

```text
$codex-orchestrator
Implemente [objetivo] conforme as regras deste projeto.
Prepare o contrato de testes antes da implementação, classifique o risco,
use os executores adequados e conclua somente após os gates exigidos.
```

Execução direta equivalente:

```bash
codex-orchestrator run plano.json
codex-orchestrator status RUN_ID
codex-orchestrator gate RUN_ID
```

O profile principal não é usado como roteador a cada operação: o Python chama diretamente `codex exec` com o modelo/papel de cada etapa. O planejamento inicial ocorre no Codex/autor da Spec; o runtime valida e executa o plano recebido, não transforma texto livre em tarefas por conta própria.

## Fluxos ponta a ponta

### Luna

```text
Spec + testes principais → baseline → Astra revisa contrato
→ política permite Luna → reserva orçamento → Luna implementa em sua worktree
→ checks do contrato
  falha: Luna recebe o diagnóstico e corrige, sem pagar revisão prematura
→ Terra revisa em contexto novo/read-only
  reprova: Luna corrige; esgotamento promove para Terra e revisor vira Sol
→ cápsula local → integração ordenada
  base diferente: aplicar em outra worktree → checks + nova revisão
→ checkpoint quando devido → Astra global → gate
```

### Terra

```text
Contrato revisado → política/orçamento → Terra implementa
  pode propor uma única delegação inferior, antes de editar:
  Luna implementa em outra worktree → checks → Terra revisa → integra no pai
→ Terra integra a própria tarefa → checks → Sol revisa
  reprova: Terra corrige; esgotamento promove para Sol/Astra
→ cápsula → integração ordenada + revalidação da base → checkpoints
→ Astra global → gate
```

### Sol e mudanças críticas

```text
Contrato revisado → política/orçamento → Sol implementa
  delegação inferior só é aceita se o piso de risco do filho permitir
→ checks → Astra revisa
→ se crítico: checks adversariais + nova revisão adversarial sem narrativa anterior
→ cápsula → integração + checkpoint obrigatório
→ Astra global
→ se crítico: awaiting_human → revisão externa + assinatura Ed25519 verificada
→ gate aprovado
```

Esgotamento do Sol bloqueia para replanejamento; Astra não passa a autor e aprovador do mesmo patch. Reparo global usa o mesmo escopo/oráculo, herda o orçamento e as tentativas da tarefa original. Uma descoberta que exige novo escopo interrompe o fluxo em vez de expandir silenciosamente a autorização.

## Paralelismo e integração

`parallel_workers = 1` é o padrão conservador. Valores de 2 a 16 habilitam tarefas de topo independentes. O scheduler respeita `depends_on`, colisões de escrita/escrita e leitura/escrita e o limite do lote até o próximo checkpoint. Tarefas críticas executam sozinhas; filhos de um mesmo pai executam sequencialmente.

Cada tarefa tem branch/worktree próprias, separadas da integração. O resultado é integrado na ordem estável do plano, não na ordem de conclusão. Se a base mudou, o patch é aplicado em uma nova candidata, testado e revisado novamente. Conflitos preservam os checkouts e bloqueiam; não existe merge forçado ou resolução silenciosa por IA.

`checkpoint_checks` devem validar uma implementação parcial coerente. Pode-se usar E2E nessa lista quando o ambiente permite. A suite final fica em `integration_checks`. Checks intermediários que exigem funcionalidades ainda não implementadas precisam ser definidos por marcos apropriados no plano; não desative o gate para contornar isso.

## Orçamentos: o que é garantido

O controlador reserva uma franquia por chamada **antes de iniciar o processo**, sob lock compartilhado, levando em conta chamadas simultâneas. Há tetos de tokens por execução, tarefa/subárvore e papel, além de chamadas, tentativas, tempo, tamanho de prompt/log e rodadas de reparo. Filhos/reparos não escapam da conta do pai.

Para limites monetários, configure `run_cost_usd`/`task_cost_usd` como strings decimais e informe `[prices.astra]`, `[prices.sol]`, `[prices.terra]`, `[prices.luna]`, cada uma com `input`, `cached_input` e `output` por milhão de tokens. A configuração rejeita preços incompletos quando existe um teto financeiro. **O projeto não embute preços comerciais nem transforma consumo de assinatura em dólares.**

Uso observado libera a parte não consumida da reserva. Telemetria ausente/inválida mantém a reserva e bloqueia novas chamadas. Entrada em cache já pertence à entrada; raciocínio já pertence à saída. Esses campos não são somados duas vezes. Telemetria de hooks e de fim de turno não é adicionada como se fossem consumos distintos.

**Limitação material do adaptador CLI:** o provedor pode cobrar uma requisição em voo antes de o consumo ser informado. O monitor cancela o processo local quando observa excesso; esse cancelamento não desfaz faturamento remoto. Portanto:

- `mode = "observed"`: tetos de admissão e disjuntor de consumo observado; não é garantia de fatura máxima.
- `mode = "strict"`: recusa inferência, pois o adaptador CLI não comprova um teto rígido por requisição. Não degrada silenciosamente para o modo observado.

`PostModelRequest` melhora a granularidade quando disponível/confiado; esse hook é diagnóstico, não uma autorização capaz de vetar requisições. O tempo de polling não elimina a janela de consumo remoto. O relatório distingue uso ausente e custo não configurado de zero.

## Aprovação humana assinada

Obrigatória para mudanças críticas; `require_human = "all"` também a exige nas demais. O controlador verifica uma assinatura Ed25519, mas **não cria, pede nem utiliza a chave privada**. O signatário deve revisar o diff, os testes e a proposta de aprovação em um ambiente inacessível aos executores.

Prepare fora do projeto uma allowlist JSON com a chave **pública** autorizada:

```json
{
  "version": 1,
  "identities": {
    "revisor@empresa": "-----BEGIN PUBLIC KEY-----\n...CHAVE PUBLICA ED25519...\n-----END PUBLIC KEY-----\n"
  }
}
```

Defina o caminho absoluto em `verification.trusted_signers`. O conteúdo da allowlist é fixado por hash ao iniciar a run. O placeholder acima precisa ser substituído por uma chave pública real; não é uma chave utilizável.

Quando o estado for `awaiting_human`:

```bash
codex-orchestrator approval-request RUN_ID > approval.json
```

No ambiente externo do signatário, depois de revisar o payload e as evidências:

```bash
openssl pkeyutl -sign -rawin -inkey /local/privado/revisor.pem \
  -in approval.json -out approval.sig
```

Traga **apenas a assinatura** de volta e verifique:

```bash
codex-orchestrator approve RUN_ID \
  --statement approval.json --signature approval.sig --identity revisor@empresa
codex-orchestrator gate RUN_ID
```

A declaração inclui namespace, run, nonce, snapshot, hash da Spec, configuração e evidências. Código/evidências alterados, chave não autorizada, assinatura adulterada ou declaração de outra run não aprovam. A assinatura prova controle da chave e aceite daquele payload, não que o humano realizou uma revisão perfeita. Nunca deixe a chave privada acessível ao modelo nem confunda esse mecanismo com isolamento multiusuário do sistema operacional.

## Hooks, evidências e recuperação

| Hook | Papel |
|---|---|
| `SessionStart` | Informa o identificador para vincular somente a sessão solicitada. |
| `Stop` | Verifica o gate, não afirma sucesso em bloqueios e impede continuações infinitas. |
| `PostModelRequest` | Registra uso compacto opcional dos processos gerenciados; não lê transcrições nem inicia modelos. |

Não existe launcher de revisão em `SubagentStop`: processos `codex exec` não são as threads nativas de `spawn_agent`. O controlador mantém a máquina de estados fora dos agentes. Hooks não substituem transições e validações do runtime.

Artefatos ficam em `GIT_COMMON_DIR/codex-orchestrator/runs/RUN_ID/`: plano, estado, requests/responses, JSONL, logs de checks, handoffs, cápsulas e atestados. Contêm código e dados do projeto; trate-os como material privado. Worktrees ficam fora do checkout e dos metadados Git. A branch final é `orchestrator/RUN_ID`; branches de tarefa usam `orchestrator-task/RUN_ID/...`.

```bash
codex-orchestrator list
codex-orchestrator status RUN_ID
codex-orchestrator resume RUN_ID
```

Retomada verifica integridade e **não zera orçamento/tentativas**. Aprovação só vale para o conteúdo validado. Uma edição externa, assinatura obsoleta ou evidência adulterada impede o gate. Intenções de integração persistidas permitem reconhecer um fast-forward concluído antes de uma interrupção.

Telemetria desconhecida não é resolvida por tentativa cega: a run permanece bloqueada para investigação. Não existe comando de zerar custos nem autoaprovação de consumo desconhecido. Para replanejar, preserve os artefatos anteriores, investigue a causa e submeta explicitamente um novo plano, contabilizando também o gasto anterior externamente.

O runtime não faz auto-push nem merge na branch de trabalho do usuário. Depois do gate, siga o workflow do projeto. Se o destino avançar ou a integração mudar código, execute os testes/revisões necessários sobre o resultado integrado; o gate anterior não se estende automaticamente ao novo snapshot.

## Migração da 0.1

```bash
python -m pip install --upgrade \
  "git+https://github.com/A1b3rt0M3rcad0/codex-orchestrator.git"
codex-orchestrator install
codex-orchestrator migrate-config
```

`install` preserva `orchestrator.toml` v1 e escreve um exemplo v2. `migrate-config` é explícito: guarda `orchestrator.toml.v1.bak`, preserva comandos/modelos e converte `attempts_per_tier` em limites por papel. Revise a política nova e faça commit do setup.

**Planos v1 não são convertidos por adivinhação.** Acrescente `reads`, `risk_tags`, `test_contract`, `adversarial_checks` e `checkpoint_checks`, escreva os testes principais e valide o plano v2. Runs v1 não são retomadas pelo runtime v2; finalize com a 0.1 ou replaneje preservando o trabalho. Não há caminho legado que contorne os controles novos.

## Testes e demonstração

```bash
python -m pip install --no-deps .
python -m unittest discover -s tests -v
python -m compileall -q codex_orchestrator
```

A suíte inclui caminhos felizes e falhas: risco, oráculos, corrupção de evidência, escalonamento, checkpoints, paralelismo, conflitos, budgets, retomada, instalação e assinaturas Ed25519 reais com chaves **temporárias de teste**. O CLI simulado está isolado em `tests/fake_codex.py`; nunca é backend padrão de produção.

### Smoke test real — opt-in, consome a conta

Com Codex autenticado, execute em uma cópia temporária do exemplo:

```bash
cp -R examples/demo_project /tmp/codex-orchestrator-demo
cp examples/demo.plan.json /tmp/codex-orchestrator-demo/plan.json
cd /tmp/codex-orchestrator-demo
git init -b main
codex-orchestrator install
```

Configure o check `unit` do exemplo:

```toml
[checks]
unit = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"]
```

Revise/autorize hooks e o uso dos modelos, depois:

```bash
git add .
git commit -m "Prepare independent acceptance contract"
codex-orchestrator doctor
codex-orchestrator validate plan.json
codex-orchestrator run plan.json
codex-orchestrator gate RUN_ID
```

Os testes do exemplo falham no baseline por comportamento não implementado. Sem Codex autenticado, não há smoke test real. `doctor` passando, testes simulados passando e assinatura válida não demonstram qualidade dos modelos.

## Validação independente e comparação de qualidade

O harness agora possui um verificador de **sensibilidade do teste de aceite**.
Os testes principais precisam detectar regressões especificadas anteriormente
pelo autor da Spec, não apenas retornar sucesso. Logs e sondas são guardados com
hash e revisados na cápsula; alterações críticas exigem esse controle mais
revisão adversarial e aprovação humana assinada.

Para verificar os **modelos reais configurados**, sem alterar código, execute
apenas quando desejar consumir chamadas do Codex:

```bash
codex-orchestrator --repo /caminho/projeto smoke-models --acknowledge-paid-requests
```

O smoke verifica o acesso e a saída estruturada dos quatro modelos. **Não é um
teste E2E do fluxo.** Depois execute o exemplo do README em um repositório
temporário com modelos autenticados, e confira o `gate` final.

Para comparar qualidade/custo de múltiplas execuções idênticas:

```bash
codex-orchestrator --repo /caminho/projeto benchmark RUN_ID_1 RUN_ID_2
```

O benchmark exige o mesmo commit-base e hash do plano; mede gates aprovados,
intervalos estatísticos, revisões, mutações detectadas, tokens e custos reais
reportados. **Não inventa notas de 0–100 nem infere qualidade idêntica entre
modelos.** Amostras pequenas recebem a classificação `exploratory`.

A matriz de propriedades verificáveis e dependências externas está em
[docs/Q2-VERIFICATION.md](docs/Q2-VERIFICATION.md).

## Limites explícitos

O controlador implementa política de execução e evidência; não prova correção semântica total. A suficiência dos testes, a declaração de dependências e as regras específicas de risco continuam exigindo revisão. Contextos separados não eliminam correlação entre modelos; aprovação humana é uma fonte adicional de decisão, não garantia matemática.

Worktrees isolam código, não rede, credenciais, bancos ou processos externos. Checks são código do projeto executado sob o usuário do controlador. Use somente comandos confiáveis e isolamento de SO/container apropriado quando necessário. Estado local e allowlist não são um serviço de autorização contra um administrador hostil da mesma máquina. Assinaturas dependem de manter a chave privada fora do alcance dos executores.

O sistema não fornece daemon, MCP server, gerenciamento de dependências/serviços, publicação automática, garantias de economia ou faturamento rígido via CLI. Não muda configurações de conta nem contorna a sandbox do Codex.

## Estrutura e documentação oficial

```text
codex_orchestrator/
  assets/       skill e referência de workflow
  contracts.py  plano, respostas e validação estrita
  config.py     configuração e limites
  policy.py     pisos de risco e oráculos selados
  budget.py     reservas e telemetria
  model.py      adaptador codex exec
  evidence.py   handoffs, cápsulas e assinatura externa
  runtime.py    máquina de estados, scheduler e integração
  system.py     Git, processos, locks e arquivos
  hooks.py      ciclo de vida e telemetria opcional
  install.py    instalação conservadora/migração
  cli.py        comandos
examples/       baseline e contrato de demonstração
tests/          unitários e integração com CLI explicitamente simulado
docs/           matriz Q1 e relatório de validação
```

Fontes do contrato de integração, a revalidar ao atualizar o CLI:

- [Codex — execução não interativa](https://developers.openai.com/codex/noninteractive)
- [Codex — hooks](https://developers.openai.com/codex/hooks)
- [Codex — configuração](https://developers.openai.com/codex/config-reference)
- [Codex — skills](https://developers.openai.com/codex/skills)
- [OpenSSL — pkeyutl e Ed25519](https://docs.openssl.org/3.5/man1/openssl-pkeyutl/)

A política, os hooks instalados e este controlador são implementação própria. Não são um workflow oficial da OpenAI.
