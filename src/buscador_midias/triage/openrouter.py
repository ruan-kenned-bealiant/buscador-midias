"""Classificador de manchetes pelo OpenRouter (API compatível com chat completions)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from buscador_midias.triage.prompt import SYSTEM_PROMPT, Policy, build_user_message, system_prompt
from buscador_midias.triage.relevance import (
    Decision,
    HeadlineItem,
    HeadlineVerdict,
    parse_verdicts,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_ATTEMPTS = 3
_TOKENS_PER_ITEM = 70
_BASE_TOKENS = 200
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class MissingOpenRouterKeyError(Exception):
    """A chave do OpenRouter não está no ambiente."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Consumo acumulado de um classificador.

    Attributes:
        calls: Chamadas concluídas com sucesso.
        prompt_tokens: Tokens de entrada.
        completion_tokens: Tokens de saída.
        cost_usd: Custo informado pelo OpenRouter (``None`` se nunca informou).
        retries: Tentativas repetidas por 429/5xx/rede.
        errors: Lotes que terminaram sem resposta (viraram ``UNKNOWN``).
        last_error: Última falha, sem corpo de resposta nem credenciais.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    retries: int = 0
    errors: int = 0
    last_error: str | None = None

    def minus(self, earlier: Usage) -> Usage:
        """Consumo ocorrido depois de ``earlier`` (diferença campo a campo).

        O custo só é conhecido se o provedor o informou depois; sem custo anterior, vale o atual.
        """
        cost = None
        if self.cost_usd is not None:
            cost = self.cost_usd - (earlier.cost_usd or 0.0)
        return Usage(
            calls=self.calls - earlier.calls,
            prompt_tokens=self.prompt_tokens - earlier.prompt_tokens,
            completion_tokens=self.completion_tokens - earlier.completion_tokens,
            cost_usd=cost,
            retries=self.retries - earlier.retries,
            errors=self.errors - earlier.errors,
            last_error=self.last_error,
        )


class OpenRouterChat:
    """Transporte do OpenRouter: uma chamada de chat com repetição, consumo e custo acumulados.

    Temperatura zero e resposta em JSON. Qualquer falha (rede, 4xx/5xx, resposta vazia) devolve
    ``None`` e é registrada em ``usage``: quem usa decide o que fazer, e nunca vaza a chave.
    Seguro para várias threads.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Cria o transporte.

        Args:
            client: Cliente httpx (injetável para testes com ``MockTransport``).
            api_key: Chave do OpenRouter. Nunca entra em mensagens de erro.
            model: Identificador do modelo no OpenRouter.
            base_url: Endereço da API.
            timeout: Tempo limite de cada chamada, em segundos.
            attempts: Tentativas por chamada (429, 5xx e falhas de rede são repetidas).
            sleep: Espera entre tentativas (injetável para testes).
        """
        self._client = client
        self._api_key = api_key
        self._model = model
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._timeout = timeout
        self._attempts = attempts
        self._sleep = sleep
        self._lock = threading.Lock()
        self._usage = Usage()

    @property
    def model(self) -> str:
        """Modelo em uso."""
        return self._model

    @property
    def usage(self) -> Usage:
        """Retrato do consumo acumulado até agora."""
        with self._lock:
            return self._usage

    def complete(self, system_prompt: str, user_message: str, *, max_tokens: int) -> str | None:
        """Faz uma chamada de chat e devolve o texto da resposta.

        Args:
            system_prompt: Prompt de sistema.
            user_message: Mensagem do usuário.
            max_tokens: Limite de tokens da resposta.

        Returns:
            O texto, ou ``None`` se o provedor falhou (o motivo fica em ``usage.last_error``).
        """
        body = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "usage": {"include": True},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        content, error = self._call(body)
        if content is None:
            self._record(error=error)
        return content

    def _call(self, body: dict[str, Any]) -> tuple[str | None, str | None]:
        """Faz a chamada com repetição; devolve (conteúdo, None) ou (None, motivo da falha)."""
        error = "sem tentativas"
        for attempt in range(1, self._attempts + 1):
            if attempt > 1:
                self._record(retry=True)
                self._sleep(2 ** (attempt - 1))
            try:
                response = self._client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=body,
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                error = type(exc).__name__
                continue
            if response.status_code in _RETRYABLE_STATUSES:
                error = f"HTTP {response.status_code}"
                continue
            if not response.is_success:
                return None, f"HTTP {response.status_code}"
            return self._read(response)
        return None, error

    def _read(self, response: httpx.Response) -> tuple[str | None, str | None]:
        """Extrai o texto da resposta e contabiliza o consumo."""
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return None, "resposta sem conteúdo"
        self._record(usage=(payload.get("usage") if isinstance(payload, dict) else None) or {})
        return (content, None) if isinstance(content, str) and content else (None, "conteúdo vazio")

    def _record(
        self,
        *,
        retry: bool = False,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Atualiza o consumo acumulado (thread-safe)."""
        with self._lock:
            current = self._usage
            cost = current.cost_usd
            reported = usage.get("cost") if usage else None
            if isinstance(reported, int | float):
                cost = (cost or 0.0) + float(reported)
            self._usage = Usage(
                calls=current.calls + (1 if usage is not None else 0),
                prompt_tokens=current.prompt_tokens + int((usage or {}).get("prompt_tokens", 0)),
                completion_tokens=current.completion_tokens
                + int((usage or {}).get("completion_tokens", 0)),
                cost_usd=cost,
                retries=current.retries + (1 if retry else 0),
                errors=current.errors + (1 if error is not None else 0),
                last_error=error if error is not None else current.last_error,
            )


class OpenRouterClassifier:
    """Pede a um modelo do OpenRouter o veredito de cada manchete de um lote.

    Qualquer falha do provedor devolve ``UNKNOWN`` para o lote: a esteira nunca descarta por
    falha do provedor.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
        system_prompt: str = SYSTEM_PROMPT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Cria o classificador.

        Args:
            client: Cliente httpx (injetável para testes com ``MockTransport``).
            api_key: Chave do OpenRouter. Nunca entra em mensagens de erro.
            model: Identificador do modelo no OpenRouter.
            base_url: Endereço da API.
            timeout: Tempo limite de cada chamada, em segundos.
            attempts: Tentativas por lote (429, 5xx e falhas de rede são repetidas).
            system_prompt: Prompt de sistema (veja ``prompt.system_prompt`` para as políticas).
            sleep: Espera entre tentativas (injetável para testes).
        """
        self._chat = OpenRouterChat(
            client,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            attempts=attempts,
            sleep=sleep,
        )
        self._system_prompt = system_prompt

    @property
    def model(self) -> str:
        """Modelo em uso."""
        return self._chat.model

    @property
    def usage(self) -> Usage:
        """Retrato do consumo acumulado até agora."""
        return self._chat.usage

    def classify(self, items: Sequence[HeadlineItem]) -> list[HeadlineVerdict]:
        """Classifica um lote de manchetes.

        Args:
            items: Manchetes do lote.

        Returns:
            Um veredito por item, na mesma ordem; ``UNKNOWN`` se o provedor falhar.
        """
        content = self._chat.complete(
            self._system_prompt,
            build_user_message(items),
            max_tokens=len(items) * _TOKENS_PER_ITEM + _BASE_TOKENS,
        )
        if content is None:
            return [HeadlineVerdict(i.id, Decision.UNKNOWN, (), "sem resposta") for i in items]
        return parse_verdicts(content, items)


def classifier_from_env(
    env: Mapping[str, str],
    client: httpx.Client,
    *,
    model: str = DEFAULT_MODEL,
    policy: Policy = Policy.ENTITIES,
) -> OpenRouterClassifier:
    """Monta o classificador com as variáveis do KyX (``OPENROUTER_API_KEY``/``_BASE_URL``).

    Args:
        env: Variáveis de ambiente.
        client: Cliente httpx.
        model: Modelo do OpenRouter.
        policy: Política de descarte do prompt.

    Raises:
        MissingOpenRouterKeyError: Se a chave não estiver definida (a mensagem nunca a traz).
    """
    api_key = env.get("OPENROUTER_API_KEY")
    if not api_key:
        raise MissingOpenRouterKeyError("defina OPENROUTER_API_KEY para usar a triagem por LLM")
    return OpenRouterClassifier(
        client,
        api_key=api_key,
        model=model,
        base_url=env.get("OPENROUTER_BASE_URL") or DEFAULT_BASE_URL,
        system_prompt=system_prompt(policy),
    )
