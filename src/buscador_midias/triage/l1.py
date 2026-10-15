"""Camada L1 da triagem: pontua a manchete e descarta o que tem pouco sinal de notícia adversa.

Um Naive Bayes sobre palavras e pares de palavras, treinado com manchetes do corpus de Mídias IA
(exemplos positivos) contra manchetes comuns de sitemaps (exemplos negativos). O limiar é calibrado
para manter uma fração alta dos positivos de teste (recall), pois descartar uma matéria relevante
custa mais do que triar uma a mais. Na falta de evidência (idioma não coberto, manchete curta,
poucas palavras conhecidas) a URL passa.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from buscador_midias.triage.headline import features, headline_text, tokens
from buscador_midias.triage.reasons import FilterReason

DEFAULT_LANGUAGES: frozenset[str] = frozenset({"Português"})
_TEST_FRACTION = 0.2


@dataclass(frozen=True, slots=True)
class HeadlineScore:
    """Pontuação de uma manchete.

    Attributes:
        log_odds: Log da razão de verossimilhança adverso/comum (> 0 parece adverso); ``0.0`` sem
            nenhum atributo conhecido. As prioris são equilibradas, então não depende do tamanho
            das classes no treino.
        known: Quantos atributos distintos da manchete existem no vocabulário do modelo.
    """

    log_odds: float
    known: int


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Desempenho medido em dados separados do treino.

    Attributes:
        threshold: Limiar escolhido (descarta quando ``log_odds`` fica abaixo).
        recall_target: Fração de positivos que o limiar deveria manter.
        positive_recall: Fração de positivos de teste mantidos (abstenções contam como mantidas).
        negative_pass_rate: Fração de negativos de teste que ainda passa (menor é melhor, mas os
            negativos reais incluem notícias adversas, então o piso não é zero).
        test_positives: Positivos no conjunto de teste.
        test_negatives: Negativos no conjunto de teste.
    """

    threshold: float
    recall_target: float
    positive_recall: float
    negative_pass_rate: float
    test_positives: int
    test_negatives: int


class NaiveBayesHeadlineModel:
    """Naive Bayes de presença de atributos (Bernoulli por documento) com suavização de Laplace."""

    def __init__(
        self,
        positive: Mapping[str, int],
        negative: Mapping[str, int],
        *,
        smoothing: float = 1.0,
    ) -> None:
        """Monta o modelo a partir das contagens de documentos por atributo.

        Args:
            positive: Em quantos documentos positivos cada atributo aparece.
            negative: Em quantos documentos negativos cada atributo aparece.
            smoothing: Suavização de Laplace.
        """
        self._positive = dict(positive)
        self._negative = dict(negative)
        self._smoothing = smoothing
        vocabulary = set(self._positive) | set(self._negative)
        size = len(vocabulary)
        self._log_positive = math.log(sum(self._positive.values()) + smoothing * size)
        self._log_negative = math.log(sum(self._negative.values()) + smoothing * size)
        self._vocabulary = frozenset(vocabulary)

    @classmethod
    def train(
        cls,
        positives: Iterable[str],
        negatives: Iterable[str],
        *,
        min_count: int = 3,
        smoothing: float = 1.0,
    ) -> Self:
        """Treina com manchetes positivas e negativas.

        Args:
            positives: Manchetes de notícias adversas.
            negatives: Manchetes comuns.
            min_count: Atributos vistos em menos documentos que isso (somando as duas classes)
                ficam fora do vocabulário.
            smoothing: Suavização de Laplace.

        Returns:
            O modelo treinado.
        """
        positive_counts: Counter[str] = Counter()
        negative_counts: Counter[str] = Counter()
        for text in positives:
            positive_counts.update(set(features(text)))
        for text in negatives:
            negative_counts.update(set(features(text)))
        total = positive_counts + negative_counts
        keep = {name for name, count in total.items() if count >= min_count}
        return cls(
            {name: positive_counts[name] for name in keep},
            {name: negative_counts[name] for name in keep},
            smoothing=smoothing,
        )

    def score(self, text: str) -> HeadlineScore:
        """Pontua uma manchete."""
        a = self._smoothing
        log_odds = 0.0
        known = 0
        for name in set(features(text)):
            if name not in self._vocabulary:
                continue
            known += 1
            log_odds += (
                math.log(self._positive.get(name, 0) + a)
                - self._log_positive
                - math.log(self._negative.get(name, 0) + a)
                + self._log_negative
            )
        return HeadlineScore(log_odds=log_odds, known=known)

    def to_dict(self) -> dict[str, Any]:
        """Representação serializável em JSON."""
        return {
            "smoothing": self._smoothing,
            "positive": self._positive,
            "negative": self._negative,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Reconstrói o modelo gravado por ``to_dict``."""
        return cls(data["positive"], data["negative"], smoothing=float(data["smoothing"]))


def threshold_for_recall(positive_scores: Sequence[float], recall: float) -> float:
    """Maior limiar que ainda mantém ao menos ``recall`` dos positivos (mantém ``>= limiar``).

    Args:
        positive_scores: ``log_odds`` dos positivos de teste.
        recall: Fração mínima a manter (ex.: 0.98).

    Returns:
        O limiar; ``-inf`` (não descarta nada) se não há positivos.
    """
    if not positive_scores:
        return float("-inf")
    ordered = sorted(positive_scores)
    index = int((1 - recall) * len(ordered) + 1e-9)
    return ordered[min(index, len(ordered) - 1)]


@dataclass(frozen=True, slots=True)
class L1Filter:
    """Decide, pela manchete, se uma URL tem sinal suficiente para seguir adiante.

    Attributes:
        model: Modelo treinado.
        threshold: Descarta quando ``log_odds`` fica abaixo.
        min_words: Manchetes com menos palavras passam (pouca evidência).
        min_known: Manchetes com menos atributos conhecidos passam.
        languages: Idiomas do catálogo (``Site.language``) que o modelo cobre; os demais passam.
    """

    model: NaiveBayesHeadlineModel
    threshold: float
    min_words: int = 4
    min_known: int = 2
    languages: frozenset[str] = DEFAULT_LANGUAGES

    def check(self, *, language: str, url: str, title: str | None) -> FilterReason | None:
        """Avalia uma URL.

        Args:
            language: Idioma da fonte no catálogo.
            url: URL da matéria.
            title: Título do news sitemap, se houver (senão usa o slug da URL).

        Returns:
            ``LOW_SIGNAL`` se deve ser descartada, ou ``None`` se segue adiante.
        """
        if language not in self.languages:
            return None
        return None if self.keeps(headline_text(url, title)) else FilterReason.LOW_SIGNAL

    def keeps(self, text: str) -> bool:
        """Se a manchete segue adiante (também quando falta evidência para descartar)."""
        if len(tokens(text)) < self.min_words:
            return True
        score = self.model.score(text)
        return score.known < self.min_known or score.log_odds >= self.threshold

    def save(self, path: Path) -> None:
        """Grava o filtro (modelo e parâmetros) em JSON."""
        payload = {
            "model": self.model.to_dict(),
            "threshold": self.threshold,
            "min_words": self.min_words,
            "min_known": self.min_known,
            "languages": sorted(self.languages),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Self:
        """Lê um filtro gravado por ``save``."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            model=NaiveBayesHeadlineModel.from_dict(data["model"]),
            threshold=float(data["threshold"]),
            min_words=int(data["min_words"]),
            min_known=int(data["min_known"]),
            languages=frozenset(data["languages"]),
        )


def train_l1(
    positives: Sequence[str],
    negatives: Sequence[str],
    *,
    recall: float = 0.98,
    seed: int = 0,
    min_count: int = 3,
    min_words: int = 4,
    min_known: int = 2,
    languages: frozenset[str] = DEFAULT_LANGUAGES,
) -> tuple[L1Filter, Evaluation]:
    """Treina o modelo, calibra o limiar e mede o desempenho em dados separados.

    Separa 20% de cada classe para teste (embaralhamento determinístico pela ``seed``), treina com
    o resto, escolhe o limiar que mantém ``recall`` dos positivos de teste que o filtro chegaria a
    avaliar e mede quantos negativos de teste ainda passam.

    Args:
        positives: Manchetes de notícias adversas.
        negatives: Manchetes comuns.
        recall: Fração de positivos que o limiar deve manter.
        seed: Semente da separação treino/teste.
        min_count: Frequência mínima de um atributo no vocabulário.
        min_words: Palavras mínimas para uma manchete ser avaliada.
        min_known: Atributos conhecidos mínimos para uma manchete ser avaliada.
        languages: Idiomas cobertos pelo filtro.

    Returns:
        O filtro treinado e a avaliação.

    Raises:
        ValueError: Se uma das classes não tem exemplos.
    """
    if not positives:
        raise ValueError("sem exemplos positivos para treinar")
    if not negatives:
        raise ValueError("sem exemplos negativos para treinar")
    rng = random.Random(seed)
    train_pos, test_pos = _split(positives, rng)
    train_neg, test_neg = _split(negatives, rng)

    model = NaiveBayesHeadlineModel.train(train_pos, train_neg, min_count=min_count)
    evaluable = [
        model.score(text).log_odds
        for text in test_pos
        if len(tokens(text)) >= min_words and model.score(text).known >= min_known
    ]
    threshold = threshold_for_recall(evaluable, recall)
    chosen = L1Filter(
        model, threshold=threshold, min_words=min_words, min_known=min_known, languages=languages
    )
    evaluation = Evaluation(
        threshold=threshold,
        recall_target=recall,
        positive_recall=_share(chosen.keeps(text) for text in test_pos),
        negative_pass_rate=_share(chosen.keeps(text) for text in test_neg),
        test_positives=len(test_pos),
        test_negatives=len(test_neg),
    )
    return chosen, evaluation


def _split(items: Sequence[str], rng: random.Random) -> tuple[list[str], list[str]]:
    """Embaralha e separa (treino, teste), com ao menos um item de teste se houver dois ou mais."""
    shuffled = list(items)
    rng.shuffle(shuffled)
    test_size = max(int(len(shuffled) * _TEST_FRACTION), 1 if len(shuffled) > 1 else 0)
    return shuffled[test_size:], shuffled[:test_size]


def _share(flags: Iterable[bool]) -> float:
    """Fração de verdadeiros (1.0 se não há itens)."""
    values = list(flags)
    return sum(values) / len(values) if values else 1.0
