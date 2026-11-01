# Как предложить изменение

## Ветки
| Ветка | Что в ней | Кто пишет |
|---|---|---|
| `Dashboard-Main` | стабильная версия панели; из неё собираются образ и релизы | только владелец |
| `infrastructurePanel-Main` | только агент узлов (`homelab-agent`) в корне — зеркало `handoff/homelab-agent` | только владелец, вместе с релизом |
| `dev` | приём изменений: сюда идут все pull request'ы | через PR после одобрения |

## Порядок
1. Форк → ветка от `dev` → изменения → `pytest -q` (и тесты агента, если трогали его).
2. Pull request **в `dev`**. CI прогоняет тесты, аудит и сборку образа (без публикации).
3. Владелец просматривает и одобряет; после слияния CI ещё раз проверяет `dev`.
4. Когда `dev` проверен, владелец переносит изменения в стабильную ветку и выпускает релиз.

Собирать релизы и публиковать образы может только владелец: `release.yml` пропускает теги от других
пользователей, а в форке образ публикуется в его собственный реестр.

Свои настройки (пути, адреса, ключи) держите только в `.env` — он в `.gitignore`. В код, тесты и примеры —
только общие значения (`192.168.1.x`, `/opt/homelab-dashboard`, `owner/repo`).

## Настройка GitHub (для владельца, один раз)
Settings → **Rules → Rulesets** (или Branches → Branch protection rules):
- `Dashboard-Main`, `infrastructurePanel-Main`: запрет удаления и force push; изменения — только владелец
  (Bypass list: вы; «Restrict updates» для остальных).
- `dev`: «Require a pull request before merging» (1 одобрение, «Require review from Code Owners»,
  сброс одобрения при новых коммитах), «Require status checks to pass» — `test` и `image` из CI,
  запрет force push и удаления.
- Теги `v*` и `agent-v*`: ruleset для тегов — создание, изменение и удаление только владельцу.

Settings → **Actions → General**: «Require approval for all outside collaborators» (запуск CI из
чужих PR — после вашего просмотра), Workflow permissions — «Read repository contents».

Settings → General → Pull Requests: по желанию — только «Squash merging».

## Выпуск релиза
1. Поднять `__version__` в `app/__init__.py` и раздел `## X.Y.Z — дата` в `CHANGELOG.md`
   (агент — `handoff/homelab-agent/homelab_agent/__init__.py` и его `CHANGELOG.md`).
2. Перенести проверенный `dev` в `Dashboard-Main`, поставить тег `vX.Y.Z` и отправить:
   `git push origin Dashboard-Main vX.Y.Z`.
3. `release.yml` проверит версию, соберёт образ (amd64 + arm64) и создаст GitHub Release с архивами.
