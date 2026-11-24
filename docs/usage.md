# Работа с каталогом

## Добавление вручную

Команды `repo add`, `task add` и `mr add` запрашивают обязательные поля, если их не передали в командной строке. Список доступных параметров выводит `--help` соответствующей команды.

```bash
kde-kb repo add --name kio --url https://invent.kde.org/frameworks/kio
kde-kb repo list
kde-kb repo update kio --default-branch master
kde-kb repo show kio
kde-kb task add --title "Report an overlong file name" --source example --external-id 527726 --type bug
kde-kb task show example:527726
kde-kb task update 527726 --status open
kde-kb mr add
kde-kb mr list --repo kio
kde-kb mr update kio 2214 --status merged
```

Номер задачи разрешается сначала по внешнему ID, затем по локальному. Для явного выбора используйте `example:527726` или `'#1'`. Команды `repo delete`, `task delete` и `mr delete` принимают те же идентификаторы, что и `show`. Удаление MR также удаляет его обсуждения, комментарии и позиции. Оригиналы доступны через `document show` по прежним ID. Репозиторий со связанными ветками или коммитами защищён внешними ключами.

## Импорт и оригиналы

| Вход | Как обрабатывается |
| --- | --- |
| JSON, YAML | Один объект с полями сущности |
| Markdown | Блок YAML между строками `---` в начале документа. Текст после блока становится описанием, если поле `description` отсутствует |
| Текст со страницы Invent, Markdown без заголовка | Исходный документ с возможностью ручной привязки |
| Git diff, patch | Полный текст изменений, который можно привязать к MR или задаче |

Для импорта используйте кодировку UTF-8. Файл читается с исходными переводами строк. Большой текст можно вставить через `kde-kb import mr --stdin` и завершить ввод с помощью Ctrl+D. Для YAML через stdin укажите `--format yaml`. Ссылку на страницу оригинала принимает `--source-url`.

```bash
kde-kb import mr --stdin --source-url https://invent.kde.org/frameworks/kio/-/merge_requests/2214 < copied-page.txt
kde-kb import diff --file changes.diff
kde-kb document show 4
kde-kb document link 4 mr 1
```

Здесь `4` — локальный ID оригинала из результата импорта, а `1` — локальный ID MR из `mr show`. Операция `document link` также принимает типы `repo` и `task`. Если приложение сохранило только оригинал, в результате будет `entity_id: null` и причина. После добавления отсутствующего репозитория можно повторить импорт. При недостатке структурированных полей создайте запись через `add` и привяжите к ней оригинал. Посмотреть исходный файл без изменений можно командой `document show ID > original.txt`.

## Поля структурированных документов

* Для репозитория нужны `name` и `url`. Дополнительно можно передать `architecture_summary`, `build_instructions`, `test_instructions`, `default_branch`, `depends_on` (имена или URL уже известных репозиториев).
* Для задачи нужен `title`. Внешний номер `external_id` передаётся вместе с `source`. Поля `reproduction_steps`, `actual_behavior`, `expected_behavior` и `solution_summary` сохраняют историю решения. Массив `depends_on` принимает ссылки на задачи. В `events` передаются объекты с `event_type`, необязательными `description` и `created_at`.
* Для MR нужны `repo`, `external_id`, `title`, `source_branch` и `source_repository_url`. Последнее поле указывает репозиторий, где находится ветка, включая форки. Номер и целевой репозиторий составляют ключ MR.
* Вложенные `discussions` и `comments` получают собственные `external_id`. Автор описывается полями `platform` и `username`. Текст `body` сохраняется полностью. Ответ на комментарий указывает `parent_external_id` в том же обсуждении. Для `position` нужны путь и ревизия. Пример находится в [mr.md](../examples/mr.md).
* В `commits` указываются SHA и зарегистрированный `repository`, при необходимости автор, сообщение и `changed_files` с путями и diff. В `test_runs` передаются команда и результат сборки или тестов (`command`, `result`), при необходимости ревизия, вывод и время. Элементы `depends_on` у MR содержат `repo` и `external_id`.

## Поиск, экспорт и восстановление

Поиск учитывает все слова запроса и возвращает тип сущности, её локальный ID и фрагмент текста. Команда `export` записывает версию схемы и содержимое прикладных таблиц в JSON. Резервная копия включает также поисковый индекс. Для открытия копии укажите её путь:

```bash
kde-kb --database ./demo-backup.sqlite3 db status
kde-kb --database ./demo-backup.sqlite3 search "filename"
```

Экспорт и резервное копирование создают новые файлы. Для обновления схемы существующей базы запустите `kde-kb db migrate`.
