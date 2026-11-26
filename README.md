<p align="center">
  <img src="assets/logo.svg" width="84" height="84" alt="GTNH Machine Hull Logo">
</p>

<h1 align="center">GTNH Machine Hull</h1>

<p align="center">
  Десктопный менеджер и Zero-Loss Pack Updater для серверов <strong>GregTech: New Horizons</strong> (Minecraft 1.7.10) на хостинге <strong>Bisquit.Host</strong> (Calagopus / Pterodactyl API) и локальных клиентов <strong>Prism Launcher / MultiMC</strong>.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Platform-Windows%2010%2F11%20x64-2b303b?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/Minecraft-1.7.10%20GTNH-e6953a?style=flat-square" alt="GTNH">
  <img src="https://img.shields.io/badge/License-MIT-3cc68a?style=flat-square" alt="MIT License">
</p>

---

## Возможности

- **Live WebSocket-консоль и телеметрия**
  - Живой поток логов сервера через встроенный RFC-6455 SSL WebSocket с автоматическим fallback на поллер (`/logs`, `fml-server-latest.log`, `latest.log`).
  - Парсинг TPS/MSPT, lag spikes, списка игроков онлайн и реальных лимитов CPU / RAM / Disk.
  - Управление питанием (`Start`, `Restart`, `Stop`, `Kill`), история команд и быстрые команды GTNH (`forge tps`, `backup start`, `dump_chunkloaders` и др.).

- **Бэкапы, X-Ray инспектор и Overlay Restore**
  - Работа с архивами ServerUtilities (`/backups`), облачными снапшотами панели и локальными резервными копиями клиента (`_gtnh_client_backup_*`) с откатом в 1 клик.
  - **Рентген-инспектор архивов:** мгновенное виртуальное чтение ZIP без распаковки (с fallback в песочницу) — проверка `level.dat`, активных измерений (`DIM*`, `PERSONAL_DIM_*`), приватов ServerUtilities и жил VisualProspecting.
  - **Overlay Restore:** хирургическое наложение заприваченных регионов `.mca` поверх мира без потери остальных измерений.

- **GTNH Pack Updater (Клиент + Сервер)**
  - **Работает локально для клиента без API-ключа** — достаточно указать инстанс Prism Launcher / MultiMC (есть автопоиск) и клиентский `.zip` новой версии GTNH.
  - Умный дифф модов (`mods/` и `mods/1.7.10/`): различает обновляемые штатные моды, пользовательские моды, отключённые `*.disabled` и конфликты ModID; корректно извлекает не-JAR ассеты (`mods/OpenSecurity/sounds/...`).
  - **Smart Overlay Merge для `config/`:** сохраняет конфиги кастомных модов и защищённые файлы (`angelica.cfg`, `angelica-options.json`, `shaders.properties`, `chromatictooltips.cfg`), не трогая `saves/`, `journeymap/`, `visualprospecting/`, `options.txt` и `instance.cfg`.
  - **Надёжное обновление сервера:** потоковая выгрузка больших `.zip` (`/files/upload`) или прямая загрузка по URL (`/files/pull`), атомарный перенос через `/_gtnh_update_rollback` с автоматическим откатом при сбое.

- **Студия Конфигов**
  - Редактирование `serverutilities.cfg`, `server.properties` и `java9args.txt` (с пресетом G1GC) прямо из приложения.

- **100% автономный интерфейс**
  - Никаких внешних CDN: весь HTML/CSS/JS и SVG-логотип встроены локально, приложение запускается мгновенно даже без интернета.

---

## Быстрый старт для пользователей

1. Скачайте готовый **`GTNH_Machine_Hull.exe`** из раздела **[Releases](../../releases)**.
2. Поместите файл в удобную папку (например, `D:\Tools\GTNH_Machine_Hull\`) и запустите.
   - Настройки сохраняются рядом с `.exe` в portable-режиме (или в `%APPDATA%\GTNHMachineHull`, если папка защищена от записи).
3. Если вы хотите обновлять **только клиент Prism Launcher** — перейдите в раздел **«Обновление сборки»** и выберите режим **«Только Клиент»** (API-ключ не требуется).
4. Для управления сервером на **Bisquit.Host** откройте раздел **«Подключение»**, введите URL панели, ваш **Client API Key** и **ID сервера**, затем нажмите **«Проверить подключение»** и **«Сохранить»**.

**Системные требования:** Windows 10 / 11 (x64) и [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) (обычно уже предустановлен в Windows).

---

## Запуск из исходников и сборка (для разработчиков)

```bash
git clone https://github.com/Danastroovakim/GTNH_Machine_Hull.git
cd GTNH_Machine_Hull
pip install -r requirements.txt
```

Запуск приложения:
```bash
python main.py
```

Сборка standalone `.exe` (результат появится в `dist/GTNH_Machine_Hull.exe`):
```bash
python build_exe.py
```

Дополнительные флаги сборки:
- `python build_exe.py --debug` — сборка с открытой консолью отладки.
- `python build_exe.py --onedir` — сборка в папку вместо одного файла.

---

## Безопасность

> **Важно:** файл `bisquit_config.json` создаётся локально при сохранении настроек и содержит ваш **Client API Key** и **ID сервера**.
> Никогда не публикуйте `bisquit_config.json`, не прикладывайте его к баг-репортам и не передавайте третьим лицам. Оба пользовательских конфига (`bisquit_config.json` и `gtnh_updater_profile.json`) исключены из `.gitignore` и не попадают в сборку `.exe`.

---

## Лицензия

Проект распространяется под лицензией [MIT](LICENSE).
