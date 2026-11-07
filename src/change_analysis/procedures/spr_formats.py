"""Чтение ответов СПР в любом разумном формате — часть пользовательской процедуры.

Ядро приложения про формат не знает: процедура приводит документ к словарю Python,
дальше работает универсальный обход. Здесь собраны читатели, которые покрывают
практически всё, что встречается в выгрузках:

===========================  ==================================================
Семейство                    Расширения
===========================  ==================================================
JSON                         ``.json``
JSON Lines / NDJSON          ``.jsonl``, ``.ndjson`` (один документ на строку)
XML                          ``.xml`` (см. ``spr_normalize_xml``)
YAML                         ``.yaml``, ``.yml``
TOML                         ``.toml``
INI / config                 ``.ini``, ``.cfg``, ``.conf``
Java properties              ``.properties``
Табличный (CSV)              ``.csv``, ``.tsv`` (заголовки через точку = вложенность)
Архивы                       ``.zip`` (первый файл внутри), ``.gz``, ``.bz2``, ``.xz``
===========================  ==================================================

Полную структуру (массивы, ``null``, различие «нет атрибута» и «пустое значение»)
сохраняют JSON, JSONL, XML, YAML и архивы.  TOML, INI, ``.properties`` и CSV
выражают только скаляры и вложенные объекты — там ``null`` становится пустой
строкой, а массивов нет; это ограничение самих форматов, а не читателя.

Безопасность: YAML читается через ``safe_load`` (никакого конструирования
объектов), распаковка ограничена по объёму, ``.zip`` берёт только первый файл.
"""

from __future__ import annotations

import bz2
import csv
import gzip
import io
import json
import lzma
import tomllib
import zipfile
from configparser import ConfigParser
from pathlib import Path

#: предел распакованного размера, чтобы архив не съел память
MAX_UNPACKED = 64 * 1024 * 1024

JSON_EXT = {"json"}
JSONL_EXT = {"jsonl", "ndjson"}
XML_EXT = {"xml"}
YAML_EXT = {"yaml", "yml"}
TOML_EXT = {"toml"}
INI_EXT = {"ini", "cfg", "conf"}
PROPERTIES_EXT = {"properties"}
TABLE_EXT = {"csv", "tsv"}
ARCHIVE_EXT = {"zip"}
COMPRESSED_EXT = {"gz", "bz2", "xz"}

#: всё, что масок файлов касается как «ответ одной заявки»
SUPPORTED_EXT = (JSON_EXT | JSONL_EXT | XML_EXT | YAML_EXT | TOML_EXT | INI_EXT
                 | PROPERTIES_EXT | TABLE_EXT | ARCHIVE_EXT)

#: готовая строка масок для датасета: ``*.json,*.jsonl,...``
DEFAULT_MASKS = ",".join(f"*.{ext}" for ext in sorted(SUPPORTED_EXT))


class UnsupportedFormat(ValueError):
    """Расширение не поддерживается — сообщение содержит список допустимых."""

    def __init__(self, suffix: str) -> None:
        shown = ", ".join("." + ext for ext in sorted(SUPPORTED_EXT | COMPRESSED_EXT))
        super().__init__(
            f"формат {suffix or '(без расширения)'} не поддерживается; "
            f"поддерживаются: {shown}. Для своего формата укажите ридер "
            f"read_document(path) в рабочей области.")


def _extension(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def supported(path: Path) -> bool:
    ext = _extension(path)
    return ext in SUPPORTED_EXT or ext in COMPRESSED_EXT


# ---------------------------------------------------------------------------
# отдельные форматы
# ---------------------------------------------------------------------------

def _literal(text: str):
    """Строка → значение: числа, булевы, null распознаются как в JSON."""
    stripped = text.strip()
    if stripped == "":
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return text


def read_json(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8-sig"))
    return _require_object(value, "JSON")


def read_jsonl(raw: bytes) -> dict:
    """JSON Lines: один документ на строку (пустые строки пропускаются).

    Если строк несколько — возвращается список документов; процедура
    нормализации берёт первый и предупреждает, чтобы это не осталось незамеченным.
    """
    lines = [line for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    if not lines:
        raise ValueError("JSONL: файл пуст")
    documents = [json.loads(line) for line in lines]
    if len(documents) == 1:
        return _require_object(documents[0], "JSONL")
    return {"_documents": documents}


def read_xml(raw: bytes) -> dict:
    from change_analysis.procedures.spr_normalize_xml import read_document as read_xml_file  # noqa: PLC0415

    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile(suffix=".xml", delete=True) as handle:
        handle.write(raw)
        handle.flush()
        return read_xml_file(Path(handle.name))


def read_yaml(raw: bytes) -> dict:
    try:
        import yaml  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - зависит от окружения
        raise ValueError("для YAML нужен пакет PyYAML: pip install pyyaml") from exc
    value = yaml.safe_load(raw.decode("utf-8-sig"))
    return _require_object(value, "YAML")


def read_toml(raw: bytes) -> dict:
    value = tomllib.loads(raw.decode("utf-8-sig"))
    return _require_object(value, "TOML")


def _nest(into: dict, dotted: str, value) -> None:
    """``a.b.c = value`` → ``{"a": {"b": {"c": value}}}``."""
    parts = [part for part in dotted.split(".") if part != ""]
    if not parts:
        return
    node = into
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def read_ini(raw: bytes) -> dict:
    parser = ConfigParser()
    parser.optionxform = str  # сохраняем регистр ключей
    parser.read_string(raw.decode("utf-8-sig"))
    out: dict = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            _nest(out, f"{section}.{key}", _literal(value))
    # ключи до первой секции ConfigParser держит в DEFAULT
    for key, value in parser.defaults().items():
        _nest(out, key, _literal(value))
    return _require_object(out, "INI")


def read_properties(raw: bytes) -> dict:
    out: dict = {}
    for line in raw.decode("utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#!":
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
        elif ":" in stripped:
            key, value = stripped.split(":", 1)
        else:
            key, value = stripped, "true"
        _nest(out, key.strip(), _literal(value))
    return _require_object(out, "properties")


def read_table(raw: bytes, delimiter: str | None = None) -> dict:
    """CSV/TSV: заголовок ``a.b.c`` задаёт вложенность, каждая строка — заявка.

    Одна строка → один документ; несколько строк → список документов.
    Пустые ячейки становятся пустой строкой (не null — CSV не различает).
    """
    text = raw.decode("utf-8-sig")
    if delimiter is None:
        first = text.split("\n", 1)[0]
        delimiter = "\t" if first.count("\t") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise ValueError("таблица пуста: нет ни одной строки данных")
    documents = []
    for row in rows:
        document: dict = {}
        for column, cell in row.items():
            if column is None:
                continue
            _nest(document, column, _literal(cell if cell is not None else ""))
        documents.append(document)
    if len(documents) == 1:
        return _require_object(documents[0], "CSV/TSV")
    return {"_documents": documents}


def _decompress(raw: bytes, ext: str) -> bytes:
    if ext == "gz":
        data = gzip.decompress(raw)
    elif ext == "bz2":
        data = bz2.decompress(raw)
    else:
        data = lzma.decompress(raw)
    if len(data) > MAX_UNPACKED:
        raise ValueError(f"после распаковки больше {MAX_UNPACKED // (1024 * 1024)} МБ")
    return data


def read_zip(raw: bytes) -> dict:
    """Берём первый файл архива, который сами умеем читать."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = [info for info in archive.infolist()
                   if not info.is_dir() and supported(Path(info.filename))]
        if not entries:
            raise ValueError("в архиве нет файлов поддерживаемого формата")
        entry = entries[0]
        if entry.file_size > MAX_UNPACKED:
            raise ValueError(f"файл внутри архива больше {MAX_UNPACKED // (1024 * 1024)} МБ")
        inner = archive.read(entry)
    return read_bytes(inner, Path(entry.filename))


def _require_object(value, family: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{family}: корень документа должен быть объектом, "
                         f"получено {type(value).__name__}")
    return value


# ---------------------------------------------------------------------------
# входная точка
# ---------------------------------------------------------------------------

def read_bytes(raw: bytes, path: Path) -> dict:
    """Разобрать содержимое по расширению (архивы и сжатие раскрываются)."""
    ext = _extension(path)
    if ext in COMPRESSED_EXT:
        inner_name = path.with_suffix("").name
        return read_bytes(_decompress(raw, ext), Path(inner_name))
    if ext in ARCHIVE_EXT:
        return read_zip(raw)
    if ext in JSON_EXT:
        return read_json(raw)
    if ext in JSONL_EXT:
        return read_jsonl(raw)
    if ext in XML_EXT:
        return read_xml(raw)
    if ext in YAML_EXT:
        return read_yaml(raw)
    if ext in TOML_EXT:
        return read_toml(raw)
    if ext in INI_EXT:
        return read_ini(raw)
    if ext in PROPERTIES_EXT:
        return read_properties(raw)
    if ext in TABLE_EXT:
        return read_table(raw, "\t" if ext == "tsv" else ",")
    raise UnsupportedFormat(path.suffix)


def read_any(path: Path) -> dict:
    """Прочитать ответ СПР в любом поддерживаемом формате."""
    raw = path.read_bytes()
    if len(raw) > MAX_UNPACKED:
        raise ValueError(f"файл больше {MAX_UNPACKED // (1024 * 1024)} МБ")
    return read_bytes(raw, path)
