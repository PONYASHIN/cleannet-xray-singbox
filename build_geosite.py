#!/usr/bin/env python3
import os
import sys
import subprocess
import shutil
import ssl
import urllib.request
import re
import ipaddress
import threading
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= НАСТРОЙКИ =================
# Скрипт предполагает, что main.go, go.mod, go.sum и папка data/
# лежат рядом с ним (то есть в текущей рабочей директории).
DLC_DATA_DIR = Path("data")           # сюда пишем файлы без расширения
OUTPUT_GEOSITE = Path("geosite.dat")
SRS_OUTPUT_DIR = Path("srs")
LINKS_FILE = Path("links.txt")
MAX_WORKERS = 8

GEODAT2SRS_BIN = "geodat2srs"
GEODAT2SRS_PATH = None
SRS_PREFIX = "geosite-"

PLAIN_AS_FULL = False
WILDCARD_AS_DOMAIN = True
WILDCARD_ALLOW_EMPTY = False
# =============================================

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def download_text(url: str) -> str:
    log(f"  Скачивание: {url}")
    try:
        context = ssl.create_default_context()
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            pass
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30, context=context) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log(f"  [Ошибка] {e}")
        return ""

# ---------- Разбор links.txt ----------

def parse_links_file(path: Path) -> list:
    if not path.exists():
        log(f"Файл ссылок не найден: {path}")
        sys.exit(1)

    sources = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split("#")
            if len(parts) < 2:
                log(f"  [Предупреждение] Строка {lineno}: нет '#', пропуск: {line}")
                continue

            url = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else ""
            target = parts[2].strip() if len(parts) > 2 else None

            if not url or not name:
                log(f"  [Предупреждение] Строка {lineno}: пустой URL или имя")
                continue

            is_white = False
            while True:
                if name.endswith("!white"):
                    name = name[: -len("!white")].strip()
                    is_white = True
                elif name.endswith("!w"):
                    name = name[: -len("!w")].strip()
                    is_white = True
                else:
                    break

            name = re.sub(r'[^\w\-.]', '_', name)
            if target:
                target = re.sub(r'[^\w\-.]', '_', target)

            if not name:
                log(f"  [Предупреждение] Строка {lineno}: некорректное имя")
                continue

            sources.append((url, name, target, is_white))

    if not sources:
        log("В links.txt не найдено ни одной корректной ссылки.")
        sys.exit(1)

    log(f"Загружено источников из {path}: {len(sources)}")
    return sources

# ---------- Определение типа контента ----------

def detect_format(text: str) -> str:
    hosts_score = 0
    dnscrypt_score = 0
    for line in text.splitlines()[:500]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("## "):
            dnscrypt_score += 1
            continue
        if s.startswith("#"):
            continue
        first = s.split()[0] if s.split() else ""
        if first in ("0.0.0.0", "127.0.0.1", "::1", "::", "255.255.255.255"):
            hosts_score += 1
        else:
            try:
                ipaddress.ip_address(first)
                hosts_score += 1
            except ValueError:
                pass
        if "sdns://" in s:
            dnscrypt_score += 1

    if hosts_score > 5 and hosts_score >= dnscrypt_score:
        return "hosts"
    if dnscrypt_score > 5:
        return "dnscrypt"
    return "plain"

# ---------- Работа с доменами ----------

LABEL_RE = re.compile(r'^[a-z0-9*]([a-z0-9\-*]*[a-z0-9*])?$', re.IGNORECASE)

def _normalize_domain(dom: str) -> str:
    dom = dom.strip().strip(".").lower()
    while ".." in dom:
        dom = dom.replace("..", ".")
    return dom

def _is_valid_domain(dom: str) -> bool:
    if not dom or "." not in dom:
        return False
    try:
        ipaddress.ip_address(dom)
        return False
    except ValueError:
        pass
    labels = dom.split(".")
    if len(labels) < 2:
        return False
    for lbl in labels:
        if not lbl:
            return False
        if lbl.startswith("-") or lbl.endswith("-"):
            return False
        if not LABEL_RE.match(lbl):
            return False
    tld = labels[-1]
    if "*" in tld:
        return False
    if len(tld) < 2 or not tld.isalpha():
        return False
    return True

def _has_wildcard(dom: str) -> bool:
    return "*" in dom

def _build_regexp(dom: str) -> str:
    labels = dom.split(".")
    pieces = []
    for lbl in labels:
        if lbl == "*":
            if WILDCARD_ALLOW_EMPTY:
                pieces.append(("soft", r"([^.]+\.)?"))
            else:
                pieces.append(("hard", r"[^.]+"))
        elif "*" in lbl:
            buf = ""
            sub = []
            for ch in lbl:
                if ch == "*":
                    if buf:
                        sub.append(re.escape(buf))
                        buf = ""
                    sub.append(r"[^.]+")
                else:
                    buf += ch
            if buf:
                sub.append(re.escape(buf))
            pieces.append(("hard", "".join(sub)))
        else:
            pieces.append(("plain", re.escape(lbl)))
    out = "^"
    for i, (kind, txt) in enumerate(pieces):
        if i > 0:
            prev_kind = pieces[i - 1][0]
            if prev_kind != "soft":
                out += r"\."
        out += txt
    out += "$"
    return out

# ---------- Парсеры ----------

def parse_plain(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = s.split("#", 1)[0].strip()
        if not s:
            continue
        if s.startswith("domain:"):
            s = s[len("domain:"):]
        elif s.startswith("full:"):
            s = s[len("full:"):]
        elif s.startswith("regexp:"):
            continue
        elif s.startswith("include:"):
            continue
        s = s.split("@", 1)[0].strip()
        s = re.sub(r'^https?://', '', s)
        s = s.split("/", 1)[0].strip()
        dom = _normalize_domain(s)
        if _is_valid_domain(dom):
            result[dom] = _has_wildcard(dom)
        for m in re.findall(r'[a-z0-9*][a-z0-9\-.*]*\.[a-z]{2,}', s, re.IGNORECASE):
            d = _normalize_domain(m)
            if _is_valid_domain(d):
                result.setdefault(d, _has_wildcard(d))
    return result

def parse_hosts(text: str) -> dict:
    result = {}
    ignore_hosts = {
        "localhost", "localhost.localdomain", "local", "broadcasthost",
        "ip6-localhost", "ip6-loopback", "ip6-allnodes", "ip6-allrouters",
    }
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = s.split("#", 1)[0].strip()
        parts = s.split()
        if len(parts) < 2:
            continue
        ip = parts[0]
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        for host in parts[1:]:
            h = _normalize_domain(host)
            if not h or h in ignore_hosts:
                continue
            if _is_valid_domain(h):
                result[h] = _has_wildcard(h)
    return result

def parse_dnscrypt(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("##"):
            s = s[2:].strip()
            if not s:
                continue
        elif s.startswith("#"):
            continue
        s = s.split("#", 1)[0].strip()
        if not s:
            continue
        s = s.split("@", 1)[0].strip()
        if not s:
            continue
        if s.startswith("sdns://") or s.startswith("http://") or s.startswith("https://"):
            continue
        s = s.strip("|^")
        if s.startswith("="):
            s = s[1:]
        dom = _normalize_domain(s)
        if _is_valid_domain(dom):
            result[dom] = _has_wildcard(dom)
        for m in re.findall(r'[a-z0-9*][a-z0-9\-.*]*\.[a-z]{2,}', s, re.IGNORECASE):
            d = _normalize_domain(m)
            if _is_valid_domain(d):
                result.setdefault(d, _has_wildcard(d))
    return result

def parse_auto(text: str) -> dict:
    fmt = detect_format(text)
    log(f"  Определён формат: {fmt}")
    if fmt == "hosts":
        return parse_hosts(text)
    elif fmt == "dnscrypt":
        return parse_dnscrypt(text)
    else:
        return parse_plain(text)

# ---------- Объединение ----------

def merge_domains(base: dict, new: dict) -> dict:
    for dom, is_wc in new.items():
        if dom not in base or is_wc:
            base[dom] = is_wc
    return base

# ---------- Белые списки ----------

def _domain_matches_pattern(dom: str, pattern: str) -> bool:
    dom = dom.lower()
    pattern = pattern.lower()
    if pattern.startswith("*."):
        base = pattern[2:]
        if dom == base:
            return False
        return dom.endswith("." + base) or (dom.startswith("*.") and dom[2:].endswith("." + base))
    if dom == pattern:
        return True
    if dom.endswith("." + pattern):
        return True
    if dom.startswith("*."):
        stripped = dom[2:]
        if stripped == pattern:
            return True
        if stripped.endswith("." + pattern):
            return True
    return False

def apply_whitelist(domains: dict, whitelist: dict) -> tuple:
    if not whitelist:
        return domains, 0
    wl_patterns = list(whitelist.keys())
    kept = {}
    removed = 0
    for dom, is_wc in domains.items():
        drop = False
        for pat in wl_patterns:
            if _domain_matches_pattern(dom, pat):
                drop = True
                break
        if drop:
            removed += 1
        else:
            kept[dom] = is_wc
    return kept, removed

# ---------- Запись файлов ----------

def _domain_line(dom: str, is_wc: bool) -> str:
    if is_wc:
        if WILDCARD_AS_DOMAIN and dom.startswith("*.") and dom.count("*") == 1:
            return f"domain:{dom[2:]}"
        else:
            return f"regexp:{_build_regexp(dom)}"
    else:
        return f"full:{dom}" if PLAIN_AS_FULL else f"domain:{dom}"

def write_category_file(name: str, domains: dict, includes: list):
    """
    Пишет data/<name> БЕЗ расширения — напрямую в формате
    domain-list-community.
    """
    path = DLC_DATA_DIR / name
    ensure_dir(path.parent)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Auto-generated: {name}\n")
        if domains:
            f.write("# --- Direct domains ---\n")
            for dom in sorted(domains.keys()):
                f.write(_domain_line(dom, domains[dom]) + "\n")
        if includes:
            f.write("# --- Includes ---\n")
            for inc in sorted(includes):
                f.write(f"include:{inc}\n")
    log(f"  Записано {len(domains)} доменов + {len(includes)} include в {path}")

# ---------- Сборка ----------

def _process_black_source(url, name, target, global_wl, local_wl):
    """
    Скачивает, парсит и фильтрует один чёрный источник.
    Возвращает (name, target, domains_filtered, removed_by_wl, error)
    или None при ошибке.
    """
    try:
        log(f"  [{name}] Скачивание: {url}")
        text = download_text(url)
        if not text:
            return (name, target, {}, 0, "пустой ответ")

        domains = parse_auto(text)
        if not domains:
            return (name, target, {}, 0, "нет доменов")

        # Сразу применяем whitelist (глобальный + локальный для этого target)
        combined_wl = {}
        if global_wl:
            combined_wl.update(global_wl)
        if local_wl:
            combined_wl.update(local_wl)

        removed = 0
        if combined_wl:
            domains, removed = apply_whitelist(domains, combined_wl)

        return (name, target, domains, removed, None)
    except Exception as e:
        return (name, target, {}, 0, str(e))


def collect_all(sources: list):
    grouped = defaultdict(dict)
    includes_map = defaultdict(set)
    name_order = []
    seen_names = set()
    name_order_lock = threading.Lock()

    whitelist_global = {}
    whitelist_by_target = defaultdict(dict)

    # ---------- ФАЗА 1: белые списки (последовательно) ----------
    white_sources = [s for s in sources if s[3]]
    black_sources = [s for s in sources if not s[3]]

    if white_sources:
        log("=" * 60)
        log("ФАЗА 1: Загрузка белых списков")
        log("=" * 60)

    for i, (url, name, target, is_white) in enumerate(white_sources, 1):
        tags = ["WHITE"]
        if target:
            tags.append(f"вычесть из {target}")
        log(f"  Белый источник #{i}: {name} [{' '.join(tags)}]")
        text = download_text(url)
        if not text:
            log(f"    Пропуск {name}: пустой ответ")
            continue
        domains = parse_auto(text)
        if not domains:
            log(f"    Пропуск {name}: нет доменов")
            continue

        if target:
            before = len(whitelist_by_target[target])
            merge_domains(whitelist_by_target[target], domains)
            log(f"    Белый список (для {target}): "
                f"+{len(whitelist_by_target[target]) - before}")
        else:
            before = len(whitelist_global)
            merge_domains(whitelist_global, domains)
            log(f"    Глобальный белый список: +{len(whitelist_global) - before}")

    # ---------- ФАЗА 2: чёрные списки (параллельно) ----------
    if black_sources:
        log("=" * 60)
        log(f"ФАЗА 2: Параллельная загрузка {len(black_sources)} чёрных источников")
        log(f"        Потоков: {MAX_WORKERS}")
        log("=" * 60)

    total_removed_inline = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _process_black_source,
                url, name, target,
                whitelist_global,
                whitelist_by_target.get(target, {}) if target else {},
            ): (url, name, target)
            for (url, name, target, _is_white) in black_sources
        }

        for fut in as_completed(futures):
            url, name, target = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                log(f"  [{name}] [Ошибка] {e}")
                continue

            r_name, r_target, domains, removed, err = result
            if err:
                log(f"  [{r_name}] Пропуск: {err}")
                continue

            # Сливаем в общий словарь под локом (потокобезопасно)
            with name_order_lock:
                if r_name not in seen_names:
                    seen_names.add(r_name)
                    name_order.append(r_name)

                before = len(grouped[r_name])
                merge_domains(grouped[r_name], domains)
                added = len(grouped[r_name]) - before

                if r_target:
                    includes_map[r_target].add(r_name)

            total_removed_inline += removed
            msg = (f"  [{r_name}] +{added} доменов "
                   f"(всего {len(grouped[r_name])})")
            if removed:
                msg += f", удалено по белому списку: {removed}"
            log(msg)

    if total_removed_inline:
        log("-" * 60)
        log(f"Всего удалено по белым спискам во время загрузки: "
            f"{total_removed_inline}")

    # ---------- Удаление старых файлов ----------
    all_names = set(grouped.keys()) | set(includes_map.keys())
    if DLC_DATA_DIR.exists():
        for old in DLC_DATA_DIR.iterdir():
            if not old.is_file():
                continue
            if "." in old.name:
                continue
            if old.name in all_names:
                old.unlink()
                log(f"  Удалён старый файл: {old}")

    # ---------- Запись файлов ----------
    log("=" * 60)
    log("Запись файлов категорий...")
    log("=" * 60)
    for name in name_order:
        own_includes = includes_map.get(name, set())
        write_category_file(name, grouped[name], sorted(own_includes))

    # «Пустые» target-файлы (только include)
    for tgt in set(includes_map.keys()):
        if tgt not in grouped:
            own_includes = includes_map.get(tgt, set())
            write_category_file(tgt, {}, sorted(own_includes))

    return list(grouped.keys())

# ---------- Проверка исходников ----------

def check_dlc_sources():
    """Проверяет, что рядом со скриптом есть main.go и go.mod."""
    main_go = Path("main.go")
    go_mod = Path("go.mod")
    if not main_go.exists():
        log(f"  [Ошибка] Не найден main.go в текущей директории.")
        log(f"  Положите скрипт рядом с исходниками domain-list-community")
        log(f"  (main.go, go.mod, go.sum и папка data/).")
        return False
    if not go_mod.exists():
        log(f"  [Ошибка] Не найден go.mod в текущей директории.")
        return False
    return True

def build_geosite():
    log("Сборка geosite.dat...")
    try:
        subprocess.run(["go", "version"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        log("  [Ошибка] Go не найден.")
        return False

    if not check_dlc_sources():
        return False

    # Удаляем старый geosite.dat, чтобы точно понять, был ли он пересоздан
    if OUTPUT_GEOSITE.exists():
        OUTPUT_GEOSITE.unlink()

    try:
        result = subprocess.run(
            ["go", "run", "./"],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            log(f"  [Ошибка] Сборка завершилась с кодом {result.returncode}")
            log(f"  STDERR: {result.stderr[:1000]}")
            return False
        log("  Сборка успешно завершена.")
    except subprocess.TimeoutExpired:
        log("  [Ошибка] Таймаут сборки.")
        return False

    if not OUTPUT_GEOSITE.exists():
        log(f"  [Ошибка] После сборки не найден {OUTPUT_GEOSITE}.")
        log(f"  Проверьте, что в main.go указано имя выходного файла "
            f"'{OUTPUT_GEOSITE.name}'.")
        return False

    log(f"Готовый файл: {OUTPUT_GEOSITE} "
        f"({OUTPUT_GEOSITE.stat().st_size} байт)")
    return True

# ---------- Генерация SRS ----------

def ensure_geodat2srs():
    global GEODAT2SRS_PATH

    found = shutil.which(GEODAT2SRS_BIN)
    if found:
        GEODAT2SRS_PATH = found
        log(f"  {GEODAT2SRS_BIN} найден: {found}")
        return True

    gopath_result = subprocess.run(
        ["go", "env", "GOPATH"],
        capture_output=True, text=True, timeout=30
    )
    if gopath_result.returncode == 0:
        gopath = gopath_result.stdout.strip()
        candidates = [
            Path(gopath) / "bin" / GEODAT2SRS_BIN,
            Path(gopath) / "bin" / f"{GEODAT2SRS_BIN}.exe",
        ]
        for c in candidates:
            if c.exists():
                GEODAT2SRS_PATH = str(c)
                log(f"  {GEODAT2SRS_BIN} найден в GOPATH: {c}")
                return True

    home = Path.home()
    candidates = [
        home / "go" / "bin" / GEODAT2SRS_BIN,
        home / "go" / "bin" / f"{GEODAT2SRS_BIN}.exe",
    ]
    for c in candidates:
        if c.exists():
            GEODAT2SRS_PATH = str(c)
            log(f"  {GEODAT2SRS_BIN} найден в $HOME/go/bin: {c}")
            return True

    log(f"  {GEODAT2SRS_BIN} не найден, устанавливаю через go install...")
    try:
        result = subprocess.run(
            ["go", "install", "github.com/runetfreedom/geodat2srs@latest"],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode != 0:
            log(f"  [Ошибка] Установка не удалась: {result.stderr[:500]}")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"  [Ошибка] {e}")
        return False

    gopath_result = subprocess.run(
        ["go", "env", "GOPATH"],
        capture_output=True, text=True, timeout=30
    )
    if gopath_result.returncode == 0:
        gopath = gopath_result.stdout.strip()
        candidates = [
            Path(gopath) / "bin" / GEODAT2SRS_BIN,
            Path(gopath) / "bin" / f"{GEODAT2SRS_BIN}.exe",
        ]
        for c in candidates:
            if c.exists():
                GEODAT2SRS_PATH = str(c)
                log(f"  Установлен и найден: {c}")
                return True

    home = Path.home()
    candidates = [
        home / "go" / "bin" / GEODAT2SRS_BIN,
        home / "go" / "bin" / f"{GEODAT2SRS_BIN}.exe",
    ]
    for c in candidates:
        if c.exists():
            GEODAT2SRS_PATH = str(c)
            log(f"  Установлен и найден: {c}")
            return True

    log(f"  [Ошибка] {GEODAT2SRS_BIN} установлен, но не найден.")
    return False

def build_srs():
    log("Генерация SRS...")

    if not OUTPUT_GEOSITE.exists():
        log(f"  [Ошибка] {OUTPUT_GEOSITE} не найден.")
        return False

    if not ensure_geodat2srs():
        log("  Пропуск генерации SRS: geodat2srs недоступен.")
        return False

    ensure_dir(SRS_OUTPUT_DIR)

    try:
        result = subprocess.run(
            [GEODAT2SRS_PATH, "geosite",
             "-i", str(OUTPUT_GEOSITE),
             "-o", str(SRS_OUTPUT_DIR),
             "--prefix", SRS_PREFIX],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            log(f"  [Ошибка] Конвертация завершилась с кодом {result.returncode}")
            log(f"  STDERR: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        log("  [Ошибка] Таймаут конвертации.")
        return False
    except FileNotFoundError:
        log(f"  [Ошибка] {GEODAT2SRS_PATH} не найден.")
        return False

    srs_files = list(SRS_OUTPUT_DIR.glob("*.srs"))
    log(f"  Сгенерировано {len(srs_files)} SRS-файлов в {SRS_OUTPUT_DIR}/")
    for f in srs_files[:5]:
        log(f"    - {f.name} ({f.stat().st_size} байт)")
    if len(srs_files) > 5:
        log(f"    ... и ещё {len(srs_files) - 5}")

    return True

# ---------- main ----------

def main():
    log("=== Начало сборки GeoSite ===")

    log("Шаг 0: Проверка исходников domain-list-community...")
    if not check_dlc_sources():
        sys.exit(1)

    log("Шаг 1: Чтение links.txt...")
    sources = parse_links_file(LINKS_FILE)
    for url, name, target, is_white in sources:
        extras = []
        if is_white:
            extras.append("WHITE")
        if target:
            if is_white:
                extras.append(f"вычесть из {target}")
            else:
                extras.append(f"include->{target}")
        suffix = f" [{' '.join(extras)}]" if extras else ""
        log(f"  - {name}{suffix}: {url}")

    log("Шаг 2: Загрузка и парсинг источников...")
    ensure_dir(DLC_DATA_DIR)
    collect_all(sources)

    log("Шаг 3: Генерация geosite.dat...")
    if not build_geosite():
        log("=== Ошибка сборки geosite.dat ===")
        sys.exit(1)

    log("Шаг 4: Генерация SRS...")
    if build_srs():
        log("=== Успех! ===")
    else:
        log("=== geosite.dat готов, но SRS не сгенерированы ===")

if __name__ == "__main__":
    main()
