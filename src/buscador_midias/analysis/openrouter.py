"""Transporte do OpenRouter (API compatível com chat completions) usado pela análise de contexto."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_TIMEOUT_SECONDS = 90.0
DEFAULT_ATTEMPTS = 3
TRUNCATED_RESPONSE = "resposta_truncada"
#: O provedor recusou a notícia pelo filtro de conteúdo (crime sexual, violência gráfica): é uma
#: recusa deliberada, sem texto nem tokens, que se repete igual com o mesmo modelo.
CONTENT_FILTERED = "filtro_de_conteudo"
_EMPTY_RESPONSE = "resposta sem conteúdo"
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class MissingOpenRouterKeyError(Exception):
    """A chave do OpenRouter não está no ambiente."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Consumo acumulado de um transporte do OpenRouter.

    Attributes:
        calls: Chamadas concluídas com sucesso.
        prompt_tokens: Tokens de entrada.
        completion_tokens: Tokens de saída.
        cost_usd: Custo informado pelo OpenRouter (``None`` se nunca informou).
        retries: Tentativas repetidas por 429/5xx/rede.
        errors: Chamadas que terminaram sem resposta.
        fallbacks: Vezes em que o modelo alternativo substituiu o principal (filtro de conteúdo).
        last_error: Última falha, sem corpo de resposta nem credenciais.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    retries: int = 0
    errors: int = 0
    fallbacks: int = 0
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
            fallbacks=self.fallbacks - earlier.fallbacks,
            last_error=self.last_error,
        )


class OpenRouterChat:
    """Transporte do OpenRouter: uma chamada de chat com repetição, consumo e custo acumulados.

    Temperatura zero e resposta em JSON. Qualquer falha (rede, 4xx/5xx, resposta vazia) devolve
    ``None`` e é registrada em ``usage``: quem usa decide o que fazer, e nunca vaza a chave.
    Quando o filtro de conteúdo do provedor recusa a notícia, repete uma vez com o modelo
    alternativo (se houver), que não tem a mesma recusa. Seguro para várias threads.
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
        fallback_model: str | None = None,
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
            fallback_model: Modelo que repete a chamada recusada pelo filtro de conteúdo; sem ele,
                a recusa vira falha.
        """
        self._client = client
        self._api_key = api_key
        self._model = model
        self._fallback_model = fallback_model if fallback_model != model else None
        self._answered = model
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
    def answered_model(self) -> str:
        """Modelo que deu a última resposta aproveitada (o alternativo, se o filtro entrou)."""
        with self._lock:
            return self._answered

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
        model = self._model
        if error == CONTENT_FILTERED and self._fallback_model:
            self._record(fallback=True)
            model = self._fallback_model
            content, error = self._call(dict(body, model=model))
        if content is None:
            self._record(error=error)
        else:
            with self._lock:
                self._answered = model
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
            content, reason = self._read(response)
            if reason == _EMPTY_RESPONSE:
                # HTTP 200 sem ``choices`` costuma ser falha momentânea do provedor de origem.
                error = reason
                continue
            return content, reason
        return None, error

    def _read(self, response: httpx.Response) -> tuple[str | None, str | None]:
        """Extrai o texto da resposta e contabiliza o consumo."""
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return None, _EMPTY_RESPONSE
        self._record(usage=(payload.get("usage") if isinstance(payload, dict) else None) or {})
        finish = payload["choices"][0].get("finish_reason")
        if finish == "content_filter":
            return None, CONTENT_FILTERED
        if finish in ("length", "max_tokens"):
            return None, TRUNCATED_RESPONSE
        return (content, None) if isinstance(content, str) and content else (None, "conteúdo vazio")

    def _record(
        self,
        *,
        retry: bool = False,
        fallback: bool = False,
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
                fallbacks=current.fallbacks + (1 if fallback else 0),
                last_error=error if error is not None else current.last_error,
            )
