# Арбитражный сканер MEXC ↔ Bybit

Сканирует спотовый межбиржевой арбитраж, шлёт находки в Telegram.
Только сканирование — никакой торговли.
Проверено: Debian 12, VPS 1 ядро / 768 MB RAM.

## 1. Настройка сервера (один раз)

Каждая команда — отдельно: вставил → Enter → дождался завершения.

    apt update && apt upgrade -y

    apt install -y python3 python3-venv screen

    mkdir ~/scanner && cd ~/scanner

    python3 -m venv venv

    source venv/bin/activate

    pip install aiohttp

## 2. Загрузка кода

Вариант А — SFTP в Termius: положить scanner.py из этого репозитория
в папку /root/scanner/

Вариант Б — через nano:

    cd ~/scanner && nano scanner.py

Вставить весь код → Ctrl+O → Enter → Ctrl+X

Проверка (должно быть ~870 строк):

    wc -l scanner.py

## 3. Секреты (.env)

Что нужно заранее:
- Telegram: бот от @BotFather (токен); ID канала — переслать сообщение
  из канала в @userinfobot (число с минусом); бот должен быть админом
  канала с правом публикации.
- Биржи: READ-only ключи, у Bybit право Wallet → Read, привязка к IP сервера.

Подставить свои значения и вставить блок целиком:

    cd ~/scanner
    cat > .env << 'EOF'
    export TG_BOT_TOKEN="токен"
    export TG_CHAT_ID="-100xxxxxxxxxx"
    export MEXC_API_KEY="ключ"
    export MEXC_API_SECRET="секрет"
    export BYBIT_API_KEY="ключ"
    export BYBIT_API_SECRET="секрет"
    EOF

Закрыть файл от чужих:

    chmod 600 .env

Проверка доставки в Telegram:

    source .env && curl -s "https://api.telegram.org/bot$TG_BOT_TOKEN/sendMessage" -d "chat_id=$TG_CHAT_ID" -d "text=Тест" && echo

Должно прийти сообщение в канал и показать "ok":true

## 4. Запуск 24/7

    screen -S scanner

Внутри screen:

    cd ~/scanner && source venv/bin/activate && source .env && python scanner.py

В первых строках НЕ должно быть "!!! ВНИМАНИЕ: сети... НЕ проверяются".
Если оно есть — проблема с ключами бирж.

Дальше просто закрыть Termius: screen отцепится сам, сканер продолжит работать.

## 5. Управление

| Действие | Команда |
|---|---|
| Жив ли сканер | screen -ls → должно быть (Detached) |
| Посмотреть вывод | screen -r scanner |
| Выйти из просмотра | Ctrl+A, потом D — или закрыть Termius |
| Остановить | screen -XS scanner quit |

После перезагрузки VPS сканер нужно запустить заново (раздел 4).

## Если что-то не так

- Предупреждение про непроверенные сети → не выполнен source .env,
  опечатка в ключах, не тот IP в привязке, или у Bybit нет Wallet → Read.
- Бот молчит → у бота не нажат Start / бот не админ канала / неверный chat_id.
- Мусор ^[[A^[[B на экране → свайп по терминалу, безвредно.
- Частые ретраи HTTP → лимиты MEXC: поднять LOOP_INTERVAL_SEC до 60
  или снизить MAX_CONCURRENCY до 5 (в CONFIG вверху scanner.py).
- Связок нет часами или днями → это норма для двух крупных бирж, не поломка.

## Важно

- .env НИКОГДА не коммитить в репозиторий.
- Каждую находку проверять на сайтах бирж вручную: оценка идёт по мгновенному
  стакану, статусы deposit/withdraw в API могут запаздывать.
