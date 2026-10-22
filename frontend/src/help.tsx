import { useTranslation } from 'react-i18next';
// Инструкция / onboarding help (live acceptance addendum #1-#5).
// ОДИН источник текстов: визард и страница «Инструкция» читают
// отсюда — две противоречащие копии не поддерживаются.

import React from 'react';
import { api } from './api';
import type { WizardStepId } from './wizard';

export function HelpCard({ title, children, testid }: { title: string; children: React.ReactNode; testid?: string }) {
  return (
    <details className="help-card" data-testid={testid ?? 'help-card'}>
      <summary>Как это сделать? · {title}</summary>
      <div className="help-card-body">{children}</div>
    </details>
  );
}

function Steps({ items }: { items: string[] }) {
  return (
    <ol>
      {items.map((s, i) => <li key={i}>{s}</li>)}
    </ol>
  );
}

// ----------------------------------------------------------------------
// Wizard step help (canonical text, addendum #1)
// ----------------------------------------------------------------------
export const WIZARD_HELP: Partial<Record<WizardStepId, { title: string; body: React.ReactNode }>> = {
  admin: {
    title: 'Администратор SHMAX',
    body: (
      <>
        <p>Это локальный администратор SHMAX — он не связан с MAX или Telegram.</p>
        <ul>
          <li>Ссылка настройки владельца — одноразовая.</li>
          <li>Сохраните пароль администратора в надёжном месте.</li>
        </ul>
      </>
    ),
  },
  'max-network': {
    title: 'Сеть MAX до входа в аккаунт',
    body: <><p>Оставьте прямое подключение, если сервер видит MAX. Если сервер находится за границей и MAX недоступен, выберите подходящий прокси или узел Xray с доступом к MAX.</p><p>Сохраните настройку, затем нажмите «Проверить сохранённое подключение». Успешная проверка сети не означает вход в аккаунт. Настройки Telegram не меняются.</p></>,
  },
  max: {
    title: 'Вход в MAX',
    body: (
      <>
        <ul>
          <li>Введите телефон, привязанный к MAX.</li>
          <li>SMS может идти долго — не нажимайте «Отправить снова» несколько раз подряд.</li>
          <li>Если MAX пишет «Требуется установить 2FA» — сначала включите двухфакторную защиту в официальном приложении MAX, затем вернитесь.</li>
          <li>После SMS SHMAX может запросить пароль 2FA MAX.</li>
        </ul>
      </>
    ),
  },
  'telegram-network': {
    title: 'Выбор сети для Telegram',
    body: (
      <>
        <p>Эта сеть применяется только к Telegram. Подключение MAX настраивается отдельно на шаге «Сеть MAX».</p>
        <p><strong>Direct</strong> — используйте, если Telegram API доступен напрямую.</p>
        <p><strong>SOCKS5 / HTTP</strong> — используйте внешний прокси для доступа к Telegram.</p>
        <p><strong>Xray подписка</strong> — вставьте URL подписки VLESS/Trojan. SHMAX сам запустит локальный Xray и направит через него только Telegram.</p>
        <Steps items={[
          'Вставьте URL подписки',
          'Нажмите «Получить подписку»',
          'Выберите рабочую ноду',
          'Нажмите «Проверить соединение»',
          'После статуса «Telegram доступен» переходите дальше',
        ]} />
      </>
    ),
  },
  'telegram-bot': {
    title: 'Как создать бота?',
    body: (
      <Steps items={[
        'Откройте @BotFather',
        'Отправьте /newbot',
        'Укажите имя',
        'Username должен заканчиваться на bot',
        'Скопируйте token в SHMAX',
        'В BotFather выполните /setprivacy',
        'Выберите созданного бота',
        'Disable Privacy Mode для обычных сообщений группы; также проверьте права администратора бота',
      ]} />
    ),
  },
  'telegram-group': {
    title: 'Как подключить группу?',
    body: (
      <Steps items={[
        'Создайте Telegram-группу',
        'Добавьте вашего SHMAX-бота',
        'Включите «Темы / Topics» — группа должна стать Forum/Supergroup',
        'Сделайте бота администратором',
        'Дайте права: отправка сообщений и управление темами',
        'Нажмите «Получить одноразовый код» в SHMAX',
        'Отправьте в General topic: /shmax_connect <КОД>',
        'Вернитесь в SHMAX — группа определится автоматически',
      ]} />
    ),
  },
  'max-chats': {
    title: 'Какие чаты выбрать?',
    body: (
      <>
        <ul>
          <li>Выберите, какие чаты MAX должны появляться в Telegram.</li>
          <li>Каждый выбранный чат станет отдельной темой в Telegram-группе.</li>
          <li>Маршруты можно изменить позже.</li>
        </ul>
      </>
    ),
  },
  users: {
    title: 'Кто может писать в MAX?',
    body: (
      <>
        <ul>
          <li>Пользователи Telegram НЕ получают право писать в MAX автоматически.</li>
          <li>Одобряйте только доверенных пользователей. Новые участники не одобрены и имеют роль read_only; Telegram admin не даёт SHMAX admin.</li>
          <li>Заблокированные и read-only пользователи не могут писать в MAX.</li>
        </ul>
        <p>«Подпись в MAX» — локальное имя автора: например, [Анна]: Клиенту уже ответили. Проверьте предпросмотр и сохраните подпись; «Очистить» возвращает имя Telegram. Профиль Telegram и права доступа не меняются, синхронизация сохраняет подпись.</p>
        <p>Необязательная синхронизация импортирует существующих молчавших участников — им не нужно писать боту. На странице Telegram укажите API ID и API Hash из <a href="https://my.telegram.org" target="_blank" rel="noreferrer">my.telegram.org</a>, сохраните настройки; в «Пользователях» нажмите «Синхронизировать с Telegram» и проверьте результат.</p>
        <p>Используется существующий бот, без userbot и входа в человеческий Telegram-аккаунт. API Hash — секрет, не отправляйте его в поддержку. Без этих настроек мост работает: членство обновляется через chat_member, видимые сообщения остаются запасным способом обнаружения. Ошибка синхронизации не останавливает мост.</p>
      </>
    ),
  },
  test: {
    title: 'Что проверяется?',
    body: (
      <>
        <ul>
          <li>Сессия MAX</li>
          <li>Сеть Telegram</li>
          <li>Бот</li>
          <li>Права группы/форума</li>
          <li>Маршруты и темы</li>
          <li>Авторизованные пользователи</li>
          <li>Очередь доставки</li>
        </ul>
        <p>Каждая ошибка сопровождается подсказкой, где её исправить.</p>
        <p>getMe проверяет API и личность бота, но не доставку сообщений группы. Предупреждение Privacy Mode означает, что getMe не подтвердил отключение режима. Проверьте права администратора, /setprivacy → Disable в BotFather и живое сообщение в General.</p>
      </>
    ),
  },
  finish: {
    title: 'Что дальше?',
    body: (
      <>
        <ul>
          <li>Конфигурацию можно менять позже в соответствующих разделах.</li>
          <li>Постоянные данные остаются в /var/lib/shmax.</li>
          <li>Приложение может заменяться при обновлении — данные не затрагиваются.</li>
        </ul>
      </>
    ),
  },
};

export function WizardHelpCard({ stepId }: { stepId: WizardStepId }) {
  const help = WIZARD_HELP[stepId];
  if (!help) return null;
  return <HelpCard title={help.title} testid={`wizard-help-${stepId}`}>{help.body}</HelpCard>;
}

// ----------------------------------------------------------------------
// Troubleshooting (addendum #3)
// ----------------------------------------------------------------------
export const TROUBLESHOOTING: Array<[string, React.ReactNode]> = [
  ['SMS MAX долго не приходит', <>Подождите 2–3 минуты. Не нажимайте «Отправить снова» несколько раз подряд — это сбрасывает очередь SMS.</>],
  ['MAX требует 2FA', <>Сначала включите двухфакторную защиту в официальном приложении MAX, затем повторите вход в SHMAX.</>],
  ['Внешний сервис недоступен', <>Для Telegram проверьте Direct, SOCKS5/HTTP или Xray на шаге «Telegram сеть». Для MAX отдельно проверьте выбранный режим на странице «Сеть MAX» и состояние сессии.</>],
  ['Telegram bot не проверяется', <>Проверьте выбранную сеть, корректность token и доступность прокси/Xray.</>],
  ['Группа не подключается', <>Проверьте: включены ли Темы (Forum), бот — администратор, есть права «отправка сообщений» и «управление темами».</>],
  ['SHMAX не видит /shmax_connect', <>Проверьте сеть, живой слушатель, действующий код в General, права бота и /setprivacy → Disable в BotFather. Отсутствие команды само по себе не доказывает включённый Privacy Mode.</>],
];

// ----------------------------------------------------------------------
// Help page (addendum #2)
// ----------------------------------------------------------------------
export function HelpPage() {
  const { t } = useTranslation();
  return (
    <section data-testid="help-page">
      <h1>Инструкция</h1>
      <div className="card" data-testid="help-section-navigation"><h3>{t("setupUx.choice")}</h3><p>{t("setupUx.help")}</p></div>
      <div className="card">
        <h3>Быстрый старт</h3>
        <p className="muted">Пройдите мастер настройки по шагам: администратор → MAX → сеть → бот → группа → чаты → пользователи → проверка → готово.</p>
      </div>
      {(['max-network', 'max', 'telegram-network', 'telegram-bot', 'telegram-group', 'max-chats', 'users', 'test'] as WizardStepId[]).map((id) => {
        const h = WIZARD_HELP[id];
        return h ? (
          <div className="card" key={id} data-testid={`help-section-${id}`}>
            <h3>{h.title}</h3>
            {h.body}
          </div>
        ) : null;
      })}
      <div className="card" data-testid="help-section-backups">
        <h3>Резервные копии</h3>
        <p>«Создать локальную копию» сохраняет архив на устройстве; он может быть незашифрованным и содержит мастер-ключ. «Экспортировать для переноса» скачивает один зашифрованный .shmax: задайте пароль и сохраните его отдельно. После создания владельца на новом сервере выберите «Восстановить из резервной копии», загрузите файл, введите пароль, проверьте состав и подтвердите восстановление. Остальные шаги мастера не нужны. Логин и пароль нового владельца сохраняются; старые веб-сеансы отзываются. Остановите исходный мост до запуска восстановленного.</p>
        <p>Неверный пароль или повреждение файла не меняют текущие данные. Если внешняя служба требует вход, повторно авторизуйте только её. Не удаляйте /var/lib/shmax и не отправляйте архив или пароль в поддержку. Фото передаются вложениями; статические стикеры — изображением в MAX и, при допустимом размере, стикером в Telegram. Анимированные/видео используют preview; без preview отправляется текстовый fallback.</p>
      </div>
      <div className="card" data-testid="help-section-updates">
        <h3>Обновления</h3>
        <p>Проверка обновлений идёт по GitHub Releases раз в 24 часа и по кнопке. Установка — только вручную, отдельным действием владельца; при сбое предыдущая версия восстанавливается автоматически.</p>
      </div>
      <div className="card" data-testid="help-section-diagnostics">
        <h3>Диагностика</h3>
        <p>Страница «Диагностика» показывает состояние интеграций при открытии. Обновите страницу для новой проверки. Соединение Telegram можно проверить в его настройках; watchdog автоматически следит за интеграциями.</p>
      </div>
      <div className="card" data-testid="troubleshooting">
        <h3>Частые проблемы</h3>
        {TROUBLESHOOTING.map(([q, a], i) => (
          <details key={i} className="help-card">
            <summary>{q}</summary>
            <div className="help-card-body">{a}</div>
          </details>
        ))}
      </div>
    </section>
  );
}

// ----------------------------------------------------------------------
// About (addendum #5)
// ----------------------------------------------------------------------
export function AboutPage({ version }: { version: string | null }) {
  const [repoUrl, setRepoUrl] = React.useState<string | null>(null);
  const [sourceState, setSourceState] = React.useState<string>('…');
  React.useEffect(() => {
    (async () => {
      try {
        const s = await api<any>('/api/update/status');
        if (s?.available?.artifact_url) {
          const m = String(s.available.artifact_url).match(
            /^https:\/\/github\.com\/[^/]+\/[^/]+/
          );
          if (m) setRepoUrl(m[0]);
        }
        // Live acceptance A13: distinguish the four real states.
        if (s?.status === 'not_configured') setSourceState('источник обновлений не настроен');
        else if (s?.status === 'update_available') setSourceState('настроен, доступно обновление');
        else if (s?.status === 'up_to_date') setSourceState('настроен, установлена последняя версия');
        else if (['timeout','network_error','http_error','rate_limited','bad_manifest','oversized'].includes(s?.status))
          setSourceState('настроен, но последняя проверка не удалась');
        else setSourceState('ещё не проверялся');
      } catch { setSourceState('не удалось получить состояние'); }
    })();
  }, []);
  return (
    <section data-testid="about-page">
      <h1>О SHMAX</h1>
      <div className="card" style={{ textAlign: 'center' }}>
        <img src="/shmax-icon-256.png" alt="Маскот SHMAX" width={128} height={128} style={{ borderRadius: 24 }} data-testid="mascot" />
        <h2 style={{ margin: '0.5rem 0 0' }}>SHMAX</h2>
        <p className="muted" style={{ marginTop: 0 }}>MAX ↔ Telegram</p>
        <p>Самостоятельный шлюз между MAX и Telegram.</p>
      </div>
      <div className="card">
        <div className="kv">
          <span className="k">Версия</span><span className="v" data-testid="about-version">{version ?? '—'}</span>
          <span className="k">Архитектура</span><span className="v">один контейнер, веб + мост в одном процессе</span>
          <span className="k">Постоянные данные</span><span className="v mono">/var/lib/shmax</span>
          <span className="k">Проект</span><span className="v">{repoUrl ? <a href={repoUrl} rel="noreferrer" target="_blank">{repoUrl}</a> : '—'}</span>
          <span className="k">Источник обновлений</span><span className="v" data-testid="about-source-state">{sourceState}</span>
        </div>
      </div>
    </section>
  );
}
