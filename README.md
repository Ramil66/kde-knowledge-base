# kde-knowledge-base

Локальная база знаний о работе над KDE. Команда `kde-kb` сохраняет исходные документы, связывает задачи, ветки, обсуждения и коммиты и ищет по ним через SQLite FTS5. Все данные хранятся в одном файле SQLite.

## Установка

Требуются Python 3.12 или новее и SQLite с FTS5. Из каталога проекта выполните:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
kde-kb init
kde-kb db status
```

Если Python установлен через uv, те же шаги можно выполнить так:

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'
source .venv/bin/activate
kde-kb init
```

По умолчанию каталог находится в `$XDG_DATA_HOME/kde-kb/catalog.sqlite3` или `~/.local/share/kde-kb/catalog.sqlite3`. Для отдельной базы задайте `KDE_KB_DATABASE` либо укажите путь перед подкомандой, например `kde-kb --database ./demo.sqlite3 init`.

## Первый импорт

В каталоге `examples` лежат учебные данные. Номера задач, обсуждения и участники служат примером формата. Следующий сценарий создаёт отдельную базу в текущем каталоге:

```bash
export KDE_KB_DATABASE="$PWD/demo.sqlite3"
kde-kb init
kde-kb import repo --file examples/repository.json
kde-kb import task --file examples/task.yaml
kde-kb import mr --file examples/mr.md
kde-kb task link-branch 527726 kio fix/file-name-too-long --source-url https://invent.kde.org/example/kio
kde-kb mr show kio 2214
kde-kb search "filename boundary"
kde-kb db backup --output demo-backup.sqlite3
kde-kb export --format json --output demo.json
```

Повторный импорт возвращает прежние идентификаторы. Изменённая версия получает отдельный исходный документ. Запись репозитория, задачи с внешним номером или MR обновляется по естественному ключу. Для задачи без внешнего номера дедупликация действует в пределах одного и того же исходного документа.

## Проверка

```bash
python -m pytest -q
ruff check src tests
ruff format --check src tests
```

[Команды CLI и форматы импорта](docs/usage.md) · [Схема БД и ER-диаграмма](docs/database.md) · [Архитектура, план MVP и следующий этап](docs/architecture.md)
