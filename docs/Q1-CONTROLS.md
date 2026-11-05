# Q1 — matriz dos oito controles

Versão: **0.2.0**. Esta matriz separa o que o controlador aplica por código do que
continua dependendo de evidência externa. A implementação não promete qualidade
idêntica entre modelos, detecção perfeita de risco ou uma fatura máxima garantida.

| Ponto auditado | Controle implementado | Evidência automatizada principal |
|---|---|---|
| 1. Classificação como ponto único de falha | Pisos monotônicos em `policy.py`, inspeção de texto, caminhos reais, conteúdo e diff; regras adicionais; herança a filhos e reparos; tarefas críticas sobem a Sol/Astra. | `test_contracts_policy.py` e `CriticalTests`: risco em escopo amplo, remoção de código sensível, herança, reclassificação após edição e bloqueio sem verificações críticas. |
| 2. Testes escritos pelo próprio executor | Contrato v2 por critério/check/baseline; testes de aceite previamente commitados fora dos escopos de escrita; manifesto de hashes; execução da baseline e revisão inicial pelo Astra. | Contrato rejeitado antes de implementar, baseline inesperada, tentativa de alterar testes/configuração e delegação que tenta substituir o oráculo são rejeitados. |
| 3. Integração somente no final | Checkpoints por lote, após tarefas críticas e ao encerrar um lote parcial; falhas bloqueiam dependentes; reparos com orçamento persistente. | Checkpoint falha antes do dependente; reparo conclui antes de liberar o dependente; orçamento não reinicia no `resume`. |
| 4. Paralelismo e conflitos | Worktree por tarefa e outra de integração; escalonamento por DAG e conflitos leitura/escrita; ordem estável de integração; nova base exige checks e revisão; conflito preserva trabalho. | Execução paralela real com processos, ordem de integração, revalidação de base, cancelamento do lote, conflito e recuperação de intenção de integração. |
| 5. Escalonamento sem contexto | Handoff factual limitado em bytes; tentativas, resultado, pendências e referências ao patch/logs; sem transcrições do executor. | Handoff limitado, falha preservada, troca correta do revisor e histórico mantido na retomada. |
| 6. Custo sem limite | Reservas atômicas por chamada; tetos de admissão por run, tarefa/subárvore e papel; preços explícitos opcionais; interrupção por consumo observado; consumo desconhecido bloqueia. | `test_budget.py`: concorrência, reservas, custos Decimal, limites de ancestral/modelo, cache/raciocínio não duplicados; integração cobre telemetria ausente, excesso e retomada. |
| 7. Pontos cegos correlacionados | Revisor sem narrativa do implementador; tarefas críticas exigem checks adversariais, nova revisão adversarial e aprovação humana externa Ed25519 vinculada ao snapshot/evidências. | Chaves e assinaturas OpenSSL reais: assinatura válida, identidade desconhecida, chave errada, declaração alterada, allowlist alterada, snapshot obsoleto e proibição de autoaprovação gerenciada. |
| 8. Releitura e reuso imprecisos | Cápsulas de revisão com spec, risco, oráculo, arquivos lidos/escritos, base/tree/commit, modelo, logs e resposta; cache por contexto exato; refresh de cápsulas obsoletas. | Reuso sem nova chamada para contexto idêntico; invalidação por fonte/spec/log/resposta alterados; refresh após reparos e gate final verificando provas. |

## Limites que não podem ser removidos apenas por instruções

**Faturamento.** O CLI informa consumo em fronteiras de resposta. O modo `observed`
controla admissão, reserva orçamento e cancela processos locais ao detectar excesso.
Uma chamada remota já enviada pode consumir mais que a reserva antes de comunicar
uso. O modo `strict` recusa inferência, em vez de inventar uma garantia de cobrança
que o adaptador não possui. As tarifas são fornecidas pelo operador, não embutidas.

**Independência.** Duas invocações de modelos da mesma família não se tornam
estatisticamente independentes. A aprovação externa obrigatória nas mudanças
críticas é um controle compensatório verificável, não uma alegação estatística.
A assinatura comprova a autorização da chave, não a qualidade da análise humana.

**Semântica.** Os testes são protegidos, mas precisam ser bons. O controlador
verifica a cobertura declarada e executa os comandos; a adequação técnica
do oráculo ainda é avaliada pelo revisor e pelo responsável do projeto. Regras de
risco também precisam ser complementadas com caminhos/domínios do projeto.

**Leitura de código.** O sistema evita chamadas e transporte de contexto redundantes
quando a evidência permanece válida. Não controla a atenção interna de um modelo
nem garante que ele nunca voltará a ler um arquivo.

**Isolamento.** Worktrees isolam alterações, não banco de dados, portas, rede ou
credenciais do sistema. Checks concorrentes devem usar recursos externos isolados.
Estado local e hashes não são uma fronteira de segurança contra o administrador
da própria máquina. Chaves privadas de aprovação ficam fora do ambiente dos workers.

## Perfil de adoção

O padrão é `parallel_workers = 1`, mantendo todos os gates ativos. O paralelismo é
opt-in e pode ser aumentado depois de medir conflitos, reprovações e consumo no
projeto consumidor. Não é necessário remover os três fluxos para iniciar de forma
conservadora. `minimum_tier` pode elevar o piso; regras críticas não podem ser
removidas por uma classificação do modelo.

A integração do consumidor permanece explícita: o runtime não publica nem faz
merge na branch do usuário. Depois de qualquer merge/rebase externo, valide o
snapshot integrado de acordo com o workflow do projeto.
