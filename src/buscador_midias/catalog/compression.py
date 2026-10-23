"""Descompressão limitada compartilhada entre transporte HTTP e mecanismos XML."""

import zlib

MAX_BYTES = 8 * 1024 * 1024


def unpack_gzip(body: bytes, max_bytes: int = MAX_BYTES) -> bytes:
    """Limita a saída e rejeita arquivos truncados."""
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        content = decoder.decompress(body, max_bytes + 1)
    except zlib.error as error:
        raise ValueError("Arquivo gzip inválido") from error
    if len(content) > max_bytes or decoder.unconsumed_tail:
        raise ValueError(f"XML descomprimido acima de {max_bytes // (1024 * 1024)} MiB")
    if not decoder.eof:
        raise ValueError("Arquivo gzip truncado")
    return content
