# Validação — v0.2.0 / Q1

## Resultado local executado

Ambiente: Linux, Python **3.13.5**, Git **2.47.3**, OpenSSL **3.5.5**.

```text
python -m unittest discover -s tests -v

Ran 126 tests in 409.603s
OK
```

A execução consolidada terminou sem falhas. Antes dela, os grupos também foram
executados separadamente durante o desenvolvimento. Os 126 testes incluem
contratos/política, ledger, instalação/hooks, evidências, scheduler e fluxos
ponta a ponta. Não se trata de 126 chamadas a modelos reais.

Os testes usam o controlador real, subprocessos reais, repositórios Git e
worktrees temporários, checks executáveis e persistência real. Apenas o processo
Codex é substituído por `tests/fake_codex.py`, explicitamente de teste.

A verificação humana utiliza pares Ed25519 temporários e **assinaturas reais com
OpenSSL**, não um mock criptográfico. Chaves privadas de fixture são criadas em
diretórios temporários e não fazem parte do repositório.

## Verificações adicionais executadas

- Instalação do pacote em diretório separado com `pip --no-build-isolation --no-deps`.
- Importação do pacote instalado fora do diretório de código-fonte.
- CLI instalado informando `0.2.0` e assets SKILL/WORKFLOW incluídos no pacote.
- Compilação dos módulos Python e validação local de JSON/TOML nos testes.
- Migração v1 → v2 com backup, idempotência e preservação de configurações existentes.

## Casos negativos cobertos

Classificação insuficiente, risco encontrado somente no diff, tentativa de reduzir
piso via delegação, alteração de testes protegidos, baseline inválida, teste mutante,
revisor mutante, revisão incompatível com critérios/snapshot, check reprovado,
falha em checkpoint, reparos excessivos, tentativas/chamadas/tokens/custo excedidos,
telemetria ausente ou inválida, concorrência no ledger, cancelamento de lote,
conflito de integração, mudança de base, alteração externa de worktree, evidência
ou aprovação obsoleta, assinatura errada, signer não autorizado e replay de
aprovação para código diferente.

A matriz ponto a ponto está em [Q1-CONTROLS.md](Q1-CONTROLS.md).

## Não validado ao vivo neste ambiente

Não havia Codex CLI autenticado disponível. Portanto, a execução real dos quatro
modelos, seus acessos na conta, a aderência deles aos contratos e os hooks dentro
de uma sessão autenticada não foram testados aqui. O README inclui smoke test
opt-in, sujeito ao consumo da conta.

A suíte não demonstra qualidade igual entre modelos, taxa real de reprovação,
latência de produção ou economia percentual. Orçamentos observados no CLI não
constituem um teto absoluto de faturamento remoto; `strict` recusa esse adaptador.

## CI

O workflow executa instalação, a suíte completa e compilação em Python **3.11,
3.12 e 3.13**, com OpenSSL real. O resultado remoto deve ser verificado na execução
Actions/PR correspondente ao commit; este relatório registra o resultado local,
sem presumir antecipadamente o resultado de CI.
