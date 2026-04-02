# HH.ru Reverse-Engineered API Map

Полная карта API hh.ru, добытая реверс-инженерингом JS-бандлов и SSR-данных.
476 эндпоинтов найдено, протестировано.

---

## Chatik API (chatik.hh.ru)

Полностью открытый внутренний API чатов. Авторизация через куки hh.ru.
Headers: `Origin: https://chatik.hh.ru`, `Referer: https://chatik.hh.ru/`, `X-XSRFToken: {_xsrf}`

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/chatik/api/chats` | GET | Полный список чатов (id, type, unread, lastMessage, resources) |
| `/chatik/api/chat_data?chatId=N` | GET | История чата (messages.items, participants) |
| `/chatik/api/chat_data_by_topic?topicId=N` | GET | Чат по ID топика переговоров |
| `/chatik/api/send` | POST | Отправка `{chatId, idempotencyKey, text}` |
| `/chatik/api/search?query=X` | GET | Поиск по чатам |
| `/chatik/api/unread` | GET | `{unreadCount, unreadSupportCount}` |
| `/chatik/api/counters` | GET | Все счётчики |
| `/chatik/api/participants/me` | GET | Данные текущего юзера |
| `/chatik/api/templates` | GET | Шаблоны сообщений |
| `/chatik/api/settings` | GET | Настройки |
| `/chatik/api/config` | GET | Конфиг (sentryDSN, build, apiHost, staticHost) |
| `/chatik/api/features` | GET | Feature flags |
| `/chatik/api/typing?chatId=N` | GET | Статус набора |
| `/chatik/api/pinned` | GET | Закреплённые чаты |
| `/chatik/api/archived` | GET | Архивные чаты |
| `/chatik/api/muted` | GET | Замьюченные |
| `/chatik/api/blocked` | GET | Заблокированные |
| `/chatik/api/drafts` | GET | Черновики |
| `/chatik/api/health` | GET | Healthcheck |
| `/chatik/api/status` | GET | Статус сервиса |

**Важно**: `/chat/messages?page=N` (hh.ru) — пагинация СЛОМАНА, всегда 20 чатов. `/chatik/api/chats` — правильная замена.

---

## Applicant APIs (hh.ru)

### Работающие ✅

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/shards/user_statuses/job_search_status?status=X` | PUT | Смена статуса поиска (active_search, looking_for_offers, not_looking_for_job) |
| `/applicant/vacancy_response/popup?vacancyId=N` | GET | Данные отклика: `resumeInconsistencies`, `test.hasTests`, `letterRequired`, `type` |
| `/applicant/vacancy_response/popup` | POST | Отправка отклика (FormData: resume_hash, vacancy_id, letter, lux) |
| `/shards/applicant/resumes` | GET | Все резюме с _attributes (percent, status, isSearchable, canPublishOrUpdate) |
| `/shards/applicant/artifacts/all` | GET | Фото и файлы `{images: [...]}` |
| `/shards/applicant/negotiations/possible_job_offers` | GET | Потенциальные офферы от работодателей |
| `/shards/vacancy/register_interaction` | POST | Аналитика `{vacancyId, interactionType: view\|click\|response\|show}` |
| `/shards/hhpro_ai_letter` | POST | AI сопроводительное письмо `{resumeHash, vacancyId}` (async) |
| `/shards/hhpro_ai_check_status` | GET | Опрос результата AI `?resumeHash=X&vacancyId=Y` |
| `/shards/resume/search` | GET | Поиск резюме (видим конкурентов) `?text=X&area=1&order_by=relevance` |
| `/shards/search/resume/clusters` | GET | Все фильтры HR с количеством `?text=X&area=1` |
| `/shards/vacancy/counts` | GET | Количество вакансий по запросу |
| `/shards/notifications/mark_as_viewed` | POST | Пометить уведомления прочитанными |
| `/shards/recommended_skills` | POST | Рекомендуемые навыки (400 — формат не найден) |

### Защищённые капчей ⚠️

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/applicant/resumes/touch` | POST | Поднятие резюме (302 → captcha) |
| `/shards/resume/edit/visibility` | POST | Видимость резюме (500) |
| `/shards/applicant/profile/update` | POST | Обновление профиля (500) |

---

## Public API (api.hh.ru)

Работает без авторизации:

| Endpoint | Описание |
|----------|----------|
| `/suggests/skill_set?text=X` | Автодополнение навыков |
| `/suggests/professional_roles?text=X` | Автодополнение ролей |
| `/professional_roles` | Полный каталог профессиональных ролей |
| `/dictionaries` | Все справочники HH (schedule, employment, experience, etc.) |

Требуют OAuth2 (hhtoken не работает как Bearer):
- `/me`, `/resumes/mine`, `/negotiations`, `/vacancies`

---

## SSR Data (HTML `<template id="HH-Lux-InitialState">`)

Каждая страница HH содержит JSON с полными данными:

| Страница | Ключ SSR | Данные |
|----------|----------|--------|
| `/resume/{hash}` | `applicantResume` | 30K chars — все поля, conditions, fieldStatuses, percent |
| `/applicant/resumes` | `applicantResumesStatistics` | search_shows, views, invitations за 7 дней |
| `/vacancy/{id}` | `vacancyView` | Полные данные вакансии, работодатель, навыки, зарплата |
| `/applicant/negotiations` | `applicantNegotiations` | Топик-лист с actions |
| Любая страница | `experiments` | A/B тесты для аккаунта |
| Любая страница | `features` | Feature flags |
| `/applicant/settings` | `xsrfToken`, `session` | Токены, сессия |

---

## Ключевые находки

### resumeInconsistencies
При GET `/applicant/vacancy_response/popup?vacancyId=N` HH возвращает:
```json
{
  "resumeInconsistencies": {
    "resume": [{
      "inconsistencies": {
        "inconsistency": [{
          "type": "EXPERIENCE",
          "required": "WORK_EXPERIENCE_FROM_3_YEAR_TO_6_YEAR",
          "actual": "WORK_EXPERIENCE_FROM_1_YEAR_TO_3_YEAR"
        }]
      }
    }]
  }
}
```
HR видит это как ⚠️ — снижает шансы. Можно фильтровать перед откликом.

### AI Cover Letter (бесплатно)
Эксперимент `hhpro_ai_cover_letter_v2: experiment` включён для аккаунта.
`POST /shards/hhpro_ai_letter` → 200, async генерация, poll через `hhpro_ai_check_status`.

### Аналитика рынка
`/shards/search/resume/clusters` для "тестировщик" в Москве:
- 35 411 активно ищут работу
- 254 669 хотят удалёнку
- 888 576 с фото (из 2.5М)
- Топ навыки: Пользователь ПК (240К), Работа в команде (206К)

---

## Справочники HH

### schedule
| ID | Название |
|----|----------|
| fullDay | Полный день |
| shift | Сменный график |
| flexible | Гибкий график |
| remote | Удалённая работа |
| flyInFlyOut | Вахтовый метод |

### experience
| ID | Название |
|----|----------|
| noExperience | Нет опыта |
| between1And3 | От 1 года до 3 лет |
| between3And6 | От 3 до 6 лет |
| moreThan6 | Более 6 лет |

### resume_access_type
| ID | Название |
|----|----------|
| no_one | Не видно никому |
| whitelist | Видно выбранным работодателям |
| blacklist | Скрыто от выбранных работодателей |
| clients | Видно всем работодателям на hh.ru |
| everyone | Видно всему интернету |
| direct | Доступно только по прямой ссылке |

### applicant_negotiation_status
| ID | Название |
|----|----------|
| active | Активные |
| invitations | Приглашения |
| response | Отклики |
| discard | Отказ |
| interview | Собеседование |
| hired | Выход на работу |

---

## Внутренняя инфраструктура

- **Chatik build**: `1.9.1`
- Internal infrastructure details omitted for security
