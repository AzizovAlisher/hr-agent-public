# Интеграция открытых вакансий на сайт компании

Recruiter отдает опубликованные вакансии через стабильный публичный API. Сайт
компании может запрашивать список, показывать карточки и принимать отклики прямо на
сайте. Готовый виджет уже содержит адаптивную форму в едином дизайне. API-ключ для
чтения и подачи заявки не нужен.

## Что считается опубликованной вакансией

Вакансия появляется в API, только если одновременно выполнены условия:

- `status` равен `open`;
- `public_apply_enabled` равен `true`;
- вакансия не находится в архивной папке;
- legacy-папка не начинается с `_`.

Закрытие вакансии, отключение публичного отклика или перенос в архив убирает ее
из каталога и делает публичную страницу отклика недоступной. Из-за HTTP-кеша
изменение может отображаться на внешнем сайте с задержкой до 60 секунд.

## Эндпоинты

Локальный пример для организации со slug `demo`:

```text
GET http://localhost:8000/public/v1/careers/demo/positions
GET http://localhost:8000/public/v1/careers/demo/positions/{position_id}
POST http://localhost:8000/public/v1/careers/demo/positions/{position_id}/apply
GET http://localhost:8000/public/v1/careers/demo/preview?locale=ru
```

Для production замените адрес API, настройте `WEB_ORIGIN` на публичный адрес
веб-приложения Recruiter и задайте `NEXT_PUBLIC_API_BASE_URL` для вкладки
«Настройки → Интеграция». Поле `apply_url` в ответе строится из `WEB_ORIGIN`.

### Список вакансий

```bash
curl --get 'http://localhost:8000/public/v1/careers/demo/positions' \
  --data-urlencode 'limit=20' \
  --data-urlencode 'location=Душанбе' \
  --data-urlencode 'employment_type=Full-time'
```

Параметры:

| Параметр | Назначение |
|---|---|
| `limit` | Число элементов, от 1 до 100; по умолчанию 50 |
| `cursor` | Непрозрачный курсор из `next_cursor` |
| `q` | Поиск по названию и публичному описанию |
| `company` | Точное совпадение компании без учета регистра |
| `location` | Точное совпадение локации без учета регистра |
| `employment_type` | Точное совпадение типа занятости без учета регистра |

Ответ отсортирован по `updated_at DESC, id DESC`, поэтому пагинация остается
детерминированной даже при одинаковом времени обновления.

```json
{
  "organization": {
    "slug": "demo",
    "name": "Демо Холдинг",
    "logo_url": "https://api.example.com/storage/logos/company.png",
    "accent_color": "#10B981"
  },
  "items": [
    {
      "id": "70af218f-95cf-48da-aebd-a8c45dbb6d63",
      "title": "Backend Engineer",
      "description": "Разработка внутренних и публичных API.",
      "full_description": "Расширенное описание вакансии.",
      "requirements": {"stack": ["Python", "PostgreSQL"]},
      "company": "Демо Холдинг",
      "location": "Душанбе",
      "work_format": "Hybrid",
      "employment_type": "Full-time",
      "salary_range": "По результатам интервью",
      "seniority": "Middle",
      "apply_url": "https://recruit.example.com/apply/70af218f-95cf-48da-aebd-a8c45dbb6d63?utm_source=careers_api",
      "api_url": "https://api.example.com/public/v1/careers/demo/positions/70af218f-95cf-48da-aebd-a8c45dbb6d63",
      "created_at": "2026-07-20T09:30:00Z",
      "updated_at": "2026-07-20T09:30:00Z"
    }
  ],
  "total": 1,
  "next_cursor": null
}
```

`next_cursor: null` означает, что следующей страницы нет. Курсор нельзя
разбирать или изменять на стороне сайта.

### Одна вакансия

```bash
curl 'http://localhost:8000/public/v1/careers/demo/positions/70af218f-95cf-48da-aebd-a8c45dbb6d63'
```

Неопубликованная вакансия, чужая организация и неизвестный ID одинаково
возвращают `404`, чтобы не раскрывать существование внутренних вакансий.

### Подача отклика

Форма отправляет `multipart/form-data`:

```bash
curl --request POST \
  'http://localhost:8000/public/v1/careers/demo/positions/70af218f-95cf-48da-aebd-a8c45dbb6d63/apply' \
  --form 'name=Иван Иванов' \
  --form 'phone=+992 90 123 4567' \
  --form 'email=ivan@example.com' \
  --form 'source=company_website' \
  --form 'file=@cv.pdf;type=application/pdf'
```

| Поле | Назначение |
|---|---|
| `name` | Имя кандида, обязательно |
| `phone` | Телефон, обязательно |
| `email` | Email, необязательно |
| `source` | Канал интеграции; по умолчанию `company_website` |
| `file` | CV в PDF или DOCX, до 15 МиБ |
| `raw_text` | Текст CV вместо `file`, до 200 000 символов |

Нужно передать `file` или `raw_text`. Повторный контакт в той же организации
возвращает `409`. При успехе API отвечает `201`, создает кандидата, безопасное событие
в таймлайне и уведомления рекрутерам в одной транзакции. Автооценка запускается в фоне.
Кандидат сразу появляется в выбранной вакансии Recruiter на этапе `new` с пустой оценкой.
Эта видимость не зависит от скорости или доступности AI-провайдера; после завершения фоновой оценки та же
запись заполняется баллами и переходит в `screening`.

## Подключение к сайту

API разрешает браузерные `GET`, `POST` и `OPTIONS`-запросы с любого домена только для маршрутов
`/public/v1/careers/*`. Авторизованные и внутренние маршруты по-прежнему
ограничены настроенным `WEB_ORIGIN`.

### Готовый виджет для любого сайта или CMS

Самый быстрый вариант — вставить два тега в нужное место страницы:

```html
<div
  data-recruiter-careers
  data-organization="demo"
  data-locale="ru"
></div>
<script
  async
  src="https://api.example.com/public/v1/careers/widget.js"
></script>
```

Виджет сам загружает вакансии, применяет логотип и акцентный цвет организации,
показывает поиск, состояния загрузки/ошибки/пустого списка, кнопку пагинации и встроенную
модальную форму отклика. Кандидат может загрузить PDF/DOCX или вставить резюме текстом; отдельная
форма Recruiter остается резервным вариантом. Виджет не требует React или другой frontend-библиотеки и
изолирует свои стили через Shadow DOM.

Поддерживаемые атрибуты контейнера:

| Атрибут | Назначение |
|---|---|
| `data-organization` | Обязательный slug организации |
| `data-locale` | `ru` или `en`; иначе используется язык HTML-страницы |
| `data-limit` | Вакансий на страницу, от 1 до 100; по умолчанию 12 |
| `data-title` | Свой заголовок блока |
| `data-intro` | Свой подзаголовок |
| `data-api-base` | Необязательный адрес API, если он отличается от домена скрипта |
| `data-source` | Метка источника отклика; по умолчанию `company_website` |
| `data-phone-prefix` | Начальное значение телефона; по умолчанию `+992 ` |

Для строгого Content Security Policy добавьте API-домен в директивы
`script-src` и `connect-src`, а домен логотипов — в `img-src`.

Точный вид виджета можно проверить до вставки на сайт по адресу
`/public/v1/careers/{organization_slug}/preview?locale=ru`. Эта же ссылка доступна по кнопке
«Предпросмотр» в разделе «Настройки → Интеграция».

### Собственный интерфейс

Простой JavaScript-запрос:

```js
const apiBase = "https://api.example.com";
const organizationSlug = "demo";

const response = await fetch(
  `${apiBase}/public/v1/careers/${organizationSlug}/positions?limit=50`,
);

if (!response.ok) throw new Error(`Careers API: ${response.status}`);
const page = await response.json();

for (const vacancy of page.items) {
  console.log(vacancy.title, vacancy.location, vacancy.apply_url);
}
```

Рабочий standalone-пример с состояниями загрузки, ошибки, пустого списка и
кнопкой «Показать еще» находится в
[`examples/careers-widget.html`](./examples/careers-widget.html). Перед
публикацией замените `API_BASE` и `ORGANIZATION_SLUG` в начале скрипта.

Для Next.js рекомендуется получать данные на сервере, чтобы вакансии попадали
в HTML и индексировались поисковыми системами:

```tsx
const API_BASE = process.env.RECRUITER_API_BASE_URL!;
const ORGANIZATION_SLUG = process.env.RECRUITER_ORGANIZATION_SLUG!;

export default async function CareersPage() {
  const response = await fetch(
    `${API_BASE}/public/v1/careers/${ORGANIZATION_SLUG}/positions?limit=100`,
    { next: { revalidate: 60 } },
  );
  if (!response.ok) throw new Error("Не удалось загрузить вакансии");
  const page = await response.json();

  return (
    <main>
      <h1>Открытые вакансии</h1>
      {page.items.map((vacancy: any) => (
        <article key={vacancy.id}>
          <h2>{vacancy.title}</h2>
          <p>{vacancy.location ?? "Локация не указана"}</p>
          <a href={vacancy.apply_url}>Откликнуться</a>
        </article>
      ))}
    </main>
  );
}
```

## Кеширование

Ответы содержат:

- `Cache-Control: public, max-age=60, stale-while-revalidate=300`;
- `ETag` для условного запроса через `If-None-Match`;
- `X-Total-Count` для общего количества вакансий.

При совпадении ETag API возвращает `304 Not Modified` без тела. Это снижает
нагрузку при частом обновлении каталога.

## Безопасность контракта

API не возвращает кандидатов, контакты рекрутеров, внутренние заметки, источник
импорта, URL источника, LLM-промпты и поля с именем, начинающимся на `_`.
Фильтрация служебных ключей в `requirements` выполняется рекурсивно. На сайте
все значения нужно выводить как текст; не вставляйте их через `innerHTML`.

## Управление публикацией

Авторизованный Recruiter API уже поддерживает изменение публикации:

```http
PATCH /positions/{position_id}
Authorization: Bearer <recruiter-jwt>
Content-Type: application/json

{"public_apply_enabled": false}
```

Для повторной публикации передайте `true`. Статус вакансии меняется тем же
маршрутом: `{"status": "open"}` или `{"status": "closed"}`. Этот запрос
должен выполняться только из доверенного приложения Recruiter, а не из
публичного сайта компании.

## Чек-лист production

1. Опубликовать API по HTTPS.
2. Установить `WEB_ORIGIN` на публичный URL Recruiter, где открывается
   `/apply/{position_id}`.
3. Установить `NEXT_PUBLIC_API_BASE_URL` на внешний HTTPS-адрес API.
4. Указать production `API_BASE` и slug организации на сайте компании.
5. Проверить список, страницу вакансии и полный тестовый отклик.
6. Добавить ссылку на политику обработки персональных данных рядом с формой.
7. Настроить мониторинг `5xx` и алерт, если каталог перестал загружаться.

Готовые шаблоны переменных находятся в `apps/api/.env.example` и
`apps/web/.env.example`. `API_BASE_URL` может быть внутренним адресом между
контейнерами, но `NEXT_PUBLIC_API_BASE_URL` и `WEB_ORIGIN` обязательно должны
быть внешними HTTPS-адресами, доступными браузеру кандидата.
