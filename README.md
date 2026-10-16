# Магазин брокколи в Telegram

Витрина (Mini App) + бот для заказов. Витрина лежит на GitHub Pages, бот работает на сервере.

## Файлы
| Файл | Для чего |
|---|---|
| `index.html` | витрина (Mini App) |
| `catalog.json` | **всё, что вы правите**: сорта, цены, окна доставки, тексты |
| `bot.py` | бот: заказы, остатки, промокоды, рассылки, статистика |
| `requirements.txt`, `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `env.example` | запуск на сервере |

## Перед запуском проверьте в `catalog.json`
- `name`, `tagline`, `tz` (часовой пояс), `currency`
- цены сортов и сета, `min_order`, `delivery_fee`, `free_from`
- `delivery`: окна, `cutoff_hour`, `lead_hours`, `days_off`
- **`trust` и `faq`**: это обещания клиентам (свежесть, замена, возврат). Оставьте только то, что готовы выполнять.
- `support`: ваш username без @ (иначе кнопка поддержки в витрине скрыта)
- добавьте фото: положите файл в репозиторий и впишите имя в `"image"`

После правки `catalog.json` бот нужно перезапустить, витрина подхватит сама.

## Запуск
1. @BotFather → токен; @userinfobot → ваш ID.
2. Витрина: GitHub → Settings → Pages → ветка `main`, папка `/ (root)`.
3. Бот (для теста подойдёт Codespaces):
   ```
   pip install -r requirements.txt
   export BOT_TOKEN='...' ADMIN_ID='...' WEBAPP_URL='https://ИМЯ.github.io/РЕПО/'
   python bot.py
   ```
4. В Telegram: `/start` → кнопка «🥦 Открыть магазин» (только она передаёт заказ боту).

## Команды администратора
`/admin` открывает меню с кнопками. Также:
- `/orders` активные заказы, `/pick` лист сборки (`/pick tomorrow`), `/stats [дней]`
- `/stock` остатки: `/stock calabrese 25`, `/stock calabrese +10`, `/stock calabrese off`
- `/promo` промокоды: `/promo add SPRING15 15 [лимит]`
- `/pause [причина]` и `/resume` пауза приёма заказов
- `/broadcast текст` рассылка (клиенты могут отписаться: `/unsubscribe`)
- под каждым заказом кнопки статусов и «💰 Отметить оплату»

Клиентам: `/invite` (скидка другу и личный промокод вам после его первой доставки), `/help`.

## Постоянная работа на сервере (VPS ≈ $4–6 в месяц)
```
cp env.example .env     # впишите свои значения
docker compose up -d --build
docker compose logs -f
```
База лежит в `data/shop.db`. Каждый день бот сам присылает вам её копию.
Обновление: загрузить новые файлы и снова `docker compose up -d --build`.

### Живые остатки и пауза в витрине (необязательно)
Без этого витрина работает, а сервер всё равно не даст заказать больше остатка, но клиент узнает об этом только при отправке.
Чтобы остатки и пауза показывались сразу, нужен свой домен:
1. Направьте A-запись домена на IP сервера, в `.env` добавьте `DOMAIN=shop.example.com`.
2. `docker compose --profile https up -d --build`
3. В `catalog.json` впишите `"api": "https://shop.example.com"`.
