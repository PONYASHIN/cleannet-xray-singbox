# 🌐 GeoBlock Builder

> Автоматическая сборка `geosite.dat` для Xray и `.srs` для sing-box
> для блокировки телеметрии и рекламы на уровне клиента


### Когда стоит использовать данные списки

| Ситуация |
|---|
| **Не нагружать свой/чужой VPS мусором** (например, обновления Windows `win-update`) |
| **Использование софта с зелёного стима** (`activation`) |
| **Блокировка рекламы и трекеров без использования стронних DNS/Hosts** |

---

### Описание категорий

1. `activation` Список адресов для активации софта
2. `ads-software` Список адресов телеметрии ПО (список небольшой и лучше использовать `ads-trackers`)
3. `ads-trackers` Небольшой список рекламы и трекеров, чаще всего его хватает с головой
4. `ads-trackers-extreme` Огромный список рекламы и трекеров (может быть много False Positive)
5. `apple` Телеметрия Apple
6. `games` Телеметрия в играх и их лаунчерах
7. `google` Телеметрия google
8. `huawei` Мусор от huawei
9. `mining` Адреса с скриптами для майнинга
10. `nvidia` Мусор от nvidia
11. `samsung` Телеметрия samsung
12. `smart-tv` Адреса телеметрии разных smart-tv
13. `twitch` Телеметрия и реклама twitch
14. `win-spy` Телеметрия Windows
15. `win-update` Обновления Windows
16. `xiaomi` Мусор от xiaomi


---

## 📖 Что это

Python-скрипт, который:

1. Читает список источников из `links.txt`
2. Загружает и парсит домены в **несколько потоков**
3. **Сразу при загрузке** применяет белые списки — удаляет домены, попавшие в whitelist
4. Генерирует файлы в формате `domain-list-community`
5. Собирает `geosite.dat`
6. Собирает `.srs` для sing-box через `geodat2srs`

---

## ✨ Возможности

| Возможность | Описание |
|---|---|
| **Три формата на входе** | `hosts`, `dnscrypt`, обычный список доменов — определяется автоматически |
| **Многопоточная загрузка** | Источники качаются параллельно через `ThreadPoolExecutor` (по умолчанию 8 потоков) |
| **Фильтрация на лету** | Whitelist применяется сразу после парсинга каждого источника |
| **Wildcard** | `*.example.com`, `sub.*.example.com`, `*ads*.example.com` |
| **Белые списки** | Глобальные или привязанные к конкретному файлу |
| **include** | Один файл может включать домены другого |
| **SRS** | Готовые `.srs` для sing-box |
| **CI/CD** | Ежедневная автосборка через GitHub Actions |

---

## 🚀 Быстрый старт

### Локально

```bash
# 1. Установите Python-зависимости
pip install certifi

# 2. Убедитесь, что Go установлен
go version

# 3. Клонируйте репозиторий

# 4. Отредактируйте links.txt

# 5. Запустите
python build_geosite.py
```

### Результаты

| Файл | Назначение |
|---|---|
| `geosite.dat` | Правила доменов для Xray |
| `srs/geosite-*.srs` | Правила доменов для sing-box |
| `data/*` | Промежуточные файлы |

---

## 📝 Формат `links.txt`

```
# Простой источник — сохраняется как data/<name>
URL#name

# Источник + include — сохраняется в data/<name>,
#   и в data/<target> добавляется строка include:<name>
URL#name#target

# Белый список — не сохраняется, домены удаляются из всех чёрных файлов
URL#name!white

# Белый список только для указанного файла
URL#name!white#target
```

Несколько источников могут писать в один файл. Дубли автоматически удаляются.

### Пример

```
# Обычная группа
https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts#ads-trackers

# ads-trackers-extreme: два источника пишут в один файл, оба включаются в ads-trackers-extreme
https://hosts.ubuntu101.co.za/hosts#ads-trackers-extreme
https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/wildcard/ultimate-onlydomains.txt#ads-trackers-extreme

# Глобальный белый список — вычитается из всех файлов
https://raw.githubusercontent.com/PONYASHIN/pihole-whitelists/refs/heads/main/whitelist_domains#whitelist!white

# Белый список только для adblock
https://raw.githubusercontent.com/PONYASHIN/pihole-whitelists/refs/heads/main/whitelist_domains#whitelist!white#adblock
```

---

## 🔧 Настройки

Все параметры — в начале `build_geosite.py`:

| Параметр | По умолчанию | Описание |
|---|---|---|
| `DLC_DATA_DIR` | `Path("data")` | Куда сохранять файлы категорий |
| `OUTPUT_GEOSITE` | `Path("geosite.dat")` | Итоговый geosite.dat |
| `SRS_OUTPUT_DIR` | `Path("srs")` | Папка для SRS |
| `SRS_PREFIX` | `geosite-` | Префикс имён SRS-файлов |
| `LINKS_FILE` | `Path("links.txt")` | Файл со ссылками |
| `MAX_WORKERS` | `8` | Количество потоков для загрузки |
| `PLAIN_AS_FULL` | `False` | Обычные домены как `full:` (True) или `domain:` (False) |
| `WILDCARD_AS_DOMAIN` | `True` | `*.example.com` → `domain:example.com` (True) или `regexp:` (False) |
| `WILDCARD_ALLOW_EMPTY` | `False` | `sub.*.example.com` — `*` может быть пустым (True) или нет (False) |

### Что означают форматы

| Синтаксис | Что покрывает |
|---|---|
| `domain:example.com` | `example.com` + все поддомены |
| `full:example.com` | Только `example.com` |
| `regexp:^[^.]+\.example\.com$` | Регулярное выражение |

---

## ⚡ Как работает многопоточность

Скрипт делит работу на две фазы:

**Фаза 1 — белые списки (последовательно).** Их обычно мало, и они нужны как фильтры до начала загрузки чёрных списков. Все `!white` источники скачиваются и складываются в глобальный и/или привязанные к target словари.

**Фаза 2 — чёрные списки (параллельно).** Через `ThreadPoolExecutor` с `MAX_WORKERS` потоками. Каждый воркер:
1. Скачивает текст источника.
2. Парсит домены (auto-detect формата).
3. **Сразу применяет whitelist** (глобальный + локальный для своего target).
4. Возвращает уже отфильтрованный словарь в главный поток.

Главный поток сливает результаты в общие структуры под локом — блокировка держится миллисекунды, только на момент слияния.

### Настройка `MAX_WORKERS`

- **4** — если ловите rate-limit от `raw.githubusercontent.com`.
- **8** (по умолчанию) — универсально.
- **16** — если источников много и сеть быстрая.

### Пример вывода

```
[12:00:00] ============================================================
[12:00:00] ФАЗА 1: Загрузка белых списков
[12:00:00] ============================================================
[12:00:00]   Белый источник #1: allowlist [WHITE]
[12:00:01]     Глобальный белый список: +1523
[12:00:01]   Белый источник #2: adblock_wl [WHITE вычесть из adblock]
[12:00:02]     Белый список (для adblock): +412
[12:00:02] ============================================================
[12:00:02] ФАЗА 2: Параллельная загрузка 4 чёрных источников
[12:00:02]         Потоков: 8
[12:00:02] ============================================================
[12:00:04]   [ads-trackers] +77078 доменов (всего 77078), удалено по белому списку: 1354
[12:00:05]   [xiaomi] +53434 доменов (всего 53434), удалено по белому списку: 218
[12:00:06]   [google] +8409 доменов (всего 8409), удалено по белому списку: 12
[12:00:06] ------------------------------------------------------------
[12:00:06] Всего удалено по белым спискам во время загрузки: 1631
```

---


---

## 🔌 Использование результатов

### Xray

```json
{
  "geosite": {
    "paths": ["./geosite.dat"]
  },
  "routing": {
    "rules": [
      {
        "type": "field",
        "domain": ["geosite:ads-trackers"],
        "outboundTag": "block"
      }
    ]
  }
}
```

### sing-box

```json
{
  "route": {
    "rule_set": [
      {
        "type": "local",
        "tag": "geosite-ads-trackers",
        "format": "binary",
        "path": "./srs/geosite-ads-trackers.srs"
      }
    ],
    "rules": [
      {
        "rule_set": "geosite-ads-trackers",
        "outbound": "block"
      }
    ]
  }
}
```

---

## ❓ FAQ

### `geodat2srs: command not found`

Скрипт ищет бинарник в `PATH`, `$GOPATH/bin`, `$HOME/go/bin`. Если не нашёл — устанавливает через `go install` и снова ищет. Если всё равно не работает — проверьте, что `$HOME/go/bin` существует и добавьте в `PATH`.

### Файлы в `data/` не перезаписываются

Скрипт удаляет только те списки в `data/`, чьи имена совпадают с именами ваших категорий. Другие списки не трогаются — если только ваш список с ними не совпадает по имени.

### Как временно отключить белые списки

Закомментируйте строки с `!white` в `links.txt` символами `#` в начале строки.

### Домены с `*` в середине

`sub.*.example.com` превращается в `regexp:^sub\.[^.]+\.example\.com$` — покрывает `sub.a.example.com`, но не `sub.example.com`. Чтобы `*` мог отсутствовать (мягкий режим), установите `WILDCARD_ALLOW_EMPTY = True`.

---

## 📁 Структура проекта

```
.
├── .github/
│   └── workflows/
│       └── build.yml               # GitHub Actions
├── build_geosite.py                # Основной скрипт
├── links.txt                       # Список источников
├── main.go                         # domain-list-community (имя выходного файла задано здесь)
├── go.mod
├── go.sum
├── data/                           # Генерируемые категории
│   ├── ads-trackers                
│   ├── ads-trackers-extreme
│   └── win-update
├── geosite.dat                     # Результат
├── srs/                            # SRS-файлы
│   └── geosite-adblock.srs
└── README.md
```

---

## 🙏 Источники

- [domain-list-community](https://github.com/v2fly/domain-list-community) — компилятор geosite.dat
- [geodat2srs](https://github.com/runetfreedom/geodat2srs) — конвертер в SRS
- [StevenBlack/hosts](https://github.com/StevenBlack/hosts) — популярный источник
- и др из файла links.txt
