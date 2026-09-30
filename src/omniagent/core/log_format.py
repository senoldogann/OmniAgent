"""Yapılandırılmış log biçimi: arayüz günlük dosyası ve arka plan servisleri (iMessage köprüsü) ortak kullanır.

Bu modül arayüze bağlı değildir; Tk ya da ui paketi içe aktarılmaz.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, TextIO, Tuple

LOG_LINE_FORMAT: str = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Bağımlılıkların INFO satırları servis loguna gürültü katar (httpx her HTTP isteğini yazar); yalnız uyarılar kalır.
QUIET_LIBRARY_LOGGERS: Tuple[str, ...] = ("httpx", "httpcore")
_LOG_RECORD_FIELDS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class StructuredFormatter(logging.Formatter):
    """`extra=` alanlarını mesaja gömmeden mesaj satırının sonuna ayrı bir JSON olarak ekler (traceback altta kalır)."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        base: str = super().formatMessage(record)
        fields: Dict[str, object] = {
            name: value for name, value in record.__dict__.items() if name not in _LOG_RECORD_FIELDS
        }
        # Çağıranın maskelediği izleme metni (`traceback` alanı) JSON'a gömülmez, satırın altına yazılır.
        trace: object = fields.pop("traceback", None)
        line: str = f"{base} {json.dumps(fields, ensure_ascii=False, default=str)}" if fields else base
        return f"{line}\n{trace}" if isinstance(trace, str) else line


def configure_stream_logging(stream: TextIO, level: int) -> logging.Handler:
    """
    Kök logger'a `stream`'e yazan yapılandırılmış işleyici ekler, seviyeyi ayarlar ve işleyiciyi döndürür.
    Uygulama kodu kök logger'ı kullandığından INFO satırları (ör. ilk balon gecikmesi) ancak kök seviye INFO ise
    yazılır; gürültülü kütüphane logları (httpx) uyarı seviyesinde tutulur.
    """
    handler: logging.StreamHandler[TextIO] = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter(LOG_LINE_FORMAT))
    root: logging.Logger = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)
    for name in QUIET_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler
