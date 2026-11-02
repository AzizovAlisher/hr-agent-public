"use client";

import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleOff,
  Clock3,
  ExternalLink,
  KeyRound,
  Loader2,
  Play,
  PlugZap,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { useMemo, useState, useTransition } from "react";
import { toast } from "sonner";

import {
  cancelIntegrationJobAction,
  checkIntegrationHealthAction,
  createIntegrationConnectionAction,
  listIntegrationConnectionsAction,
  listIntegrationJobsAction,
  replayIntegrationJobAction,
  revokeIntegrationConnectionAction,
  runIntegrationJobAction,
  setIntegrationCredentialsAction,
  startIntegrationOAuthAction,
  updateIntegrationConnectionAction,
} from "@/app/(app)/integrations/actions";
import type {
  IntegrationAdminAccess,
  IntegrationConnection,
  IntegrationJob,
  IntegrationProvider,
  IntegrationProviderDefinition,
  IntegrationState,
} from "@/app/(app)/integrations/types";
import { useI18n } from "@/components/i18n/locale-provider";
import { machineLabel } from "@/components/i18n/machine-label";
import { cn } from "@/lib/utils";

/** @ui-only Integration-admin composition properties. */
type Props = {
  access: IntegrationAdminAccess;
  providers: IntegrationProviderDefinition[];
  initialConnections: IntegrationConnection[];
  connectionCursor: string | null;
  initialJobs: IntegrationJob[];
  jobCursor: string | null;
  loadFailed: boolean;
};

/** @ui-only Integration-admin tab state. */
type Tab = "connections" | "jobs";

const COPY = {
  en: {
    title: "Integration connections",
    subtitle: "Manage provider access, health, durable work, and revocation without exposing credentials.",
    connections: "Connections",
    jobs: "Provider jobs",
    add: "Add connection",
    provider: "Provider",
    name: "Connection name",
    create: "Create",
    creating: "Creating…",
    unavailable: "Unavailable in this deployment",
    noConnections: "No provider connections yet.",
    noJobs: "No provider jobs yet.",
    loadFailed: "Integration data could not be loaded. Retry before making changes.",
    capabilities: "Capabilities",
    safeSettings: "Non-secret settings",
    saveSettings: "Save settings",
    credentials: "Write-only credentials",
    credentialsHint: "Saved values are encrypted and are never returned by the API.",
    saveCredentials: "Save credentials",
    oauth: "Continue with provider OAuth",
    health: "Check health",
    revoke: "Revoke",
    revokeConfirm: "Revoke this connection now? Local credentials will be deleted immediately.",
    oneTime: "Copy this signing secret now",
    oneTimeHint: "It is shown once and cannot be recovered. Store it in your password manager.",
    configured: "Credentials configured",
    notConfigured: "Credentials not configured",
    lastHealth: "Last health check",
    lastSuccess: "Last success",
    never: "Never",
    error: "Last error",
    attempts: "Attempts",
    queued: "Queued",
    running: "Running",
    retrying: "Retrying",
    succeeded: "Succeeded",
    dead_letter: "Dead letter",
    cancelled: "Cancelled",
    cancel: "Cancel",
    replay: "Replay",
    runNow: "Run now",
    loadMore: "Load more",
    updated: "Updated",
    failed: "Action failed",
    copy: "Copy",
    copied: "Copied",
    account: "Provider account",
    scopes: "Granted scopes",
    refresh: "Refresh view",
    expand: "Expand connection",
    collapse: "Collapse connection",
  },
  ru: {
    title: "Подключения интеграций",
    subtitle: "Управляйте доступом, состоянием, фоновыми заданиями и отзывом без раскрытия учётных данных.",
    connections: "Подключения",
    jobs: "Задания провайдеров",
    add: "Добавить подключение",
    provider: "Провайдер",
    name: "Название подключения",
    create: "Создать",
    creating: "Создание…",
    unavailable: "Недоступно в этом развёртывании",
    noConnections: "Подключений провайдеров пока нет.",
    noJobs: "Заданий провайдеров пока нет.",
    loadFailed: "Данные интеграций не загрузились. Повторите загрузку до изменений.",
    capabilities: "Возможности",
    safeSettings: "Настройки без секретов",
    saveSettings: "Сохранить настройки",
    credentials: "Учётные данные только для записи",
    credentialsHint: "Значения шифруются и никогда не возвращаются API.",
    saveCredentials: "Сохранить учётные данные",
    oauth: "Продолжить OAuth провайдера",
    health: "Проверить состояние",
    revoke: "Отозвать",
    revokeConfirm: "Отозвать подключение? Локальные учётные данные будут удалены немедленно.",
    oneTime: "Скопируйте секрет подписи сейчас",
    oneTimeHint: "Он показывается один раз и не восстанавливается. Сохраните его в менеджере паролей.",
    configured: "Учётные данные настроены",
    notConfigured: "Учётные данные не настроены",
    lastHealth: "Последняя проверка",
    lastSuccess: "Последний успех",
    never: "Никогда",
    error: "Последняя ошибка",
    attempts: "Попытки",
    queued: "В очереди",
    running: "Выполняется",
    retrying: "Повтор",
    succeeded: "Выполнено",
    dead_letter: "Требует внимания",
    cancelled: "Отменено",
    cancel: "Отменить",
    replay: "Повторить",
    runNow: "Запустить",
    loadMore: "Загрузить ещё",
    updated: "Обновлено",
    failed: "Действие не выполнено",
    copy: "Копировать",
    copied: "Скопировано",
    account: "Аккаунт провайдера",
    scopes: "Выданные права",
    refresh: "Обновить",
    expand: "Развернуть подключение",
    collapse: "Свернуть подключение",
  },
} as const;

/** @ui-only Localized integration presentation copy. */
type Copy = Record<keyof (typeof COPY)["en"], string>;

const LABELS: Record<IntegrationProvider, string> = {
  google_workspace: "Google Workspace",
  microsoft_365: "Microsoft 365",
  telegram: "Telegram Bot API",
  hh_ru: "hh.ru",
  somon: "Somon.tj",
  csv_xlsx: "CSV / XLSX",
  generic_webhook: "Signed REST / webhook",
  assessment_generic: "Assessment provider",
};

const STATUS_STYLE: Record<IntegrationState, string> = {
  connected: "border-success/30 bg-success/10 text-success",
  connecting: "border-warning/30 bg-warning/10 text-warning",
  disconnected: "border-border bg-bg text-text-muted",
  degraded: "border-warning/30 bg-warning/10 text-warning",
  expired: "border-danger/30 bg-danger/10 text-danger",
  revoked: "border-border bg-bg text-text-muted",
  unavailable: "border-danger/30 bg-danger/10 text-danger",
};

export function IntegrationAdmin({
  access,
  providers,
  initialConnections,
  connectionCursor: initialConnectionCursor,
  initialJobs,
  jobCursor: initialJobCursor,
  loadFailed,
}: Props) {
  const { locale } = useI18n();
  const t = COPY[locale];
  const [tab, setTab] = useState<Tab>("connections");
  const [connections, setConnections] = useState(initialConnections);
  const [jobs, setJobs] = useState(initialJobs);
  const [connectionCursor, setConnectionCursor] = useState(initialConnectionCursor);
  const [jobCursor, setJobCursor] = useState(initialJobCursor);
  const [oneTimeSecret, setOneTimeSecret] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const replaceConnection = (next: IntegrationConnection) => {
    setConnections((current) => {
      const found = current.some((item) => item.id === next.id);
      return found
        ? current.map((item) => (item.id === next.id ? next : item))
        : [next, ...current];
    });
  };
  const replaceJob = (next: IntegrationJob) => {
    setJobs((current) => current.map((item) => (item.id === next.id ? next : item)));
  };

  const loadMoreConnections = () => {
    if (!connectionCursor) return;
    startTransition(async () => {
      const result = await listIntegrationConnectionsAction(connectionCursor);
      if (!result.ok) {
        toast.error(t.failed);
        return;
      }
      setConnections((current) => [...current, ...result.data.items]);
      setConnectionCursor(result.data.next_cursor);
    });
  };
  const loadMoreJobs = () => {
    if (!jobCursor) return;
    startTransition(async () => {
      const result = await listIntegrationJobsAction(jobCursor);
      if (!result.ok) {
        toast.error(t.failed);
        return;
      }
      setJobs((current) => [...current, ...result.data.items]);
      setJobCursor(result.data.next_cursor);
    });
  };

  if (!access.read && !access.manage) {
    return <div className="rounded-xl border border-border bg-bg-elevated p-6 text-sm text-text-muted">403</div>;
  }

  return (
    <div className="mx-auto w-full max-w-7xl space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{t.title}</h1>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-text-muted">{t.subtitle}</p>
        </div>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-sm font-medium"
        >
          <RefreshCw className="h-4 w-4" />
          {t.refresh}
        </button>
      </header>

      {loadFailed ? (
        <div role="alert" className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/10 p-4 text-sm text-danger">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          {t.loadFailed}
        </div>
      ) : null}

      {oneTimeSecret ? (
        <OneTimeSecret value={oneTimeSecret} t={t} onClose={() => setOneTimeSecret(null)} />
      ) : null}

      <div role="tablist" aria-label={t.title} className="flex gap-1 rounded-lg border border-border bg-bg-elevated p-1">
        {(["connections", "jobs"] as const).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={tab === item}
            onClick={() => setTab(item)}
            className={cn(
              "min-h-11 flex-1 rounded-md px-4 text-sm font-medium",
              tab === item ? "bg-bg text-text shadow-sm" : "text-text-muted",
            )}
          >
            {item === "connections" ? t.connections : t.jobs}
          </button>
        ))}
      </div>

      {tab === "connections" ? (
        <div className="space-y-5">
          {access.manage ? (
            <CreateConnection
              providers={providers}
              pending={pending}
              t={t}
              locale={locale}
              onCreate={(provider, name, configuration) => {
                startTransition(async () => {
                  const result = await createIntegrationConnectionAction({ provider, name, configuration });
                  if (!result.ok) {
                    toast.error(`${t.failed}: ${machineLabel(result.code, locale)}`);
                    return;
                  }
                  replaceConnection(result.data.connection);
                  setOneTimeSecret(result.data.webhook_signing_secret);
                  toast.success(t.updated);
                });
              }}
            />
          ) : null}
          {connections.length ? (
            <div className="grid gap-4 xl:grid-cols-2">
              {connections.map((connection) => (
                <ConnectionCard
                  key={connection.id}
                  connection={connection}
                  definition={providers.find((item) => item.provider === connection.provider)}
                  access={access}
                  pending={pending}
                  t={t}
                  locale={locale}
                  onUpdate={replaceConnection}
                  startTransition={startTransition}
                />
              ))}
            </div>
          ) : (
            <Empty icon={PlugZap} text={t.noConnections} />
          )}
          {connectionCursor ? <LoadMore pending={pending} label={t.loadMore} onClick={loadMoreConnections} /> : null}
        </div>
      ) : (
        <div className="space-y-4">
          {jobs.length ? jobs.map((job) => (
            <JobCard
              key={job.id}
              job={job}
              connection={connections.find((item) => item.id === job.connection_id)}
              canManage={access.manage}
              pending={pending}
              t={t}
              locale={locale}
              onChange={replaceJob}
              startTransition={startTransition}
            />
          )) : <Empty icon={Clock3} text={t.noJobs} />}
          {jobCursor ? <LoadMore pending={pending} label={t.loadMore} onClick={loadMoreJobs} /> : null}
        </div>
      )}
    </div>
  );
}

function CreateConnection({ providers, pending, t, locale, onCreate }: {
  providers: IntegrationProviderDefinition[];
  pending: boolean;
  t: Copy;
  locale: "ru" | "en";
  onCreate: (provider: IntegrationProvider, name: string, configuration: Record<string, unknown>) => void;
}) {
  const available = providers.filter((item) => item.locally_available);
  const [provider, setProvider] = useState<IntegrationProvider>(available[0]?.provider ?? "csv_xlsx");
  const [name, setName] = useState("");
  const [configuration, setConfiguration] = useState<Record<string, unknown>>({});
  const selected = providers.find((item) => item.provider === provider);
  return (
    <section className="rounded-xl border border-border bg-bg-elevated p-5">
      <h2 className="text-sm font-semibold">{t.add}</h2>
      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <label className="text-xs font-medium text-text-muted">
          {t.provider}
          <select
            value={provider}
            onChange={(event) => {
              setProvider(event.target.value as IntegrationProvider);
              setConfiguration({});
            }}
            className="mt-1 min-h-11 w-full rounded-lg border border-border bg-bg px-3 text-sm text-text"
          >
            {providers.map((item) => (
              <option key={item.provider} value={item.provider} disabled={!item.locally_available}>
                {LABELS[item.provider]}{item.locally_available ? "" : ` · ${t.unavailable}`}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs font-medium text-text-muted">
          {t.name}
          <input
            value={name}
            maxLength={160}
            onChange={(event) => setName(event.target.value)}
            className="mt-1 min-h-11 w-full rounded-lg border border-border bg-bg px-3 text-sm text-text"
          />
        </label>
      </div>
      <ConfigurationFields provider={provider} value={configuration} onChange={setConfiguration} locale={locale} />
      {selected?.unavailable_reason ? <p className="mt-3 text-xs text-danger">{machineLabel(selected.unavailable_reason, locale)}</p> : null}
      <button
        type="button"
        disabled={pending || !name.trim() || selected?.locally_available === false}
        onClick={() => onCreate(provider, name.trim(), configuration)}
        className="mt-4 inline-flex min-h-11 items-center gap-2 rounded-lg bg-accent px-4 text-sm font-semibold text-white disabled:opacity-50"
      >
        {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />}
        {pending ? t.creating : t.create}
      </button>
    </section>
  );
}

function ConnectionCard({ connection, definition, access, pending, t, locale, onUpdate, startTransition }: {
  connection: IntegrationConnection;
  definition?: IntegrationProviderDefinition;
  access: IntegrationAdminAccess;
  pending: boolean;
  t: Copy;
  locale: "ru" | "en";
  onUpdate: (connection: IntegrationConnection) => void;
  startTransition: (callback: () => Promise<void>) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [configuration, setConfiguration] = useState(connection.configuration);
  const credentialKeys = useMemo(() => {
    if (!definition) return [];
    if (definition.oauth) return definition.credential_keys.filter((key) => ["client_id", "client_secret"].includes(key));
    if (connection.provider === "telegram") return ["bot_token"];
    return definition.credential_keys;
  }, [connection.provider, definition]);
  const [credentials, setCredentials] = useState<Record<string, string>>({});

  const action = (work: () => Promise<{ ok: boolean; data?: unknown; code?: string }>, done?: (data: never) => void) => {
    startTransition(async () => {
      const result = await work();
      if (!result.ok) {
        toast.error(`${t.failed}: ${machineLabel(result.code ?? "unknown", locale)}`);
        return;
      }
      if (done) done(result.data as never);
      toast.success(t.updated);
    });
  };
  const oauth = () => {
    startTransition(async () => {
      const redirectUri = `${window.location.origin}/integrations/oauth/callback`;
      const result = await startIntegrationOAuthAction(connection.id, redirectUri);
      if (!result.ok) {
        toast.error(`${t.failed}: ${machineLabel(result.code, locale)}`);
        return;
      }
      window.location.assign(result.data.authorization_url);
    });
  };

  return (
    <article className="rounded-xl border border-border bg-bg-elevated p-5">
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-border bg-bg"><ShieldCheck className="h-4 w-4" /></div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate text-sm font-semibold">{connection.name}</h3>
            <span className={cn("rounded-full border px-2 py-0.5 text-[11px] font-semibold", STATUS_STYLE[connection.status])}>{machineLabel(connection.status, locale)}</span>
          </div>
          <p className="mt-1 text-xs text-text-muted">{LABELS[connection.provider]}</p>
        </div>
        <button type="button" aria-label={`${expanded ? t.collapse : t.expand}: ${connection.name}`} aria-expanded={expanded} onClick={() => setExpanded((value) => !value)} className="flex min-h-11 min-w-11 items-center justify-center rounded-lg border border-border">
          {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-3 text-xs">
        <Metric label={t.lastHealth} value={formatDate(connection.last_health_at, locale, t.never)} />
        <Metric label={t.lastSuccess} value={formatDate(connection.last_success_at, locale, t.never)} />
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {connection.capabilities.map((capability) => <span key={capability} className="rounded bg-bg px-2 py-1 text-[11px] text-text-muted">{machineLabel(capability, locale)}</span>)}
      </div>
      {connection.last_error_code ? <p className="mt-3 text-xs text-danger">{t.error}: {machineLabel(connection.last_error_code, locale)}</p> : null}
      <div className="mt-4 flex flex-wrap gap-2">
        <button type="button" disabled={pending || !access.manage || connection.status === "revoked"} onClick={() => action(() => checkIntegrationHealthAction(connection.id), (data) => onUpdate({ ...connection, status: (data as { status: IntegrationState }).status, last_health_at: (data as { checked_at: string }).checked_at, last_error_code: (data as { error_code: string | null }).error_code }))} className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-xs font-medium disabled:opacity-50"><Activity className="h-4 w-4" />{t.health}</button>
        {access.manage && connection.status !== "revoked" ? <button type="button" disabled={pending} onClick={() => { if (window.confirm(t.revokeConfirm)) action(() => revokeIntegrationConnectionAction(connection.id), (data) => onUpdate(data as IntegrationConnection)); }} className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-danger/30 px-3 text-xs font-medium text-danger disabled:opacity-50"><CircleOff className="h-4 w-4" />{t.revoke}</button> : null}
      </div>

      {expanded ? (
        <div className="mt-5 space-y-5 border-t border-border pt-5">
          {connection.provider_account_id ? <Metric label={t.account} value={connection.provider_account_id} /> : null}
          {connection.granted_scopes.length ? <Metric label={t.scopes} value={connection.granted_scopes.map((scope) => machineLabel(scope, locale)).join(", ")} /> : null}
          <section>
            <h4 className="text-xs font-semibold">{t.safeSettings}</h4>
            <ConfigurationFields provider={connection.provider} value={configuration} onChange={setConfiguration} locale={locale} />
            {access.manage ? <button type="button" disabled={pending || connection.status === "revoked"} onClick={() => action(() => updateIntegrationConnectionAction(connection.id, { configuration }), (data) => onUpdate(data as IntegrationConnection))} className="mt-3 inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-xs font-medium disabled:opacity-50"><Save className="h-4 w-4" />{t.saveSettings}</button> : null}
          </section>
          {credentialKeys.length && access.credentials ? (
            <section className="rounded-lg border border-border bg-bg p-4">
              <h4 className="text-xs font-semibold">{t.credentials}</h4>
              <p className="mt-1 text-xs text-text-muted">{t.credentialsHint}</p>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                {credentialKeys.map((key) => (
                  <label key={key} className="text-xs font-medium text-text-muted">
                    {machineLabel(key, locale)}
                    <input type="password" autoComplete="new-password" value={credentials[key] ?? ""} onChange={(event) => setCredentials((current) => ({ ...current, [key]: event.target.value }))} className="mt-1 min-h-11 w-full rounded-lg border border-border bg-bg-elevated px-3 text-sm text-text" />
                  </label>
                ))}
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button type="button" disabled={pending || credentialKeys.some((key) => !(credentials[key] ?? "").trim()) || connection.status === "revoked"} onClick={() => action(() => setIntegrationCredentialsAction(connection.id, credentials), () => { setCredentials({}); onUpdate({ ...connection, credentials_configured: true, status: definition?.oauth ? "connecting" : "disconnected" }); })} className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-accent px-3 text-xs font-semibold text-white disabled:opacity-50"><KeyRound className="h-4 w-4" />{t.saveCredentials}</button>
                {definition?.oauth && connection.credentials_configured && connection.status !== "revoked" ? <button type="button" disabled={pending} onClick={oauth} className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-xs font-medium disabled:opacity-50"><ExternalLink className="h-4 w-4" />{t.oauth}</button> : null}
              </div>
            </section>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function ConfigurationFields({ provider, value, onChange, locale }: { provider: IntegrationProvider; value: Record<string, unknown>; onChange: (value: Record<string, unknown>) => void; locale: "ru" | "en" }) {
  const labels = locale === "ru" ? {
    calendarId: "Идентификатор календаря",
    tenant: "Организация Microsoft",
    chatId: "Идентификатор чата Telegram",
    parseMode: "Формат сообщения",
    plainText: "Обычный текст",
    deliveryEndpoint: "HTTPS-адрес доставки",
    approvedBaseUrl: "Разрешённый оператором базовый HTTPS-адрес",
  } : {
    calendarId: "Calendar ID",
    tenant: "Microsoft tenant",
    chatId: "Telegram chat ID",
    parseMode: "Message format",
    plainText: "Plain text",
    deliveryEndpoint: "HTTPS delivery endpoint",
    approvedBaseUrl: "Operator-approved HTTPS base URL",
  };
  const field = (key: string, label: string, placeholder = "") => (
    <label className="text-xs font-medium text-text-muted">
      {label}
      <input value={typeof value[key] === "string" ? String(value[key]) : ""} placeholder={placeholder} onChange={(event) => onChange({ ...value, [key]: event.target.value })} className="mt-1 min-h-11 w-full rounded-lg border border-border bg-bg px-3 text-sm text-text" />
    </label>
  );
  const fields: React.ReactNode[] = [];
  if (provider === "google_workspace") fields.push(field("calendar_id", labels.calendarId, "primary"));
  if (provider === "microsoft_365") {
    fields.push(field("tenant", labels.tenant, "organizations"));
    fields.push(field("calendar_id", labels.calendarId, "primary"));
  }
  if (provider === "telegram") {
    fields.push(field("chat_id", labels.chatId, "@vacancies"));
    fields.push(
      <label key="parse_mode" className="text-xs font-medium text-text-muted">
        {labels.parseMode}
        <select value={typeof value.parse_mode === "string" ? value.parse_mode : ""} onChange={(event) => onChange({ ...value, parse_mode: event.target.value || undefined })} className="mt-1 min-h-11 w-full rounded-lg border border-border bg-bg px-3 text-sm text-text">
          <option value="">{labels.plainText}</option><option value="HTML">HTML</option><option value="MarkdownV2">MarkdownV2</option>
        </select>
      </label>,
    );
  }
  if (provider === "generic_webhook") fields.push(field("target_url", labels.deliveryEndpoint));
  if (provider === "assessment_generic") fields.push(field("base_url", labels.approvedBaseUrl));
  if (!fields.length) return null;
  return <div className="mt-3 grid gap-3 sm:grid-cols-2">{fields.map((item, index) => <div key={index}>{item}</div>)}</div>;
}

function JobCard({ job, connection, canManage, pending, t, locale, onChange, startTransition }: { job: IntegrationJob; connection?: IntegrationConnection; canManage: boolean; pending: boolean; t: Copy; locale: "ru" | "en"; onChange: (job: IntegrationJob) => void; startTransition: (callback: () => Promise<void>) => void }) {
  const mutate = (action: () => ReturnType<typeof cancelIntegrationJobAction>) => {
    startTransition(async () => {
      const result = await action();
      if (!result.ok) {
        toast.error(`${t.failed}: ${machineLabel(result.code, locale)}`);
        return;
      }
      onChange(result.data);
      toast.success(t.updated);
    });
  };
  const StatusIcon = job.status === "succeeded" ? CheckCircle2 : job.status === "dead_letter" ? AlertTriangle : job.status === "cancelled" ? XCircle : Clock3;
  return (
    <article className="rounded-xl border border-border bg-bg-elevated p-5">
      <div className="flex flex-wrap items-start gap-3">
        <StatusIcon className={cn("mt-0.5 h-5 w-5", job.status === "succeeded" ? "text-success" : job.status === "dead_letter" ? "text-danger" : "text-text-muted")} />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold">{machineLabel(job.operation, locale)}</h3>
          <p className="mt-1 break-all text-xs text-text-muted">{connection?.name ?? job.connection_id} · {formatDate(job.created_at, locale, t.never)}</p>
        </div>
        <span className="rounded-full border border-border bg-bg px-2 py-1 text-[11px] font-semibold">{t[job.status]}</span>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <Metric label={t.attempts} value={`${job.attempt_count}/${job.max_attempts}`} />
        <Metric label="ID" value={job.id} />
        <Metric label={t.error} value={job.failure_code ? machineLabel(job.failure_code, locale) : "—"} />
      </div>
      {canManage ? <div className="mt-4 flex flex-wrap gap-2">
        {(["queued", "retrying"] as const).includes(job.status as "queued" | "retrying") ? <>
          <button type="button" disabled={pending} onClick={() => mutate(() => cancelIntegrationJobAction(job.id))} className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-xs font-medium disabled:opacity-50"><XCircle className="h-4 w-4" />{t.cancel}</button>
          <button type="button" disabled={pending} onClick={() => mutate(() => runIntegrationJobAction(job.id))} className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-xs font-medium disabled:opacity-50"><Play className="h-4 w-4" />{t.runNow}</button>
        </> : null}
        {job.status === "dead_letter" ? <button type="button" disabled={pending} onClick={() => mutate(() => replayIntegrationJobAction(job.id))} className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-3 text-xs font-medium disabled:opacity-50"><RotateCcw className="h-4 w-4" />{t.replay}</button> : null}
      </div> : null}
    </article>
  );
}

function OneTimeSecret({ value, t, onClose }: { value: string; t: Copy; onClose: () => void }) {
  return <section role="alert" className="rounded-xl border border-warning/40 bg-warning/10 p-5">
    <div className="flex items-start gap-3"><KeyRound className="mt-0.5 h-5 w-5 text-warning" /><div className="min-w-0 flex-1"><h2 className="text-sm font-semibold">{t.oneTime}</h2><p className="mt-1 text-xs text-text-muted">{t.oneTimeHint}</p><code className="mt-3 block break-all rounded-lg border border-border bg-bg p-3 text-xs">{value}</code></div></div>
    <div className="mt-3 flex gap-2"><button type="button" onClick={async () => { await navigator.clipboard.writeText(value); toast.success(t.copied); }} className="min-h-11 rounded-lg bg-accent px-4 text-xs font-semibold text-white">{t.copy}</button><button type="button" onClick={onClose} className="min-h-11 rounded-lg border border-border px-4 text-xs font-medium">OK</button></div>
  </section>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="min-w-0"><p className="text-[11px] font-medium uppercase tracking-wide text-text-faint">{label}</p><p className="mt-1 break-all text-xs text-text-muted">{value}</p></div>; }
function LoadMore({ pending, label, onClick }: { pending: boolean; label: string; onClick: () => void }) { return <button type="button" disabled={pending} onClick={onClick} className="mx-auto flex min-h-11 items-center gap-2 rounded-lg border border-border px-4 text-sm font-medium disabled:opacity-50">{pending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}{label}</button>; }
function Empty({ icon: Icon, text }: { icon: typeof PlugZap; text: string }) { return <div className="rounded-xl border border-dashed border-border p-10 text-center text-sm text-text-muted"><Icon className="mx-auto mb-3 h-6 w-6" />{text}</div>; }
function formatDate(value: string | null, locale: "ru" | "en", fallback: string) { return value ? new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : fallback; }
