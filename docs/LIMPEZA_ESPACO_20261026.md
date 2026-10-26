# Limpeza e compactação de evidências — 26/10/2026

O espaço livre passou de aproximadamente 420 MiB para **12.00 GiB**.

- Cache de downloads npm limpo; dependências instaladas preservadas.
- Cache de build Docker sem uso há sete dias e imagens antigas sem tag foram limpos. Nenhum contêiner existente ou volume foi excluído. O utilitário oficial de recuperação de espaço do Docker Desktop também foi executado.
- **36,948 evidências HTML com mais de 24 horas** foram compactadas com gzip sem perda de conteúdo, economizando **10.32 GiB** somente nas cópias HTML.
- Cada cópia compactada foi relida e conferida por SHA-256 antes de retirar a representação bruta. O hash original, caminho e mtime estão em `var/maintenance/space-20261026/compressed.jsonl`.
- Chaves `.html` e registros do corpus permanecem iguais. Os leitores locais e o visualizador abrem automaticamente a cópia `.html.gz` e devolvem os bytes originais, com limite de 16 MiB após descompressão e confinamento na raiz permitida.
- Evidências recentes não foram compactadas. Nenhuma notícia, registro de banco, histórico de rodada ou evidência foi descartado.
- A ingestão estava ociosa e foi pausada durante a compactação. Ingestão e painel foram restabelecidos, mantendo o agendamento automático desligado. Nenhuma rodada foi criada pela manutenção.

## Verificação

34 testes de armazenamento, captura e leitura de evidências passaram. Testes cobriram a integridade dos bytes, substituição por captura mais recente, bloqueio de travessia por symlink e tamanho máximo descomprimido. Foram reabertas amostras locais e três evidências reais do corpus pelas rotas do painel, com conferência dos hashes originais; os resultados estão em `visualizacao-verificada.json` junto ao log da manutenção. Ruff, mypy dos módulos alterados e `git diff --check` passaram.

## Manutenção futura

`scripts/compact_old_evidence.py` lista os candidatos por padrão. `--apply --target-free-gib 12` compacta arquivos antigos até atingir a folga desejada. Execute quando não houver gravação concorrente de evidências; nesta manutenção o serviço foi pausado para isso. Os arquivos gzip podem ser descompactados individualmente com ferramentas padrão se necessário, preservando o arquivo compactado. A geração de evidências novas continua usando o formato HTML normal.

Referência do procedimento Docker: [FAQ oficial para Linux](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/linuxfaqs/).
