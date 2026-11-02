"use client";

import {
  BarChart3,
  CheckCircle2,
  Clipboard,
  Copy,
  FileSpreadsheet,
  Globe2,
  KeyRound,
  Link2,
  Loader2,
  PlugZap,
  RadioTower,
  RefreshCw,
  RotateCw,
  Send,
  ShieldAlert,
  UploadCloud,
  Webhook,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import {
  createReferralLinkAction,
  createSourceIntegrationAction,
  enqueueHeadHunterImportAction,
  getSourceAnalyticsAction,
  importSourceFileAction,
  listReferralLinksAction,
  listSourceIntegrationsAction,
  listSourceImportRunsAction,
  previewSourcePublicationAction,
  previewSourceFileAction,
  publishSourcePublicationAction,
  revokeReferralLinkAction,
  rotateReferralLinkAction,
  rotateSourceWebhookSecretAction,
  updateSourceIntegrationAction,
} from "@/app/(app)/sourcing/actions";
import type {
  ReferralLink,
  SourceAnalytics,
  SourceCapabilityState,
  SourceImportRun,
  SourceImportFilePreview,
  SourceImportOutcome,
  SourceIntegration,
  SourceJob,
  SourceProvider,
  SourcePublication,
  SourcingLoadFailures,
  SourcingPosition,
} from "@/app/(app)/sourcing/types";
import type { IntegrationConnection } from "@/app/(app)/integrations/types";
import { useI18n } from "@/components/i18n/locale-provider";
import { cn } from "@/lib/utils";

/** @ui-only Sourcing workspace tab state. */
type Tab = "connections" | "import" | "referrals" | "telegram" | "analytics";

function isSourceJob(outcome: SourceImportOutcome): outcome is SourceJob {
  return "operation" in outcome && "attempt_count" in outcome;
}

const COPY = {
  en: {
    title: "Regional sourcing",
    subtitle:
      "Bring candidates into one pipeline, publish vacancies, and measure every source.",
    accessDenied: "Your current permissions do not allow access to sourcing.",
    loadFailed: "Some sourcing data could not be loaded.",
    retry: "Retry",
    connections: "Connections",
    import: "Import",
    referrals: "Referral links",
    telegram: "Publish",
    analytics: "Source analytics",
    addConnection: "Add a source",
    connectionHint:
      "Bind live sources to a canonical provider connection. hh.ru and Telegram also support an explicitly labeled local sandbox.",
    canonicalConnection: "Provider connection",
    chooseCanonical: "Choose a compatible connection",
    localSandbox: "Local sandbox (no external delivery)",
    manageProviders: "Manage provider connections",
    hhArea: "hh.ru area ID",
    sourceMore: "Load more sources",
    hhSync: "hh.ru candidate sync",
    hhVacancyId: "hh.ru vacancy ID",
    hhSyncHint: "Imports official negotiation collections with bounded pagination and encrypted resume checkpoints.",
    sync: "Queue sync",
    jobQueued: "Provider job queued",
    provider: "Provider",
    name: "Connection name",
    mode: "Mode",
    sandbox: "Sandbox",
    live: "Live",
    validatedImport: "Validated import and tracked links",
    create: "Create connection",
    active: "Active",
    disabled: "Disabled",
    ready: "Ready",
    credentialsNotConfigured: "Credentials not configured",
    error: "Error",
    noConnections: "No source connections yet.",
    turnOn: "Enable",
    turnOff: "Disable",
    lastSync: "Last sync",
    never: "Never",
    candidateImport: "Candidate intake",
    fileImport: "File import",
    webhookIntake: "Webhook",
    vacancyPublish: "Vacancy publishing",
    available: "Available",
    unsupported: "Unsupported",
    webhookEndpoint: "Webhook endpoint",
    webhookHelp:
      "Send a versioned envelope signed with HMAC-SHA256. Use a unique event ID and current timestamp; signatures expire after five minutes, and exact retries are processed once.",
    webhookPayload: "Request example",
    rotateSecret: "Rotate secret",
    rotateConfirm:
      "Rotate this webhook secret? The previous secret will stop working immediately.",
    secretTitle: "Copy this secret now",
    secretWarning:
      "It is shown only once and cannot be recovered. Store it in your password manager.",
    copied: "Copied",
    saved: "Changes saved",
    failed: "Action failed",
    uploadTitle: "CSV / XLSX candidate import",
    uploadHint:
      "Preview and map up to 5,000 candidates / 25 MiB. Files above 500 rows or 10 MiB move to the durable worker queue.",
    chooseCsvConnection: "Choose a CSV connection",
    chooseFile: "Choose a UTF-8 CSV or XLSX file",
    upload: "Import candidates",
    previewFile: "Preview and validate",
    previewReady: "Validated import preview",
    previewRows: (rows: number) => `${rows} parsed rows`,
    previewSync: "Runs immediately after confirmation",
    previewDurable: "Runs in the durable worker queue after confirmation",
    previewTruncated: "Showing the first 20 rows",
    mappingJson: "Column mapping (canonical field → file header)",
    queuedImport: "Durable import queued",
    requiredColumns: "Required columns",
    optionalColumns: "Optional columns",
    latestResult: "Latest result",
    requested: "Rows",
    created: "Created",
    duplicates: "Duplicates",
    failedRows: "Failed",
    recentImports: "Recent imports",
    noImports: "No import runs yet.",
    processing: "Processing",
    completed: "Completed",
    partial: "Partial",
    failedStatus: "Failed",
    referralTitle: "Trackable application links",
    referralHint:
      "Create a separate link for each employee, community, or campaign. Applications are attributed automatically.",
    choosePosition: "Choose a vacancy",
    label: "Link label",
    campaign: "Campaign (optional)",
    source: "Source",
    maxApplications: "Application limit (optional)",
    expires: "Expires (default: 30 days, maximum: 90 days)",
    createLink: "Create link",
    newLink: "New referral link — copy it now",
    linkOneTime:
      "For security, the full URL is only returned at creation. Analytics remain available below.",
    clicks: "Clicks",
    applications: "Applications",
    conversion: "Conversion",
    revoke: "Revoke",
    revokeConfirm: "Revoke this referral link?",
    rotateLink: "Rotate",
    rotateLinkConfirm:
      "Rotate this referral link? The previous URL will stop working immediately.",
    revoked: "Revoked",
    noReferrals: "No links for this vacancy yet.",
    linkNotStored: "URL hidden after creation",
    telegramTitle: "Vacancy publication",
    telegramHint:
      "Publish through Telegram or hh.ru, or create a tracked Somon.tj link. Review the exact public copy and application URL first.",
    chooseTelegram: "Choose a publishing source",
    postLanguage: "Post language",
    preview: "Build preview",
    publish: "Publish",
    postPreview: "Publication preview",
    trackingUrl: "Tracked application URL",
    noPreview: "Choose a connection and vacancy to create a preview.",
    publishedSandbox: "Published in sandbox",
    publicationQueued: "Queued for delivery",
    publicationPublished: "Published",
    publicationTracked: "Tracked link created",
    publicationError: "Delivery failed",
    previewStatus: "Preview",
    publicationFailed: "Not published — credentials are not configured",
    analyticsTitle: "Applications by source",
    analyticsHint:
      "Attribution combines imports, referral/UTM applications, and original candidate sources.",
    totalApplications: "Total applications",
    hired: "Hired",
    refresh: "Refresh",
    noAnalytics: "No attributed applications yet.",
    positionClosed: "closed",
    webhookPositionHint:
      "Replace POSITION_UUID and YOUR_SECRET. The executable example requires jq and OpenSSL.",
  },
  ru: {
    title: "Источники кандидатов",
    subtitle:
      "Собирайте кандидатов в одной воронке, публикуйте вакансии и измеряйте каждый источник.",
    accessDenied: "Текущие права не разрешают доступ к источникам кандидатов.",
    loadFailed: "Часть данных об источниках не загрузилась.",
    retry: "Повторить",
    connections: "Подключения",
    import: "Импорт",
    referrals: "Реферальные ссылки",
    telegram: "Публикация",
    analytics: "Аналитика источников",
    addConnection: "Добавить источник",
    connectionHint:
      "Свяжите рабочий источник с каноническим подключением провайдера. Для hh.ru и Telegram также доступен явно отмеченный локальный тестовый режим.",
    canonicalConnection: "Подключение провайдера",
    chooseCanonical: "Выберите совместимое подключение",
    localSandbox: "Локальный тест (без внешней отправки)",
    manageProviders: "Управление подключениями провайдеров",
    hhArea: "ID региона hh.ru",
    sourceMore: "Загрузить ещё источники",
    hhSync: "Синхронизация кандидатов hh.ru",
    hhVacancyId: "ID вакансии hh.ru",
    hhSyncHint: "Импортирует официальные коллекции откликов с ограниченной пагинацией и зашифрованными контрольными точками.",
    sync: "Поставить в очередь",
    jobQueued: "Задание провайдера поставлено в очередь",
    provider: "Провайдер",
    name: "Название подключения",
    mode: "Режим",
    sandbox: "Тестовый",
    live: "Боевой",
    validatedImport: "Проверенный импорт и отслеживаемые ссылки",
    create: "Создать подключение",
    active: "Активно",
    disabled: "Отключено",
    ready: "Готово",
    credentialsNotConfigured: "Учётные данные не настроены",
    error: "Ошибка",
    noConnections: "Подключений источников пока нет.",
    turnOn: "Включить",
    turnOff: "Отключить",
    lastSync: "Последняя синхронизация",
    never: "Никогда",
    candidateImport: "Приём кандидатов",
    fileImport: "Импорт файла",
    webhookIntake: "Вебхук",
    vacancyPublish: "Публикация вакансий",
    available: "Доступно",
    unsupported: "Не поддерживается",
    webhookEndpoint: "Адрес вебхука",
    webhookHelp:
      "Отправляйте версионированный конверт с подписью HMAC-SHA256. Используйте уникальный ID события и текущее время; подпись действует пять минут, а точный повтор обрабатывается один раз.",
    webhookPayload: "Пример запроса",
    rotateSecret: "Сменить секрет",
    rotateConfirm:
      "Сменить секрет вебхука? Предыдущий секрет сразу перестанет работать.",
    secretTitle: "Скопируйте секрет сейчас",
    secretWarning:
      "Он показывается один раз и не восстанавливается. Сохраните его в менеджере паролей.",
    copied: "Скопировано",
    saved: "Изменения сохранены",
    failed: "Не удалось выполнить действие",
    uploadTitle: "Импорт кандидатов из CSV / XLSX",
    uploadHint:
      "Проверьте и сопоставьте до 5 000 кандидатов / 25 МиБ. Файлы больше 500 строк или 10 МиБ обрабатываются надёжной фоновой очередью.",
    chooseCsvConnection: "Выберите CSV-подключение",
    chooseFile: "Выберите CSV в UTF-8 или файл XLSX",
    upload: "Импортировать кандидатов",
    previewFile: "Проверить и показать",
    previewReady: "Проверенный предпросмотр импорта",
    previewRows: (rows: number) => `Распознано строк: ${rows}`,
    previewSync: "Будет выполнено сразу после подтверждения",
    previewDurable: "Будет выполнено надёжной фоновой очередью после подтверждения",
    previewTruncated: "Показаны первые 20 строк",
    mappingJson: "Сопоставление столбцов (каноническое поле → заголовок файла)",
    queuedImport: "Фоновый импорт поставлен в очередь",
    requiredColumns: "Обязательные столбцы",
    optionalColumns: "Необязательные столбцы",
    latestResult: "Последний результат",
    requested: "Строк",
    created: "Создано",
    duplicates: "Дубликатов",
    failedRows: "Ошибок",
    recentImports: "История импорта",
    noImports: "Импортов пока не было.",
    processing: "Обрабатывается",
    completed: "Завершён",
    partial: "Частично",
    failedStatus: "Ошибка",
    referralTitle: "Отслеживаемые ссылки для отклика",
    referralHint:
      "Создавайте отдельную ссылку для сотрудника, сообщества или кампании. Отклики атрибутируются автоматически.",
    choosePosition: "Выберите вакансию",
    label: "Название ссылки",
    campaign: "Кампания (необязательно)",
    source: "Источник",
    maxApplications: "Лимит откликов (необязательно)",
    expires: "Срок действия (по умолчанию 30 дней, максимум 90)",
    createLink: "Создать ссылку",
    newLink: "Новая реферальная ссылка — скопируйте сейчас",
    linkOneTime:
      "Полный URL возвращается только при создании. Аналитика по ссылке останется доступной ниже.",
    clicks: "Переходы",
    applications: "Отклики",
    conversion: "Конверсия",
    revoke: "Отозвать",
    revokeConfirm: "Отозвать эту реферальную ссылку?",
    rotateLink: "Сменить ссылку",
    rotateLinkConfirm:
      "Сменить эту реферальную ссылку? Предыдущий адрес сразу перестанет работать.",
    revoked: "Отозвана",
    noReferrals: "Для этой вакансии ссылок пока нет.",
    linkNotStored: "URL скрыт после создания",
    telegramTitle: "Публикация вакансии",
    telegramHint:
      "Опубликуйте вакансию через Telegram или hh.ru либо создайте отслеживаемую ссылку Somon.tj. Сначала проверьте публичный текст и ссылку.",
    chooseTelegram: "Выберите источник публикации",
    postLanguage: "Язык публикации",
    preview: "Создать предпросмотр",
    publish: "Опубликовать",
    postPreview: "Предпросмотр публикации",
    trackingUrl: "Отслеживаемая ссылка на отклик",
    noPreview: "Выберите подключение и вакансию, чтобы создать предпросмотр.",
    publishedSandbox: "Опубликовано в тестовом режиме",
    publicationQueued: "Поставлено в очередь",
    publicationPublished: "Опубликовано",
    publicationTracked: "Отслеживаемая ссылка создана",
    publicationError: "Ошибка отправки",
    previewStatus: "Предпросмотр",
    publicationFailed: "Не опубликовано — учётные данные не настроены",
    analyticsTitle: "Отклики по источникам",
    analyticsHint:
      "Атрибуция объединяет импорт, реферальные/UTM-отклики и исходные источники кандидатов.",
    totalApplications: "Всего откликов",
    hired: "Нанято",
    refresh: "Обновить",
    noAnalytics: "Атрибутированных откликов пока нет.",
    positionClosed: "закрыта",
    webhookPositionHint:
      "Замените POSITION_UUID и YOUR_SECRET. Для исполняемого примера нужны jq и OpenSSL.",
  },
};

/** @ui-only Localized sourcing presentation copy. */
type CopyText = (typeof COPY)["en"];

const PROVIDER_LABELS: Record<SourceProvider, string> = {
  hh: "hh.ru",
  somon: "Somon.tj",
  telegram: "Telegram",
  csv: "CSV / XLSX",
  generic_webhook: "Generic webhook",
};

const TABS: { id: Tab; icon: typeof PlugZap }[] = [
  { id: "connections", icon: PlugZap },
  { id: "import", icon: FileSpreadsheet },
  { id: "referrals", icon: Link2 },
  { id: "telegram", icon: RadioTower },
  { id: "analytics", icon: BarChart3 },
];

/** @ui-only Sourcing workspace composition properties. */
export type SourcingWorkspaceProps = {
  access: SourcingAccess;
  initialIntegrations: SourceIntegration[];
  sourceIntegrationCursor: string | null;
  providerConnections: IntegrationConnection[];
  initialRuns: SourceImportRun[];
  positions: SourcingPosition[];
  initialAnalytics: SourceAnalytics;
  initialPositionId: string;
  initialReferrals: ReferralLink[];
  loadFailures: SourcingLoadFailures;
  publicApiBaseUrl: string;
};

/** @ui-only Permission-derived sourcing controls. */
export type SourcingAccess = {
  viewConnections: boolean;
  manageConnections: boolean;
  rotateCredentials: boolean;
  viewRuns: boolean;
  importCandidates: boolean;
  viewReferrals: boolean;
  manageReferrals: boolean;
  publish: boolean;
  viewAnalytics: boolean;
};

export function SourcingWorkspace({
  access,
  initialIntegrations,
  sourceIntegrationCursor: initialSourceCursor,
  providerConnections,
  initialRuns,
  positions,
  initialAnalytics,
  initialPositionId,
  initialReferrals,
  loadFailures,
  publicApiBaseUrl,
}: SourcingWorkspaceProps) {
  const { locale } = useI18n();
  const t = COPY[locale];
  const availableTabs = TABS.filter(({ id }) => {
    if (id === "connections") return access.viewConnections || access.manageConnections;
    if (id === "import") return access.viewRuns;
    if (id === "referrals") return access.viewReferrals || access.manageReferrals;
    if (id === "telegram") return access.publish;
    return access.viewAnalytics;
  });
  const [tab, setTab] = useState<Tab>(availableTabs[0]?.id ?? "connections");
  const [integrations, setIntegrations] = useState(initialIntegrations);
  const [sourceCursor, setSourceCursor] = useState(initialSourceCursor);
  const [runs, setRuns] = useState(initialRuns);
  const [analytics, setAnalytics] = useState(initialAnalytics);
  const [positionId, setPositionId] = useState(initialPositionId);
  const [referrals, setReferrals] = useState(initialReferrals);
  const [importRun, setImportRun] = useState<SourceImportRun | null>(null);
  const [importPreview, setImportPreview] =
    useState<SourceImportFilePreview | null>(null);
  const [importJob, setImportJob] = useState<SourceJob | null>(null);
  const [publication, setPublication] = useState<SourcePublication | null>(null);
  const [sourceJob, setSourceJob] = useState<SourceJob | null>(null);
  const [newReferralUrl, setNewReferralUrl] = useState<string | null>(null);
  const [oneTimeSecret, setOneTimeSecret] = useState<{
    connection: string;
    value: string;
  } | null>(null);
  const [pending, startTransition] = useTransition();
  const failedCount = Object.values(loadFailures).filter(Boolean).length;
  const activeTab = availableTabs.some((item) => item.id === tab)
    ? tab
    : availableTabs[0]?.id;

  function fail(error?: string) {
    toast.error(error || t.failed);
  }

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(t.copied);
    } catch {
      fail();
    }
  }

  function createConnection(body: {
    provider: SourceProvider;
    name: string;
    connection_id: string | null;
    configuration: Record<string, unknown>;
  }) {
    if (!access.manageConnections) return;
    startTransition(async () => {
      const result = await createSourceIntegrationAction(body);
      if (!result.ok) return fail(result.error);
      setIntegrations((current) =>
        [...current, result.data.integration].sort((a, b) =>
          `${a.provider}:${a.name}`.localeCompare(`${b.provider}:${b.name}`),
        ),
      );
      if (result.data.webhook_secret) {
        setOneTimeSecret({
          connection: result.data.integration.name,
          value: result.data.webhook_secret,
        });
      }
      toast.success(t.saved);
    });
  }

  function loadMoreSources() {
    if (!sourceCursor || !access.viewConnections) return;
    startTransition(async () => {
      const result = await listSourceIntegrationsAction(sourceCursor);
      if (!result.ok) return fail(result.error);
      setIntegrations((current) => [
        ...current,
        ...result.data.items.filter(
          (item) => !current.some((existing) => existing.id === item.id),
        ),
      ]);
      setSourceCursor(result.data.next_cursor);
    });
  }

  function toggleConnection(integration: SourceIntegration) {
    if (!access.manageConnections) return;
    startTransition(async () => {
      const result = await updateSourceIntegrationAction(integration.id, {
        is_active: !integration.is_active,
      });
      if (!result.ok) return fail(result.error);
      setIntegrations((current) =>
        current.map((item) => (item.id === result.data.id ? result.data : item)),
      );
      toast.success(t.saved);
    });
  }

  function rotateSecret(integration: SourceIntegration) {
    if (!access.rotateCredentials) return;
    if (!window.confirm(t.rotateConfirm)) return;
    startTransition(async () => {
      const result = await rotateSourceWebhookSecretAction(integration.id);
      if (!result.ok) return fail(result.error);
      setOneTimeSecret({
        connection: integration.name,
        value: result.data.webhook_secret,
      });
      toast.success(t.saved);
    });
  }

  function importFile(integrationId: string, data: FormData) {
    if (!access.importCandidates) return;
    startTransition(async () => {
      const result = await importSourceFileAction(integrationId, data);
      if (!result.ok) return fail(result.error);
      if (isSourceJob(result.data)) {
        setImportJob(result.data);
        setImportPreview(null);
        toast.success(t.queuedImport);
        return;
      }
      const completedRun = result.data;
      setImportRun(completedRun);
      setImportPreview(null);
      setRuns((current) => [
        completedRun,
        ...current.filter((run) => run.id !== completedRun.id),
      ]);
      toast.success(t.saved);
    });
  }

  function previewFile(integrationId: string, data: FormData) {
    if (!access.importCandidates) return;
    startTransition(async () => {
      const result = await previewSourceFileAction(integrationId, data);
      if (!result.ok) return fail(result.error);
      setImportPreview(result.data);
      setImportJob(null);
      toast.success(t.previewReady);
    });
  }

  function refreshRuns() {
    if (!access.viewRuns) return;
    startTransition(async () => {
      const result = await listSourceImportRunsAction();
      if (!result.ok) return fail(result.error);
      setRuns(result.data.items);
      toast.success(t.saved);
    });
  }

  function syncHeadHunter(
    integrationId: string,
    selectedPositionId: string,
    vacancyId: string,
  ) {
    if (!access.importCandidates) return;
    startTransition(async () => {
      const result = await enqueueHeadHunterImportAction(integrationId, {
        position_id: selectedPositionId,
        vacancy_id: vacancyId,
        collection_ids: [],
        max_pages: 10,
        max_records: 500,
        resume_from_cursor: true,
        idempotency_key: crypto.randomUUID(),
      });
      if (!result.ok) return fail(result.error);
      setSourceJob(result.data);
      toast.success(t.jobQueued);
    });
  }

  function chooseReferralPosition(nextPositionId: string) {
    setPositionId(nextPositionId);
    setNewReferralUrl(null);
    if (!nextPositionId) {
      setReferrals([]);
      return;
    }
    if (!access.viewReferrals) return;
    startTransition(async () => {
      const result = await listReferralLinksAction(nextPositionId);
      if (!result.ok) return fail(result.error);
      setReferrals(result.data);
    });
  }

  function createReferral(body: {
    label: string;
    campaign: string | null;
    source: string;
    expires_at: string | null;
    max_applications: number | null;
  }) {
    if (!access.manageReferrals) return;
    if (!positionId) return fail(t.choosePosition);
    startTransition(async () => {
      const result = await createReferralLinkAction(positionId, body);
      if (!result.ok) return fail(result.error);
      setReferrals((current) => [result.data.link, ...current]);
      setNewReferralUrl(result.data.link.url);
      toast.success(t.saved);
    });
  }

  function revokeReferral(link: ReferralLink) {
    if (!access.manageReferrals) return;
    if (!window.confirm(t.revokeConfirm)) return;
    startTransition(async () => {
      const result = await revokeReferralLinkAction(link.id);
      if (!result.ok) return fail(result.error);
      setReferrals((current) =>
        current.map((item) => (item.id === result.data.id ? result.data : item)),
      );
      toast.success(t.saved);
    });
  }

  function rotateReferral(link: ReferralLink) {
    if (!access.manageReferrals) return;
    if (!window.confirm(t.rotateLinkConfirm)) return;
    startTransition(async () => {
      const result = await rotateReferralLinkAction(link.id);
      if (!result.ok) return fail(result.error);
      setReferrals((current) =>
        current.map((item) =>
          item.id === result.data.link.id ? result.data.link : item,
        ),
      );
      setNewReferralUrl(result.data.link.url);
      toast.success(t.saved);
    });
  }

  function publicationAction(
    kind: "preview" | "publish",
    integrationId: string,
    selectedPositionId: string,
    postLocale: "ru" | "en",
  ) {
    if (!access.publish) return;
    startTransition(async () => {
      const action =
        kind === "preview"
          ? previewSourcePublicationAction
          : publishSourcePublicationAction;
      const result = await action(integrationId, selectedPositionId, postLocale);
      if (!result.ok) return fail(result.error);
      setPublication(result.data);
      if (kind === "publish" && result.data.status === "credentials_not_configured") {
        toast.error(t.publicationFailed);
      } else {
        toast.success(kind === "preview" ? t.previewStatus : t.saved);
      }
    });
  }

  function refreshAnalytics() {
    if (!access.viewAnalytics) return;
    startTransition(async () => {
      const result = await getSourceAnalyticsAction();
      if (!result.ok) return fail(result.error);
      setAnalytics(result.data);
      toast.success(t.saved);
    });
  }

  if (!activeTab) {
    return (
      <div className="mx-auto max-w-4xl">
        <header className="mb-6">
          <h1 className="text-[24px] font-semibold tracking-[-0.025em]">{t.title}</h1>
          <p className="mt-1 text-sm text-text-muted">{t.subtitle}</p>
        </header>
        <div className="flex items-start gap-3 rounded-xl border border-amber-300/60 bg-amber-50 p-5 text-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
          <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
          <p className="text-sm">{t.accessDenied}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-6">
        <h1 className="text-[24px] font-semibold tracking-[-0.025em]">{t.title}</h1>
        <p className="mt-1 text-sm text-text-muted">{t.subtitle}</p>
      </header>

      {failedCount > 0 ? (
        <div className="mb-5 flex items-center gap-3 rounded-lg border border-amber-300/60 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
          <ShieldAlert className="h-4 w-4 shrink-0" />
          <span className="min-w-0 flex-1">{t.loadFailed}</span>
          <button
            onClick={() => window.location.reload()}
            className="inline-flex min-h-6 min-w-6 items-center justify-center px-1 font-medium underline"
          >
            {t.retry}
          </button>
        </div>
      ) : null}

      {oneTimeSecret ? (
        <OneTimeValue
          title={`${t.secretTitle} · ${oneTimeSecret.connection}`}
          hint={t.secretWarning}
          value={oneTimeSecret.value}
          onCopy={copy}
          onClose={() => setOneTimeSecret(null)}
        />
      ) : null}

      <div className="mb-6 flex gap-1 overflow-x-auto border-b border-border">
        {availableTabs.map(({ id, icon: Icon }) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={cn(
              "-mb-px flex shrink-0 items-center gap-1.5 border-b px-4 py-3 text-xs font-medium",
              activeTab === id
                ? "border-text text-text"
                : "border-transparent text-text-muted hover:text-text",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {t[id]}
          </button>
        ))}
      </div>

      {activeTab === "connections" ? (
        <ConnectionsPanel
          canView={access.viewConnections}
          canManage={access.manageConnections}
          canRotate={access.rotateCredentials}
          integrations={integrations}
          providerConnections={providerConnections}
          hasMore={Boolean(sourceCursor)}
          publicApiBaseUrl={publicApiBaseUrl}
          pending={pending}
          t={t}
          locale={locale}
          onCreate={createConnection}
          onToggle={toggleConnection}
          onRotate={rotateSecret}
          onCopy={copy}
          onLoadMore={loadMoreSources}
        />
      ) : null}
      {activeTab === "import" ? (
        <ImportPanel
          canImport={access.importCandidates}
          integrations={integrations}
          positions={positions}
          sourceJob={sourceJob}
          importJob={importJob}
          importPreview={importPreview}
          runs={runs}
          latest={importRun}
          pending={pending}
          t={t}
          locale={locale}
          onImport={importFile}
          onPreview={previewFile}
          onPreviewClear={() => setImportPreview(null)}
          onRefresh={refreshRuns}
          onSyncHeadHunter={syncHeadHunter}
        />
      ) : null}
      {activeTab === "referrals" ? (
        <ReferralsPanel
          canView={access.viewReferrals}
          canManage={access.manageReferrals}
          positions={positions}
          positionId={positionId}
          referrals={referrals}
          newUrl={newReferralUrl}
          pending={pending}
          t={t}
          locale={locale}
          onPosition={chooseReferralPosition}
          onCreate={createReferral}
          onRotate={rotateReferral}
          onRevoke={revokeReferral}
          onCopy={copy}
          onDismissUrl={() => setNewReferralUrl(null)}
        />
      ) : null}
      {activeTab === "telegram" ? (
        <TelegramPanel
          integrations={integrations}
          positions={positions}
          publication={publication}
          pending={pending}
          t={t}
          locale={locale}
          onAction={publicationAction}
          onCopy={copy}
        />
      ) : null}
      {activeTab === "analytics" ? (
        <AnalyticsPanel
          analytics={analytics}
          pending={pending}
          t={t}
          onRefresh={refreshAnalytics}
        />
      ) : null}
    </div>
  );
}

function ConnectionsPanel({
  canView,
  canManage,
  canRotate,
  integrations,
  providerConnections,
  hasMore,
  publicApiBaseUrl,
  pending,
  t,
  locale,
  onCreate,
  onToggle,
  onRotate,
  onCopy,
  onLoadMore,
}: {
  canView: boolean;
  canManage: boolean;
  canRotate: boolean;
  integrations: SourceIntegration[];
  providerConnections: IntegrationConnection[];
  hasMore: boolean;
  publicApiBaseUrl: string;
  pending: boolean;
  t: CopyText;
  locale: "ru" | "en";
  onCreate: (body: {
    provider: SourceProvider;
    name: string;
    connection_id: string | null;
    configuration: Record<string, unknown>;
  }) => void;
  onToggle: (integration: SourceIntegration) => void;
  onRotate: (integration: SourceIntegration) => void;
  onCopy: (value: string) => void;
  onLoadMore: () => void;
}) {
  const [provider, setProvider] = useState<SourceProvider>("csv");
  const [connectionId, setConnectionId] = useState("");
  const providerMap: Record<SourceProvider, string> = {
    hh: "hh_ru",
    somon: "somon",
    telegram: "telegram",
    csv: "csv_xlsx",
    generic_webhook: "generic_webhook",
  };
  const requiredCapabilities: Record<
    SourceProvider,
    IntegrationConnection["capabilities"]
  > = {
    hh: ["candidate.import", "vacancy.publish"],
    somon: ["file.import", "tracked_link.create"],
    telegram: ["vacancy.publish"],
    csv: ["file.import"],
    generic_webhook: ["webhook.receive"],
  };
  const compatible = providerConnections.filter(
    (item) =>
      item.provider === providerMap[provider] &&
      item.status === "connected" &&
      requiredCapabilities[provider].every((capability) =>
        item.capabilities.includes(capability),
      ),
  );
  const sandbox = connectionId === "__sandbox__";
  return (
    <div className={cn("grid gap-5", canManage && canView && "lg:grid-cols-[330px_1fr]")}>
      {canManage ? <section className="h-fit rounded-xl border border-border bg-bg-elevated p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <PlugZap className="h-4 w-4" />
          {t.addConnection}
        </h2>
        <p className="mt-1.5 text-xs leading-5 text-text-muted">{t.connectionHint}</p>
        <form
          className="mt-4 space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            onCreate({
              provider,
              name: String(form.get("name") || ""),
              connection_id: sandbox ? null : connectionId,
              configuration: sandbox
                ? { mode: "sandbox" }
                : provider === "hh"
                  ? { area_id: String(form.get("area_id") || "") }
                  : {},
            });
            event.currentTarget.reset();
          }}
        >
          <label className="block text-xs text-text-muted">
            {t.provider}
            <select
              value={provider}
              onChange={(event) => {
                setProvider(event.target.value as SourceProvider);
                setConnectionId("");
              }}
              className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text"
            >
              {(Object.keys(PROVIDER_LABELS) as SourceProvider[]).map((item) => (
                <option key={item} value={item}>
                  {PROVIDER_LABELS[item]}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-xs text-text-muted">
            {t.canonicalConnection}
            <select
              required
              value={connectionId}
              onChange={(event) => setConnectionId(event.target.value)}
              className="mt-1 min-h-11 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text"
            >
              <option value="">{t.chooseCanonical}</option>
              {(provider === "hh" || provider === "telegram") ? (
                <option value="__sandbox__">{t.localSandbox}</option>
              ) : null}
              {compatible.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} · {item.status}
                </option>
              ))}
            </select>
          </label>
          <Link
            href="/integrations"
            className="inline-flex min-h-11 items-center text-xs font-medium text-accent hover:underline"
          >
            {t.manageProviders}
          </Link>
          <label className="block text-xs text-text-muted">
            {t.name}
            <input
              name="name"
              required
              maxLength={160}
              className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text"
            />
          </label>
          {provider === "hh" && !sandbox ? (
            <label className="block text-xs text-text-muted">
              {t.hhArea}
              <input
                name="area_id"
                required
                maxLength={32}
                className="mt-1 min-h-11 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text"
              />
            </label>
          ) : null}
          {sandbox ? (
            <p className="rounded-md border border-blue-300/60 bg-blue-50 px-3 py-2 text-xs text-blue-900 dark:bg-blue-950/20 dark:text-blue-200">
              {t.mode}: {t.localSandbox}
            </p>
          ) : provider === "somon" ? (
            <p className="rounded-md border border-emerald-300/60 bg-emerald-50 px-3 py-2 text-xs text-emerald-900 dark:bg-emerald-950/20 dark:text-emerald-200">
              {t.mode}: {t.validatedImport}
            </p>
          ) : null}
          <button
            disabled={pending || !connectionId}
            className="flex w-full items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
          >
            {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlugZap className="h-3.5 w-3.5" />}
            {t.create}
          </button>
        </form>
      </section> : null}

      {canView ? <div className="space-y-4">
        {integrations.length === 0 ? (
          <EmptyState icon={Globe2} text={t.noConnections} />
        ) : (
          integrations.map((integration) => (
            <IntegrationCard
              key={integration.id}
              integration={integration}
              publicApiBaseUrl={publicApiBaseUrl}
              pending={pending}
              canManage={canManage}
              canRotate={canRotate}
              t={t}
              locale={locale}
              onToggle={onToggle}
              onRotate={onRotate}
              onCopy={onCopy}
            />
          ))
        )}
        {hasMore ? (
          <button
            type="button"
            disabled={pending}
            onClick={onLoadMore}
            className="mx-auto flex min-h-11 items-center gap-2 rounded-lg border border-border px-4 text-xs font-medium disabled:opacity-50"
          >
            {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {t.sourceMore}
          </button>
        ) : null}
      </div> : null}
    </div>
  );
}

function IntegrationCard({
  integration,
  publicApiBaseUrl,
  pending,
  canManage,
  canRotate,
  t,
  locale,
  onToggle,
  onRotate,
  onCopy,
}: {
  integration: SourceIntegration;
  publicApiBaseUrl: string;
  pending: boolean;
  canManage: boolean;
  canRotate: boolean;
  t: CopyText;
  locale: "ru" | "en";
  onToggle: (integration: SourceIntegration) => void;
  onRotate: (integration: SourceIntegration) => void;
  onCopy: (value: string) => void;
}) {
  const webhookUrl = `${publicApiBaseUrl}/source-webhooks/${integration.id}`;
  const candidatePayload = JSON.stringify(
    {
      external_id: "ats-12345",
      position_id: "POSITION_UUID",
      name: locale === "en" ? "Candidate Name" : "Имя кандидата",
      phone: "+992900000000",
      email: "candidate@example.com",
      cv_text: locale === "en" ? "Candidate CV text" : "Текст резюме кандидата",
      source_url: "https://example.com/profile/12345",
      utm_campaign: "summer-hiring",
    },
  );
  return (
    <section className="rounded-xl border border-border bg-bg-elevated p-5">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-border bg-bg">
          {integration.provider === "generic_webhook" ? (
            <Webhook className="h-4 w-4" />
          ) : integration.provider === "telegram" ? (
            <RadioTower className="h-4 w-4" />
          ) : (
            <Globe2 className="h-4 w-4" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-sm font-semibold">{integration.name}</h3>
            <StatusBadge status={integration.status} t={t} />
          </div>
          <p className="mt-1 text-xs text-text-muted">
            {PROVIDER_LABELS[integration.provider]}
            {typeof integration.configuration.mode === "string"
              ? ` · ${integration.configuration.mode === "sandbox" ? t.sandbox : t.live}`
              : ""}
            {integration.connection_status
              ? ` · ${integration.connection_provider}: ${integration.connection_status}`
              : ""}
            {` · ${t.lastSync}: ${formatDate(integration.last_sync_at, locale, t.never)}`}
          </p>
        </div>
        {canManage ? <button
          type="button"
          disabled={pending}
          onClick={() => onToggle(integration)}
          className="rounded-md border border-border px-2.5 py-1.5 text-xs font-medium disabled:opacity-50"
        >
          {integration.is_active ? t.turnOff : t.turnOn}
        </button> : null}
      </div>

      <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Capability label={t.candidateImport} state={integration.capabilities.candidate_import} t={t} />
        <Capability label={t.fileImport} state={integration.capabilities.file_import} t={t} />
        <Capability label={t.webhookIntake} state={integration.capabilities.webhook_intake} t={t} />
        <Capability label={t.vacancyPublish} state={integration.capabilities.vacancy_publish} t={t} />
      </div>

      {integration.provider === "generic_webhook" ? (
        <div className="mt-4 rounded-lg border border-border bg-bg p-4">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="min-w-0 flex-1 text-xs font-semibold">{t.webhookEndpoint}</h4>
            <button
              type="button"
              onClick={() => onCopy(webhookUrl)}
              className="flex items-center gap-1 text-xs text-accent"
            >
              <Copy className="h-3.5 w-3.5" />
              {t.copied.replace("Copied", "Copy").replace("Скопировано", "Копировать")}
            </button>
            {canRotate ? <button
              type="button"
              disabled={pending}
              onClick={() => onRotate(integration)}
              className="flex items-center gap-1 text-xs text-text-muted hover:text-text disabled:opacity-50"
            >
              <RotateCw className="h-3.5 w-3.5" />
              {t.rotateSecret}
            </button> : null}
          </div>
          <code className="mt-2 block break-all rounded border border-border px-2.5 py-2 text-[11px] text-text-muted">
            {webhookUrl}
          </code>
          <p className="mt-3 text-xs leading-5 text-text-muted">{t.webhookHelp}</p>
          <details className="mt-3">
            <summary className="cursor-pointer text-xs font-medium">{t.webhookPayload}</summary>
            <pre className="mt-2 overflow-x-auto rounded-md bg-background p-3 text-[11px] leading-5 text-text-muted">
              {`WEBHOOK_SECRET='YOUR_SECRET'
TIMESTAMP="$(date +%s)"
BODY="$(jq -cn --arg id "evt-$TIMESTAMP-ats-12345" --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson data '${candidatePayload}' '{version:"2026-08-01",id:$id,type:"source.candidate.created",created_at:$created_at,data:$data}')"
SIGNATURE="v1=$(printf '%s.%s' "$TIMESTAMP" "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" -hex | awk '{print $2}')"
curl -X POST '${webhookUrl}' -H 'Content-Type: application/json' -H "X-Webhook-Secret: $WEBHOOK_SECRET" -H "X-HR-Webhook-Timestamp: $TIMESTAMP" -H "X-HR-Webhook-Signature: $SIGNATURE" --data-binary "$BODY"`}
            </pre>
            <p className="mt-2 text-[11px] text-text-muted">{t.webhookPositionHint}</p>
          </details>
        </div>
      ) : null}
    </section>
  );
}

function Capability({
  label,
  state,
  t,
}: {
  label: string;
  state: SourceCapabilityState;
  t: CopyText;
}) {
  const labelByState: Record<SourceCapabilityState, string> = {
    available: t.available,
    sandbox: t.sandbox,
    credentials_not_configured: t.credentialsNotConfigured,
    unsupported: t.unsupported,
  };
  return (
    <div className="rounded-md border border-border bg-bg px-3 py-2">
      <p className="text-[10px] uppercase tracking-wide text-text-muted">{label}</p>
      <p
        className={cn(
          "mt-1 text-xs font-medium",
          state === "available" && "text-emerald-600",
          state === "sandbox" && "text-blue-600",
          state === "credentials_not_configured" && "text-amber-600",
          state === "unsupported" && "text-text-muted",
        )}
      >
        {labelByState[state]}
      </p>
    </div>
  );
}

function ImportPanel({
  canImport,
  integrations,
  positions,
  sourceJob,
  importJob,
  importPreview,
  runs,
  latest,
  pending,
  t,
  locale,
  onImport,
  onPreview,
  onPreviewClear,
  onRefresh,
  onSyncHeadHunter,
}: {
  canImport: boolean;
  integrations: SourceIntegration[];
  positions: SourcingPosition[];
  sourceJob: SourceJob | null;
  importJob: SourceJob | null;
  importPreview: SourceImportFilePreview | null;
  runs: SourceImportRun[];
  latest: SourceImportRun | null;
  pending: boolean;
  t: CopyText;
  locale: "ru" | "en";
  onImport: (integrationId: string, data: FormData) => void;
  onPreview: (integrationId: string, data: FormData) => void;
  onPreviewClear: () => void;
  onRefresh: () => void;
  onSyncHeadHunter: (
    integrationId: string,
    positionId: string,
    vacancyId: string,
  ) => void;
}) {
  const available = integrations.filter(
    (item) => item.is_active && item.capabilities.file_import === "available",
  );
  const [integrationId, setIntegrationId] = useState(available[0]?.id ?? "");
  const hhConnections = integrations.filter(
    (item) =>
      item.provider === "hh" &&
      item.is_active &&
      item.capabilities.candidate_import === "available",
  );
  const [hhIntegrationId, setHhIntegrationId] = useState(
    hhConnections[0]?.id ?? "",
  );
  const [hhPositionId, setHhPositionId] = useState(positions[0]?.id ?? "");
  const [hhVacancyId, setHhVacancyId] = useState("");
  const importForm = useRef<HTMLFormElement>(null);
  const importIdempotencyKey = useRef<string | null>(null);

  function importFormData(): FormData | null {
    if (!importForm.current) return null;
    const data = new FormData(importForm.current);
    importIdempotencyKey.current ??= `source-import-${crypto.randomUUID()}`;
    data.set("idempotency_key", importIdempotencyKey.current);
    return data;
  }

  function invalidatePreview() {
    importIdempotencyKey.current = null;
    onPreviewClear();
  }
  return (
    <div className={cn("grid gap-5", canImport && "lg:grid-cols-[420px_1fr]")}>
      {canImport ? <section className="h-fit rounded-xl border border-border bg-bg-elevated p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <UploadCloud className="h-4 w-4" />
          {t.uploadTitle}
        </h2>
        <p className="mt-1.5 text-xs leading-5 text-text-muted">{t.uploadHint}</p>
        <form
          ref={importForm}
          className="mt-4 space-y-3"
          onSubmit={(event) => event.preventDefault()}
          onChange={invalidatePreview}
        >
          <select
            required
            value={integrationId}
            onChange={(event) => setIntegrationId(event.target.value)}
            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
          >
            <option value="">{t.chooseCsvConnection}</option>
            {available.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
          <label className="block rounded-lg border border-dashed border-border bg-bg p-4 text-center text-xs text-text-muted">
            <FileSpreadsheet className="mx-auto mb-2 h-6 w-6" />
            {t.chooseFile}
            <input
              name="file"
              type="file"
              required
              accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              className="mt-3 block w-full text-xs"
            />
          </label>
          <details className="rounded-lg border border-border bg-bg p-3 text-xs">
            <summary className="cursor-pointer font-medium">{t.mappingJson}</summary>
            <textarea
              name="mapping"
              rows={7}
              spellCheck={false}
              defaultValue={JSON.stringify(
                {
                  external_id: "external_id",
                  position_id: "position_id",
                  name: "name",
                  phone: "phone",
                  email: "email",
                  cv_text: "cv_text",
                  source_url: "source_url",
                  utm_campaign: "utm_campaign",
                },
                null,
                2,
              )}
              className="mt-3 w-full resize-y rounded-md border border-border bg-bg-elevated p-2 font-mono text-[11px] leading-5"
            />
          </details>
          <div className="grid gap-2 sm:grid-cols-2">
            <button
              type="button"
              disabled={pending || !integrationId}
              onClick={() => {
                const data = importFormData();
                if (data) onPreview(integrationId, data);
              }}
              className="flex w-full items-center justify-center gap-2 rounded-md border border-border bg-bg px-3 py-2 text-xs font-semibold disabled:opacity-50"
            >
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileSpreadsheet className="h-3.5 w-3.5" />}
              {t.previewFile}
            </button>
            <button
              type="button"
              disabled={pending || !integrationId || !importPreview}
              onClick={() => {
                const data = importFormData();
                if (data) onImport(integrationId, data);
              }}
              className="flex w-full items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
            >
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <UploadCloud className="h-3.5 w-3.5" />}
              {t.upload}
            </button>
          </div>
        </form>
        <div className="mt-4 rounded-lg border border-border bg-bg p-3 text-xs">
          <p>
            <span className="font-medium">{t.requiredColumns}:</span>{" "}
            <code>external_id, position_id, name, cv_text</code>
          </p>
          <p className="mt-2 text-text-muted">
            <span className="font-medium text-text">{t.optionalColumns}:</span>{" "}
            <code>phone, email, source_url, utm_campaign</code>
          </p>
        </div>
        {importPreview ? (
          <div className="mt-4 rounded-lg border border-emerald-300/60 bg-emerald-50/60 p-3 text-xs dark:bg-emerald-950/20">
            <div className="flex flex-wrap items-center gap-2">
              <CheckCircle2 className="h-4 w-4 text-emerald-600" />
              <strong>{t.previewReady}</strong>
              <span className="text-text-muted">{t.previewRows(importPreview.row_count)}</span>
            </div>
            <p className="mt-2 text-text-muted">
              {importPreview.import_mode === "durable_job"
                ? t.previewDurable
                : t.previewSync}
              {importPreview.truncated ? ` · ${t.previewTruncated}` : ""}
            </p>
            <div className="mt-3 max-h-52 overflow-auto rounded-md border border-border bg-bg">
              <table className="w-full min-w-[620px] text-left text-[10.5px]">
                <thead className="sticky top-0 bg-bg-elevated text-text-muted">
                  <tr>
                    <th className="px-2 py-1.5">#</th>
                    <th className="px-2 py-1.5">external_id</th>
                    <th className="px-2 py-1.5">name</th>
                    <th className="px-2 py-1.5">email</th>
                    <th className="px-2 py-1.5">phone</th>
                    <th className="px-2 py-1.5">CV</th>
                  </tr>
                </thead>
                <tbody>
                  {importPreview.items.map((item) => (
                    <tr key={`${item.row}:${item.external_id}`} className="border-t border-border">
                      <td className="px-2 py-1.5">{item.row}</td>
                      <td className="px-2 py-1.5 font-mono">{item.external_id}</td>
                      <td className="px-2 py-1.5">{item.name}</td>
                      <td className="px-2 py-1.5">{item.masked_email ?? "—"}</td>
                      <td className="px-2 py-1.5">{item.masked_phone ?? "—"}</td>
                      <td className="px-2 py-1.5">{item.cv_characters}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}
        {importJob ? (
          <Link href="/integrations" className="mt-4 block rounded-md border border-border bg-bg p-3 text-xs text-accent hover:underline">
            {t.queuedImport}: {importJob.status} · {importJob.attempt_count}/{importJob.max_attempts}
          </Link>
        ) : null}
        {latest ? (
          <div className="mt-4">
            <h3 className="text-xs font-semibold">{t.latestResult}</h3>
            <RunMetrics run={latest} t={t} />
          </div>
        ) : null}
        {hhConnections.length ? (
          <div className="mt-6 border-t border-border pt-5">
            <h3 className="text-sm font-semibold">{t.hhSync}</h3>
            <p className="mt-1 text-xs leading-5 text-text-muted">{t.hhSyncHint}</p>
            <div className="mt-3 space-y-3">
              <select
                value={hhIntegrationId}
                onChange={(event) => setHhIntegrationId(event.target.value)}
                className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-sm"
              >
                {hhConnections.map((item) => (
                  <option key={item.id} value={item.id}>{item.name}</option>
                ))}
              </select>
              <select
                value={hhPositionId}
                onChange={(event) => setHhPositionId(event.target.value)}
                className="min-h-11 w-full rounded-md border border-border bg-bg px-3 text-sm"
              >
                <option value="">{t.choosePosition}</option>
                {positions.map((item) => (
                  <option key={item.id} value={item.id}>{item.title}</option>
                ))}
              </select>
              <label className="block text-xs text-text-muted">
                {t.hhVacancyId}
                <input
                  value={hhVacancyId}
                  onChange={(event) => setHhVacancyId(event.target.value)}
                  pattern="[A-Za-z0-9_-]{1,255}"
                  maxLength={255}
                  className="mt-1 min-h-11 w-full rounded-md border border-border bg-bg px-3 text-sm"
                />
              </label>
              <button
                type="button"
                disabled={
                  pending || !hhIntegrationId || !hhPositionId || !hhVacancyId
                }
                onClick={() =>
                  onSyncHeadHunter(hhIntegrationId, hhPositionId, hhVacancyId)
                }
                className="flex min-h-11 w-full items-center justify-center gap-2 rounded-md bg-accent px-3 text-xs font-semibold text-white disabled:opacity-50"
              >
                {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                {t.sync}
              </button>
              {sourceJob ? (
                <Link href="/integrations" className="block rounded-md border border-border bg-bg p-3 text-xs text-accent hover:underline">
                  {t.jobQueued}: {sourceJob.status} · {sourceJob.attempt_count}/{sourceJob.max_attempts}
                </Link>
              ) : null}
            </div>
          </div>
        ) : null}
      </section> : null}

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="mb-3 flex items-center gap-2">
          <h2 className="min-w-0 flex-1 text-sm font-semibold">{t.recentImports}</h2>
          <button
            type="button"
            disabled={pending}
            onClick={onRefresh}
            className="flex items-center gap-1 text-xs text-text-muted hover:text-text disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", pending && "animate-spin")} />
            {t.refresh}
          </button>
        </div>
        {runs.length === 0 ? (
          <p className="py-12 text-center text-sm text-text-muted">{t.noImports}</p>
        ) : (
          <div className="divide-y divide-border">
            {runs.map((run) => (
              <div key={run.id} className="py-3">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-xs font-medium">
                    {PROVIDER_LABELS[run.provider as SourceProvider] || run.provider}
                  </span>
                  <RunStatus status={run.status} t={t} />
                  <time className="text-[10px] text-text-muted">
                    {formatDate(run.created_at, locale, t.never)}
                  </time>
                </div>
                <RunMetrics run={run} t={t} compact />
                {run.error_code ? (
                  <p className="mt-1 font-mono text-[10px] text-red-600">{run.error_code}</p>
                ) : null}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function RunMetrics({ run, t, compact = false }: { run: SourceImportRun; t: CopyText; compact?: boolean }) {
  return (
    <div className={cn("grid grid-cols-4 gap-2", compact ? "mt-2" : "mt-3")}>
      {[
        [t.requested, run.requested_count],
        [t.created, run.created_count],
        [t.duplicates, run.duplicate_count],
        [t.failedRows, run.failed_count],
      ].map(([label, value]) => (
        <div key={String(label)} className="rounded-md bg-bg px-2 py-2 text-center">
          <p className="font-mono text-sm font-semibold">{value}</p>
          <p className="mt-0.5 truncate text-[9px] uppercase tracking-wide text-text-muted">{label}</p>
        </div>
      ))}
    </div>
  );
}

function RunStatus({ status, t }: { status: SourceImportRun["status"]; t: CopyText }) {
  const labels = {
    processing: t.processing,
    completed: t.completed,
    partial: t.partial,
    failed: t.failedStatus,
  };
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[10px] font-medium",
        status === "completed" && "bg-emerald-500/10 text-emerald-600",
        status === "processing" && "bg-blue-500/10 text-blue-600",
        status === "partial" && "bg-amber-500/10 text-amber-600",
        status === "failed" && "bg-red-500/10 text-red-600",
      )}
    >
      {labels[status]}
    </span>
  );
}

function ReferralsPanel({
  canView,
  canManage,
  positions,
  positionId,
  referrals,
  newUrl,
  pending,
  t,
  locale,
  onPosition,
  onCreate,
  onRotate,
  onRevoke,
  onCopy,
  onDismissUrl,
}: {
  canView: boolean;
  canManage: boolean;
  positions: SourcingPosition[];
  positionId: string;
  referrals: ReferralLink[];
  newUrl: string | null;
  pending: boolean;
  t: CopyText;
  locale: "ru" | "en";
  onPosition: (id: string) => void;
  onCreate: (body: {
    label: string;
    campaign: string | null;
    source: string;
    expires_at: string | null;
    max_applications: number | null;
  }) => void;
  onRotate: (link: ReferralLink) => void;
  onRevoke: (link: ReferralLink) => void;
  onCopy: (value: string) => void;
  onDismissUrl: () => void;
}) {
  return (
    <div className="space-y-5">
      {newUrl ? (
        <OneTimeValue
          title={t.newLink}
          hint={t.linkOneTime}
          value={newUrl}
          onCopy={onCopy}
          onClose={onDismissUrl}
        />
      ) : null}
      <div className={cn("grid gap-5", canView && "lg:grid-cols-[360px_1fr]")}>
        <section className="h-fit rounded-xl border border-border bg-bg-elevated p-5">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <Link2 className="h-4 w-4" />
            {t.referralTitle}
          </h2>
          <p className="mt-1.5 text-xs leading-5 text-text-muted">{t.referralHint}</p>
          <select
            value={positionId}
            onChange={(event) => onPosition(event.target.value)}
            className="mt-4 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
          >
            <option value="">{t.choosePosition}</option>
            {positions.map((position) => (
              <option key={position.id} value={position.id}>
                {position.title}
                {position.status !== "open" ? ` · ${t.positionClosed}` : ""}
              </option>
            ))}
          </select>
          {canManage ? <form
            className="mt-3 space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              const expires = String(form.get("expires") || "");
              const max = String(form.get("max") || "");
              onCreate({
                label: String(form.get("label") || ""),
                campaign: String(form.get("campaign") || "") || null,
                source: String(form.get("source") || "referral"),
                expires_at: expires ? new Date(expires).toISOString() : null,
                max_applications: max ? Number(max) : null,
              });
              event.currentTarget.reset();
            }}
          >
            <input
              name="label"
              required
              maxLength={160}
              placeholder={t.label}
              className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
            />
            <div className="grid grid-cols-2 gap-2">
              <input
                name="source"
                required
                maxLength={64}
                defaultValue="referral"
                placeholder={t.source}
                className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
              />
              <input
                name="campaign"
                maxLength={160}
                placeholder={t.campaign}
                className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
              />
            </div>
            <label className="block text-xs text-text-muted">
              {t.expires}
              <input
                name="expires"
                type="datetime-local"
                className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
              />
            </label>
            <label className="block text-xs text-text-muted">
              {t.maxApplications}
              <input
                name="max"
                type="number"
                min={1}
                max={100000}
                className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
              />
            </label>
            <button
              disabled={pending || !positionId}
              className="flex w-full items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
            >
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Link2 className="h-3.5 w-3.5" />}
              {t.createLink}
            </button>
          </form> : null}
        </section>

        {canView ? <section className="rounded-xl border border-border bg-bg-elevated p-5">
          <h2 className="mb-3 text-sm font-semibold">{t.referrals}</h2>
          {!positionId || referrals.length === 0 ? (
            <p className="py-12 text-center text-sm text-text-muted">{t.noReferrals}</p>
          ) : (
            <div className="divide-y divide-border">
              {referrals.map((link) => {
                const conversion = link.click_count
                  ? Math.round((link.application_count / link.click_count) * 1000) / 10
                  : 0;
                return (
                  <div key={link.id} className="py-4 first:pt-0 last:pb-0">
                    <div className="flex flex-wrap items-start gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="truncate text-sm font-medium">{link.label}</h3>
                          <span
                            className={cn(
                              "rounded-full px-2 py-0.5 text-[10px] font-medium",
                              link.is_active
                                ? "bg-emerald-500/10 text-emerald-600"
                                : "bg-bg text-text-muted",
                            )}
                          >
                            {link.is_active ? t.active : t.revoked}
                          </span>
                        </div>
                        <p className="mt-1 text-xs text-text-muted">
                          {link.source}
                          {link.campaign ? ` · ${link.campaign}` : ""}
                          {link.expires_at
                            ? ` · ${t.expires}: ${formatDate(link.expires_at, locale, t.never)}`
                            : ""}
                        </p>
                        <p className="mt-1 font-mono text-[10px] text-text-muted">
                          {t.linkNotStored} · {link.id.slice(0, 8)}
                        </p>
                      </div>
                      {link.is_active && canManage ? (
                        <div className="flex items-center gap-3">
                          <button
                            type="button"
                            disabled={pending}
                            onClick={() => onRotate(link)}
                            className="text-xs text-accent disabled:opacity-50"
                          >
                            {t.rotateLink}
                          </button>
                          <button
                            type="button"
                            disabled={pending}
                            onClick={() => onRevoke(link)}
                            className="text-xs text-red-600 disabled:opacity-50"
                          >
                            {t.revoke}
                          </button>
                        </div>
                      ) : null}
                    </div>
                    <div className="mt-3 grid grid-cols-3 gap-2">
                      <Metric label={t.clicks} value={link.click_count} />
                      <Metric label={t.applications} value={link.application_count} />
                      <Metric label={t.conversion} value={`${conversion}%`} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section> : null}
      </div>
    </div>
  );
}

function TelegramPanel({
  integrations,
  positions,
  publication,
  pending,
  t,
  locale,
  onAction,
  onCopy,
}: {
  integrations: SourceIntegration[];
  positions: SourcingPosition[];
  publication: SourcePublication | null;
  pending: boolean;
  t: CopyText;
  locale: "ru" | "en";
  onAction: (
    kind: "preview" | "publish",
    integrationId: string,
    positionId: string,
    locale: "ru" | "en",
  ) => void;
  onCopy: (value: string) => void;
}) {
  const publishers = integrations.filter(
    (item) =>
      (["telegram", "hh", "somon"] as SourceProvider[]).includes(item.provider) &&
      item.is_active &&
      item.capabilities.vacancy_publish !== "credentials_not_configured" &&
      item.capabilities.vacancy_publish !== "unsupported",
  );
  const [integrationId, setIntegrationId] = useState(publishers[0]?.id ?? "");
  const [positionId, setPositionId] = useState(positions[0]?.id ?? "");
  const [postLocale, setPostLocale] = useState<"ru" | "en">(locale);
  const canRun = Boolean(integrationId && positionId);
  return (
    <div className="grid gap-5 lg:grid-cols-[360px_1fr]">
      <section className="h-fit rounded-xl border border-border bg-bg-elevated p-5">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <RadioTower className="h-4 w-4" />
          {t.telegramTitle}
        </h2>
        <p className="mt-1.5 text-xs leading-5 text-text-muted">{t.telegramHint}</p>
        <div className="mt-4 space-y-3">
          <select
            value={integrationId}
            onChange={(event) => setIntegrationId(event.target.value)}
            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
          >
            <option value="">{t.chooseTelegram}</option>
            {publishers.map((item) => (
              <option key={item.id} value={item.id}>
                {PROVIDER_LABELS[item.provider]} · {item.name} · {item.capabilities.vacancy_publish === "sandbox" ? t.sandbox : item.connection_status ?? t.live}
              </option>
            ))}
          </select>
          <select
            value={positionId}
            onChange={(event) => setPositionId(event.target.value)}
            className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
          >
            <option value="">{t.choosePosition}</option>
            {positions.map((position) => (
              <option key={position.id} value={position.id}>
                {position.title}
              </option>
            ))}
          </select>
          <label className="block text-xs text-text-muted">
            {t.postLanguage}
            <select
              value={postLocale}
              onChange={(event) => setPostLocale(event.target.value as "ru" | "en")}
              className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
            >
              <option value="ru">Русский</option>
              <option value="en">English</option>
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              disabled={pending || !canRun}
              onClick={() => onAction("preview", integrationId, positionId, postLocale)}
              className="flex items-center justify-center gap-1.5 rounded-md border border-border px-3 py-2 text-xs font-medium disabled:opacity-50"
            >
              <Clipboard className="h-3.5 w-3.5" />
              {t.preview}
            </button>
            <button
              type="button"
              disabled={pending || !canRun}
              onClick={() => onAction("publish", integrationId, positionId, postLocale)}
              className="flex items-center justify-center gap-1.5 rounded-md bg-accent px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
            >
              {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
              {t.publish}
            </button>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="mb-3 flex items-center gap-2">
          <h2 className="min-w-0 flex-1 text-sm font-semibold">{t.postPreview}</h2>
          {publication ? <PublicationStatus publication={publication} t={t} /> : null}
        </div>
        {!publication ? (
          <p className="py-16 text-center text-sm text-text-muted">{t.noPreview}</p>
        ) : (
          <>
            <div className="rounded-lg border border-border bg-bg p-4">
              <p className="whitespace-pre-wrap text-sm leading-6">{publication.post_text}</p>
            </div>
            <div className="mt-4">
              <div className="flex items-center gap-2">
                <p className="min-w-0 flex-1 text-xs font-medium">{t.trackingUrl}</p>
                <button
                  type="button"
                  onClick={() => onCopy(publication.tracking_url)}
                  className="flex items-center gap-1 text-xs text-accent"
                >
                  <Copy className="h-3.5 w-3.5" />
                  {t.copied.replace("Copied", "Copy").replace("Скопировано", "Копировать")}
                </button>
              </div>
              <Link
                href={publication.tracking_url}
                target="_blank"
                rel="noreferrer"
                className="mt-1 block break-all font-mono text-[11px] text-accent hover:underline"
              >
                {publication.tracking_url}
              </Link>
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function PublicationStatus({ publication, t }: { publication: SourcePublication; t: CopyText }) {
  const failed = publication.status === "failed" || publication.job_status === "dead_letter";
  const label = failed
    ? t.publicationError
    : publication.status === "published_sandbox"
      ? t.publishedSandbox
      : publication.status === "published"
        ? t.publicationPublished
        : publication.status === "tracked"
          ? t.publicationTracked
          : publication.status === "queued"
            ? t.publicationQueued
            : publication.status === "credentials_not_configured"
              ? t.publicationFailed
              : t.previewStatus;
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 text-[10px] font-medium",
        failed
          ? "bg-red-500/10 text-red-600"
          : ["published_sandbox", "published", "tracked"].includes(publication.status)
            ? "bg-emerald-500/10 text-emerald-600"
            : "bg-blue-500/10 text-blue-600",
      )}
    >
      {label}
    </span>
  );
}

function AnalyticsPanel({
  analytics,
  pending,
  t,
  onRefresh,
}: {
  analytics: SourceAnalytics;
  pending: boolean;
  t: CopyText;
  onRefresh: () => void;
}) {
  const max = Math.max(...analytics.items.map((item) => item.applications), 1);
  const hired = analytics.items.reduce((sum, item) => sum + item.hired, 0);
  const conversion = analytics.total_applications
    ? Math.round((hired / analytics.total_applications) * 1000) / 10
    : 0;
  return (
    <section className="rounded-xl border border-border bg-bg-elevated p-5">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="flex items-center gap-2 text-sm font-semibold">
            <BarChart3 className="h-4 w-4" />
            {t.analyticsTitle}
          </h2>
          <p className="mt-1.5 text-xs leading-5 text-text-muted">{t.analyticsHint}</p>
        </div>
        <button
          type="button"
          disabled={pending}
          onClick={onRefresh}
          className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs disabled:opacity-50"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", pending && "animate-spin")} />
          {t.refresh}
        </button>
      </div>
      <div className="mt-5 grid gap-3 sm:grid-cols-3">
        <Metric label={t.totalApplications} value={analytics.total_applications} large />
        <Metric label={t.hired} value={hired} large />
        <Metric label={t.conversion} value={`${conversion}%`} large />
      </div>
      {analytics.items.length === 0 ? (
        <p className="py-14 text-center text-sm text-text-muted">{t.noAnalytics}</p>
      ) : (
        <div className="mt-6 space-y-4">
          {analytics.items.map((item) => (
            <div key={item.source}>
              <div className="mb-1.5 flex items-center gap-3 text-xs">
                <span className="min-w-0 flex-1 truncate font-medium">{item.source}</span>
                <span className="font-mono text-text-muted">
                  {item.applications} · {item.hired} {t.hired.toLowerCase()} · {item.conversion_rate}%
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-bg">
                <div
                  className="h-full rounded-full bg-accent"
                  style={{ width: `${Math.max((item.applications / max) * 100, 2)}%` }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function StatusBadge({ status, t }: { status: SourceIntegration["status"]; t: CopyText }) {
  const labels = {
    ready: t.ready,
    credentials_not_configured: t.credentialsNotConfigured,
    error: t.error,
    disabled: t.disabled,
  };
  const good = status === "ready";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium",
        good
          ? "bg-emerald-500/10 text-emerald-600"
          : status === "credentials_not_configured"
            ? "bg-amber-500/10 text-amber-600"
            : "bg-bg text-text-muted",
      )}
    >
      {good ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {labels[status]}
    </span>
  );
}

function OneTimeValue({
  title,
  hint,
  value,
  onCopy,
  onClose,
}: {
  title: string;
  hint: string;
  value: string;
  onCopy: (value: string) => void;
  onClose: () => void;
}) {
  const { m } = useI18n();
  return (
    <div className="mb-5 rounded-xl border border-amber-300/70 bg-amber-50 p-4 text-amber-950 dark:bg-amber-950/20 dark:text-amber-100">
      <div className="flex items-start gap-3">
        <KeyRound className="mt-0.5 h-4 w-4 shrink-0" />
        <div className="min-w-0 flex-1">
          <h2 className="text-xs font-semibold">{title}</h2>
          <p className="mt-1 text-xs opacity-80">{hint}</p>
          <code className="mt-3 block break-all rounded border border-amber-300/60 bg-white/60 px-3 py-2 text-xs dark:bg-black/20">
            {value}
          </code>
        </div>
        <button type="button" onClick={onClose} aria-label={m.common.close} className="opacity-60 hover:opacity-100">
          <XCircle className="h-4 w-4" />
        </button>
      </div>
      <button
        type="button"
        onClick={() => onCopy(value)}
        className="mt-3 flex items-center gap-1.5 rounded-md bg-amber-900 px-3 py-1.5 text-xs font-medium text-white dark:bg-amber-200 dark:text-amber-950"
      >
        <Copy className="h-3.5 w-3.5" />
        {title}
      </button>
    </div>
  );
}

function Metric({
  label,
  value,
  large = false,
}: {
  label: string;
  value: string | number;
  large?: boolean;
}) {
  return (
    <div className="rounded-lg border border-border bg-bg p-3 text-center">
      <p className={cn("font-mono font-semibold", large ? "text-xl" : "text-sm")}>{value}</p>
      <p className="mt-1 text-[10px] uppercase tracking-wide text-text-muted">{label}</p>
    </div>
  );
}

function EmptyState({ icon: Icon, text }: { icon: typeof Globe2; text: string }) {
  return (
    <div className="rounded-xl border border-border bg-bg-elevated py-16 text-center">
      <Icon className="mx-auto h-6 w-6 text-text-muted" />
      <p className="mt-3 text-sm text-text-muted">{text}</p>
    </div>
  );
}

function formatDate(value: string | null, locale: "ru" | "en", fallback: string) {
  if (!value) return fallback;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return fallback;
  return new Intl.DateTimeFormat(locale === "en" ? "en-US" : "ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}
