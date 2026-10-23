"""Contrato dos mecanismos e parâmetros validados de configuração."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

from buscador_midias.catalog.mechanisms.parsing import Document, parse_document
from buscador_midias.catalog.store_errors import CatalogError


@dataclass(frozen=True)
class Parameter:
    """Campo configurável e seus limites de domínio."""

    key: str
    label: str
    default: int | str
    description: str
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[tuple[str, str], ...] = ()

    def validate(self, value: object) -> int | str:
        """Valida tipos estritos e intervalos; booleanos não são aceitos como números."""
        if self.choices:
            if not isinstance(value, str) or value not in dict(self.choices):
                raise CatalogError(f"{self.label}: selecione uma opção válida.")
            return value
        if (
            type(value) is not int
            or self.minimum is None
            or self.maximum is None
            or not self.minimum <= value <= self.maximum
        ):
            raise CatalogError(
                f"{self.label}: informe um inteiro entre {self.minimum} e {self.maximum}."
            )
        return value


COMMON_PARAMETERS = (
    Parameter(
        "lookback_days",
        "Janela de notícias (dias)",
        0,
        "0 aceita qualquer data; 365 busca até um ano atrás. Datas desconhecidas são sinalizadas.",
        0,
        3650,
    ),
    Parameter(
        "timeout_seconds",
        "Duração máxima do teste (segundos)",
        45,
        "Orçamento total, incluindo robots, XMLs e notícias.",
        5,
        60,
    ),
    Parameter(
        "max_requests",
        "Máximo de requisições",
        24,
        "Conta também robots e cada redirecionamento.",
        4,
        48,
    ),
    Parameter(
        "article_samples",
        "Notícias para testar",
        3,
        "Use 0 para verificar apenas o XML; as amostras respeitam a janela configurada.",
        0,
        3,
    ),
    Parameter(
        "sample_order",
        "Seleção das amostras",
        "distributed",
        "A ordem vem do XML e não garante ordem de publicação.",
        choices=(
            ("distributed", "Distribuídas: início, meio e fim"),
            ("first", "Primeiras URLs"),
            ("last", "Últimas URLs"),
        ),
    ),
    Parameter(
        "max_response_mib",
        "Tamanho máximo do XML/resposta (MiB)",
        8,
        "Limite aplicado ao download e ao XML descomprimido.",
        1,
        16,
    ),
)
TREE_PARAMETERS = (
    Parameter(
        "max_documents",
        "Máximo de documentos XML",
        12,
        "Inclui o XML inicial e os filhos lidos.",
        1,
        24,
    ),
    Parameter(
        "max_depth",
        "Profundidade máxima",
        10,
        "O XML inicial está no nível 0. URLs repetidas não são lidas novamente.",
        0,
        10,
    ),
)


class Mechanism(ABC):
    """Comportamento de um modelo; rede, execução e banco são compartilhados."""

    id: str
    label: str
    description: str
    kind: str | None
    expand: bool = False
    parameters: tuple[Parameter, ...] = COMMON_PARAMETERS

    @abstractmethod
    def accepts(self, document: Document) -> bool:
        """Decide se o XML interpretado corresponde ao mecanismo."""
        ...

    def read(self, body: bytes, url: str, settings: dict[str, Any]) -> Document:
        """Interpreta XML com limite configurado de tamanho."""
        return parse_document(body, url, settings["max_response_mib"] * 1024 * 1024)

    def children(self, document: Document) -> list[str]:
        """Modelos diretos não expandem referências a filhos."""
        return []

    def requested_kind(self, declared: str, document: Document) -> str:
        """Mantém o tipo declarado; automático pode ajustar o tipo detectado."""
        return declared

    def defaults(self) -> dict[str, Any]:
        """Obtém uma cópia dos valores padrão do modelo."""
        return {p.key: p.default for p in self.parameters}

    def validate(self, values: object) -> dict[str, Any]:
        """Completa padrões e recusa campos desconhecidos ou valores inválidos."""
        if not isinstance(values, dict) or values.keys() - self.defaults().keys():
            raise CatalogError("Parâmetros desconhecidos para este mecanismo.")
        return {p.key: p.validate(values.get(p.key, p.default)) for p in self.parameters}

    def descriptor(self) -> dict[str, Any]:
        """Metadados e formulário declarativo; não expõe execução arbitrária."""
        return dict(
            id=self.id,
            label=self.label,
            description=self.description,
            kind=self.kind,
            expand=self.expand,
            defaults=self.defaults(),
            parameters_schema=[asdict(p) for p in self.parameters],
        )
