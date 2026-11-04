# Validação da versão 0.1.0

Data: 04/11/2026. Ambiente local: Linux, Python 3.13.5 e Git.

## Executado

`python -m unittest discover -s tests -v`

```text
Ran 50 tests in 136.822s
OK
```

A suíte testa o controlador real, processos reais, repositórios Git temporários,
worktrees, checks, persistência e hooks. Somente o processo Codex é substituído
por `tests/fake_codex.py`; nenhuma inferência é feita durante esses testes.

Cenários cobertos incluem os três pares executor/revisor, delegação aninhada,
correção após testes ou revisão, escalonamento, reparos globais, limites de
chamadas/tarefas/tentativas, timeouts, retomada, aprovação obsoleta, alterações
fora de escopo, revisor mutante, hooks de commit mutantes e instalação
conservadora/idempotente. As worktrees ficam fora do checkout original e de
metadados `.git`.

Também foi verificada a instalação do pacote com setuptools em um destino
separado, a descoberta dos assets distribuídos e a resposta `0.1.0` do CLI.

## Não executado neste ambiente

Não havia Codex CLI autenticado disponível. Portanto, não foram validados ao vivo:
a autenticação da conta, a disponibilidade dos quatro modelos, a execução dos
hooks dentro de uma sessão real do Codex nem a qualidade de implementações reais.
O smoke test opt-in está documentado no README. Os testes simulados não comprovam
economia de tokens, equivalência de qualidade ou latência entre modelos.

## CI

O workflow GitHub Actions executa os testes em Python 3.11, 3.12 e 3.13.
O resultado de cada execução deve ser consultado na aba Actions ou no PR; este
registro local não presume que uma execução remota tenha passado.
