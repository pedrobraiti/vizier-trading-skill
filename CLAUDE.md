# Instruções para o Claude neste projeto

> **VIZIER** — o cérebro do trio agentic-trading. Skill `/vizier` que orquestra **Scout** (sentidos/dados)
> e **Valet** (mãos/execução). Ler `.claude/context.md` e `docs/SPEC-origin.md` para o panorama completo.

## Fronteira inviolável (regra nº1 deste projeto)

A skill **consome** Scout e Valet exatamente como eles são. **NUNCA** adiciona, modifica ou "pluga" nada
dentro de Scout ou Valet — os dois são MCPs independentes. Toda inteligência, cola, estado e decisão mora
NA SKILL. Nenhum MCP chama o outro; nenhum MCP conclui. Se a skill precisar de algo que nenhum MCP entrega,
resolve na skill (helper Python ou prompt), jamais embutindo lógica nos MCPs.

## Código público / estado privado

O repo desta skill será **público** (terceiro do trio). Mas a **memória de trading real**
(`memory/theses/*.yaml`, `memory/decision_log.jsonl`, snapshots de NAV) é **gitignored** — posições e
decisões reais NUNCA vão pro GitHub. Só templates/exemplos são commitados. Espelha o `.env` gitignored dos MCPs.

## Memória persistente

Ao iniciar **qualquer** conversa neste projeto, antes de agir:
1. Leia `.claude/handoff.md` **PRIMEIRO** — é o ponteiro mais fresco: responde "de onde parei" com detalhe.
2. Leia `.claude/context.md` para o estado macro/estável do projeto.
3. Leia `.claude/todo.md` para saber o que está em progresso e o que vem a seguir.
4. Rode `git log --oneline -20` para ver atividade recente.
5. Se a tarefa tocar em área sensível/arquitetural, leia `.claude/decisions.md`.

### Manter o handoff vivo

O `.claude/handoff.md` é o que permite a **próxima sessão começar de onde esta parou**. Trate-o como documento vivo:
- Ao concluir qualquer passo significativo (não só no fim da sessão), atualize-o.
- Escreva com detalhe suficiente para um chat novo retomar sem reconstruir seu raciocínio: onde parou, o contexto mental, o próximo passo concreto e o que está em aberto.
- Atualize a data e **sobrescreva** o conteúdo antigo — ele reflete sempre o ESTADO ATUAL de "onde paramos", não é histórico append-only (esse papel é do git e do `decisions.md`).

## Disciplina do TODO

- O `.claude/todo.md` é **mandatório** e deve sempre refletir a realidade do projeto.
- Ao sair do planning mode (ou após planejar qualquer coisa com o usuário), atualize o TODO com tarefas e subtarefas granulares.
- Marque `[x]` a subtarefa **no mesmo commit** em que ela é concluída.
- Subtarefas devem ser pequenas e modulares — se uma não cabe em um commit, quebra em menores.

## Disciplina de commits

- Sempre que uma subtarefa do TODO for **concluída** (não trabalho intermediário), faça um commit.
- Use **Conventional Commits**: `feat:`, `fix:`, `refactor:`, `docs:`, `chore:`, `test:`, `style:`.
- Mensagens claras, no imperativo, descrevendo o **porquê** quando não óbvio.
- **Nunca** inclua `Co-Authored-By: Claude` nas mensagens de commit.
- Antes de cada commit, avalie e atualize **no mesmo commit** se necessário:
  - `.claude/handoff.md` (de onde parei — detalhado, refletindo o estado atual).
  - `.claude/todo.md` (marcar subtarefa concluída).
  - `.claude/context.md` (estado atual mudou?).
  - `.claude/decisions.md` (houve decisão arquitetural nova?).
  - `README.md` (mudou stack, dependências, forma de rodar?).
  - `.env.example` (adicionou/removeu variável em `.env`? espelha aqui sem valores).

## Arquitetura

Seguir os padrões definidos em `~/.claude/rules/BEST_PRACTICES.md` (código profissional, modular, testável;
arquitetura escolhida conforme o projeto). Lógica sensível a dinheiro (§B: teto cumulativo, drawdown kill,
sizing, reconciliação) fica em **helpers Python determinísticos com testes**, não no julgamento livre do LLM.

## Autonomia

Neste projeto você tem autonomia ampliada — use com critério profissional (autonomia controlada, não automática).

### Subagentes
- Delegue buscas amplas e trabalho paralelo a subagentes para manter o contexto principal limpo: `Explore` para varreduras em muitos arquivos, `Plan` para desenhar implementação, `general-purpose` para tarefas multi-etapa.
- Use quando a tarefa exigir varrer bastante código, pesquisar em paralelo ou planejar algo não-trivial. Para tarefas simples, resolva direto.

### Agent crews
- Pode ativar múltiplos agentes em paralelo (agent crews) quando a **complexidade do projeto justificar**: várias frentes independentes, refactor grande, investigação ampla.
- Não use crews para trabalho simples — é custo e ruído desnecessários.

### Skills (find-skills)
- Quando perceber que uma capacidade especializada ajudaria (testing, design, deploy, um framework específico), **pesquise proativamente**: `npx skills find <query>` ou a skill `find-skills`.
- Você pode baixar e instalar skills úteis **sem pedir confirmação**, respeitando estas regras:
  - **Sempre LOCAL ao projeto, NUNCA global.** Instale com o projeto como diretório atual e **sem** o flag `-g`:
    ```bash
    npx skills add <owner/repo> --skill <nome> --copy -y
    ```
    Isso instala em `.claude/skills/` (versionado no git). O `--copy` evita symlink apontando pra fora do repo.
  - **Nunca** use `-g` / `--global`. (A documentação da própria `find-skills` sugere `-g` por padrão — **ignore isso aqui**: neste projeto é sempre local.)
  - Prefira fontes confiáveis (`anthropics`, `vercel-labs`, `microsoft`) e skills com volume de instalações relevante; desconfie de fontes obscuras.
  - Informe no resumo qual skill instalou e por quê — a skill entra no commit junto.
