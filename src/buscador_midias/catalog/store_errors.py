"""Erros seguros de domínio compartilhados entre cadastro e testes de leitura."""


class CatalogError(Exception):
    """Erro de domínio com código HTTP e mensagem segura para a interface."""

    def __init__(self, message: str, status: int = 422) -> None:
        """Define mensagem e status."""
        super().__init__(message)
        self.status = status
