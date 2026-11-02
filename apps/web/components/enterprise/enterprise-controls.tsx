"use client";

import {
  ArchiveRestore,
  Bot,
  DatabaseBackup,
  FileClock,
  Gauge,
  KeyRound,
  LockKeyhole,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import { machineLabel } from "@/components/i18n/machine-label";
import {
  createApiKeyAction,
  deletePermissionOverrideAction,
  loadUsagePageAction,
  putPermissionOverrideAction,
  restoreTrashAction,
  revokeApiKeyAction,
  runRetentionAction,
  updateEnterprisePolicyAction,
  updateModelConfigurationAction,
} from "@/app/(app)/enterprise/actions";
import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { cn } from "@/lib/utils";

type Policy = ApiResponseSchema<"EnterprisePolicyOut">;
type PermissionOverride = ApiResponseSchema<"PermissionOverrideOut">;
type AuditItem = ApiResponseSchema<"AuditLogOut">;
type AuditPage = ApiResponseSchema<"AuditLogPage">;
type TrashItem = ApiResponseSchema<"TrashItemOut">;
type TrashPage = ApiResponseSchema<"TrashPage">;
type Retention = ApiResponseSchema<"RetentionPreviewOut">;
type ApiKey = ApiResponseSchema<"ApiKeyOut">;
type SsoConfig = ApiResponseSchema<"SsoConfigurationOut">;
type ModelConfig = ApiResponseSchema<"ModelConfigurationOut">;
type EntitlementUsage = ApiResponseSchema<"EntitlementUsageOut">;
type EntitlementMetric = EntitlementUsage["metric"];
type Entitlements = ApiResponseSchema<"EntitlementsOut">;
type UsageItem = ApiResponseSchema<"UsageEventOut">;
type UsagePage = ApiResponseSchema<"UsagePage">;

/** @ui-only Enterprise workspace tab state. */
type Tab = "policy" | "usage" | "access" | "audit" | "recovery" | "providers";

const COPY = {
  en: {
    title: "Enterprise controls",
    subtitle: "Retention, access, audit history, recovery, and private infrastructure.",
    accessDenied: "Your current permissions do not allow access to this area.",
    policy: "Policy",
    usage: "Usage & limits",
    access: "Access & API",
    audit: "Audit log",
    recovery: "Recovery",
    providers: "SSO & models",
    save: "Save changes",
    saved: "Changes saved",
    failed: "Could not save changes",
    days: "days",
    candidateRetention: "Candidate retention",
    deletedRetention: "Trash retention",
    auditRetention: "Audit retention",
    exports: "Allow candidate CSV exports",
    backups: "Allow core-data portability exports",
    compensation: "Roles allowed to see compensation",
    approvalSeparation: "Approval separation of duties",
    approvalSeparationHint:
      "When enabled, the author must use a different approver. Disable only when the tenant contract explicitly permits self-approval.",
    separateRequisitions: "Require a different approver for requisitions",
    separateOffers: "Require a different approver for candidate offers",
    separateHiringDecisions: "Require a different approver for hiring decisions",
    permissions: "Role permission overrides",
    role: "Role",
    permission: "Permission",
    allow: "Allow",
    deny: "Deny",
    addOverride: "Save override",
    noOverrides: "No overrides; role defaults are active.",
    apiKeys: "API keys",
    apiHint: "Secrets are shown once. Store them in a password manager.",
    keyName: "Key name",
    createKey: "Create API key",
    newSecret: "Copy this secret now",
    revoke: "Revoke",
    never: "Never",
    noAudit: "No authenticated changes have been recorded yet.",
    noTrash: "Trash is empty.",
    restore: "Restore",
    purge: "Run retention cleanup",
    purgeHint: "Permanently removes expired trash and audit rows. Active candidates stay safe.",
    purgeConfirm: "Permanently purge expired records? This cannot be undone.",
    backup: "Download core-data export (not restorable)",
    backupHint:
      "Includes organization, users, positions, candidates, and legacy scores. Disaster recovery is operated separately and restores the complete database and private-object store.",
    deletedCandidates: "Expired candidates",
    deletedPositions: "Expired positions",
    oldCandidates: "Old active candidates (preview only)",
    oldAudit: "Expired audit records",
    sso: "Single sign-on",
    unverifiedSso:
      "Production SSO is outside this pilot. Existing metadata is read-only and sign-in enforcement remains unavailable.",
    issuer: "Issuer URL",
    clientId: "Client ID",
    domains: "Allowed domains (comma-separated)",
    enabled: "Enable configuration",
    model: "Private model routing",
    provider: "Provider",
    baseUrl: "OpenAI-compatible base URL",
    modelName: "Model name",
    residency: "Data residency",
    managedDisclosure:
      "I accept that candidate data sent through managed OpenRouter may be processed outside the tenant's country. Human review remains required for hiring decisions.",
    privateModelHint:
      "Private endpoints must be operator-allowlisted. The server validates the endpoint before enabling it.",
    inheritedModelHint:
      "Uses the operator-selected private provider. Choose OpenRouter explicitly to accept managed cross-border processing.",
    status: "Status",
    contractUsage: "Contract usage",
    contractUsageHint:
      "Current resources come from live records; periodic activity comes from the append-only usage ledger.",
    technicalLimits: "Technical safety limits",
    limit: "Limit",
    used: "Used",
    remaining: "Remaining",
    unlimited: "Unlimited",
    currentState: "Current state",
    periodicCumulative: "Periodic cumulative",
    usageHistory: "Usage history",
    noUsage: "No usage events have been recorded yet.",
    loadMore: "Load more",
    loading: "Loading…",
    retry: "Retry",
    usageUnavailable: "Contract usage is temporarily unavailable.",
    usageLoadFailed: "Could not load more usage events.",
    occurredAt: "Time",
    event: "Event",
    resource: "Resource",
  },
  ru: {
    title: "Контроль и безопасность",
    subtitle: "Хранение данных, права, аудит, восстановление и приватная инфраструктура.",
    accessDenied: "Текущие права не разрешают доступ к этому разделу.",
    policy: "Политики",
    usage: "Лимиты и использование",
    access: "Доступ и API",
    audit: "Журнал аудита",
    recovery: "Восстановление",
    providers: "SSO и модели",
    save: "Сохранить",
    saved: "Изменения сохранены",
    failed: "Не удалось сохранить изменения",
    days: "дней",
    candidateRetention: "Хранение кандидатов",
    deletedRetention: "Хранение корзины",
    auditRetention: "Хранение аудита",
    exports: "Разрешить CSV-экспорт кандидатов",
    backups: "Разрешить переносимый экспорт основных данных",
    compensation: "Роли с доступом к компенсации",
    approvalSeparation: "Разделение обязанностей при согласовании",
    approvalSeparationHint:
      "Если параметр включён, автору требуется другой согласующий. Отключайте его только тогда, когда договор с организацией явно разрешает самосогласование.",
    separateRequisitions: "Другой согласующий для заявок на найм",
    separateOffers: "Другой согласующий для предложений кандидатам",
    separateHiringDecisions: "Другой согласующий для решений о найме",
    permissions: "Переопределения прав ролей",
    role: "Роль",
    permission: "Право",
    allow: "Разрешить",
    deny: "Запретить",
    addOverride: "Сохранить право",
    noOverrides: "Переопределений нет — действуют права роли.",
    apiKeys: "API-ключи",
    apiHint: "Секрет показывается один раз. Сохраните его в менеджере паролей.",
    keyName: "Название ключа",
    createKey: "Создать API-ключ",
    newSecret: "Скопируйте секрет сейчас",
    revoke: "Отозвать",
    never: "Никогда",
    noAudit: "Авторизованных изменений пока не было.",
    noTrash: "Корзина пуста.",
    restore: "Восстановить",
    purge: "Запустить очистку",
    purgeHint: "Навсегда удаляет просроченную корзину и аудит. Активные кандидаты не затрагиваются.",
    purgeConfirm: "Навсегда удалить просроченные записи? Это действие необратимо.",
    backup: "Скачать экспорт основных данных (без восстановления)",
    backupHint:
      "Включает организацию, пользователей, вакансии, кандидатов и прежние оценки. Полное аварийное восстановление выполняется отдельно для всей базы данных и приватного хранилища.",
    deletedCandidates: "Просроченные кандидаты",
    deletedPositions: "Просроченные вакансии",
    oldCandidates: "Старые активные кандидаты (только обзор)",
    oldAudit: "Просроченные записи аудита",
    sso: "Единый вход",
    unverifiedSso:
      "Промышленный SSO не входит в этот пилот. Существующие метаданные доступны только для чтения, принудительный вход отключён.",
    issuer: "URL издателя",
    clientId: "Client ID",
    domains: "Разрешённые домены (через запятую)",
    enabled: "Включить конфигурацию",
    model: "Маршрутизация приватной модели",
    provider: "Провайдер",
    baseUrl: "OpenAI-совместимый базовый URL",
    modelName: "Название модели",
    residency: "Размещение данных",
    managedDisclosure:
      "Я принимаю, что данные кандидатов при использовании управляемого OpenRouter могут обрабатываться за пределами страны организации. Решения о найме требуют участия человека.",
    privateModelHint:
      "Приватный адрес должен быть разрешён оператором. Сервер проверит его перед включением.",
    inheritedModelHint:
      "Используется приватный провайдер оператора. Выберите OpenRouter явно, чтобы принять трансграничную обработку.",
    status: "Статус",
    contractUsage: "Использование по договору",
    contractUsageHint:
      "Текущие ресурсы считаются по активным записям, периодические операции — по неизменяемому журналу использования.",
    technicalLimits: "Технические ограничения безопасности",
    limit: "Лимит",
    used: "Использовано",
    remaining: "Осталось",
    unlimited: "Без ограничений",
    currentState: "Текущее состояние",
    periodicCumulative: "Накопление за период",
    usageHistory: "История использования",
    noUsage: "Событий использования пока нет.",
    loadMore: "Показать ещё",
    loading: "Загрузка…",
    retry: "Повторить",
    usageUnavailable: "Данные по договорным лимитам временно недоступны.",
    usageLoadFailed: "Не удалось загрузить следующие события.",
    occurredAt: "Время",
    event: "Событие",
    resource: "Ресурс",
  },
};

const PERMISSIONS = [
  "account.read",
  "account.manage",
  "candidates.read",
  "candidates.write",
  "candidates.contact",
  "candidates.documents",
  "candidates.export",
  "applications.read",
  "applications.write",
  "applications.disposition",
  "assessments.read",
  "assessments.manage",
  "assessments.grade",
  "assessments.results",
  "interviews.read",
  "interviews.manage",
  "scorecards.submit",
  "requisitions.read",
  "requisitions.manage",
  "requisitions.approve",
  "requisitions.compensation",
  "offers.read",
  "offers.manage",
  "offers.compensation",
  "communications.read",
  "communications.manage",
  "communications.send",
  "scheduling.read",
  "scheduling.manage",
  "workflows.read",
  "workflows.manage",
  "workflows.execute",
  "analytics.read",
  "analytics.export",
  "ai.use",
  "ai.manage",
  "ai.managed_provider",
  "ai.sensitive_context",
  "integrations.read",
  "integrations.manage",
  "integrations.credentials",
  "sourcing.read",
  "sourcing.manage",
  "sourcing.publish",
  "talent.read",
  "talent.manage",
  "enterprise.read",
  "enterprise.manage",
  "enterprise.audit_export",
  "enterprise.backup_export",
];

/** @ui-only Permission-derived controls passed from the server page. */
export type EnterpriseControlAccess = {
  viewPolicy: boolean;
  managePolicy: boolean;
  managePermissions: boolean;
  viewAudit: boolean;
  manageRecovery: boolean;
  downloadBackup: boolean;
  viewApiKeys: boolean;
  manageApiKeys: boolean;
  viewSso: boolean;
  manageSso: boolean;
  viewModel: boolean;
  manageModel: boolean;
  viewEntitlements: boolean;
  viewUsage: boolean;
};

export type EnterpriseControlsProps = {
  access: EnterpriseControlAccess;
  initialPolicy: Policy | null;
  initialOverrides: PermissionOverride[];
  initialAudit: AuditPage;
  initialTrash: TrashPage;
  initialRetention: Retention | null;
  initialApiKeys: ApiKey[];
  initialSso: SsoConfig | null;
  initialModelConfig: ModelConfig | null;
  initialEntitlements: Entitlements | null;
  initialUsage: UsagePage | null;
};

export function EnterpriseControls({
  access,
  initialPolicy,
  initialOverrides,
  initialAudit,
  initialTrash,
  initialRetention,
  initialApiKeys,
  initialSso,
  initialModelConfig,
  initialEntitlements,
  initialUsage,
}: EnterpriseControlsProps) {
  const { locale } = useI18n();
  const t = COPY[locale];
  const router = useRouter();
  const tabs: { id: Tab; label: string; icon: React.ReactNode }[] = [
    ...(access.viewPolicy
      ? [{ id: "policy" as const, label: t.policy, icon: <ShieldCheck className="h-4 w-4" /> }]
      : []),
    ...(access.viewEntitlements
      ? [{ id: "usage" as const, label: t.usage, icon: <Gauge className="h-4 w-4" /> }]
      : []),
    ...(access.managePermissions || access.viewApiKeys || access.manageApiKeys
      ? [{ id: "access" as const, label: t.access, icon: <KeyRound className="h-4 w-4" /> }]
      : []),
    ...(access.viewAudit
      ? [{ id: "audit" as const, label: t.audit, icon: <FileClock className="h-4 w-4" /> }]
      : []),
    ...(access.manageRecovery || access.downloadBackup
      ? [{ id: "recovery" as const, label: t.recovery, icon: <ArchiveRestore className="h-4 w-4" /> }]
      : []),
    ...(access.viewSso || access.manageSso || access.viewModel || access.manageModel
      ? [{ id: "providers" as const, label: t.providers, icon: <Bot className="h-4 w-4" /> }]
      : []),
  ];
  const [tab, setTab] = useState<Tab>(tabs[0]?.id ?? "policy");
  const [pending, startTransition] = useTransition();
  const [apiSecret, setApiSecret] = useState<string | null>(null);
  const activeTab = tabs.some((item) => item.id === tab) ? tab : tabs[0]?.id;

  function run(action: () => Promise<{ ok: boolean; error?: string }>) {
    startTransition(async () => {
      const result = await action();
      if (result.ok) {
        toast.success(t.saved);
        router.refresh();
      } else toast.error(result.error || t.failed);
    });
  }

  if (!activeTab) {
    return (
      <div className="mx-auto mt-16 max-w-xl rounded-xl border border-border bg-bg-elevated p-8 text-center">
        <ShieldCheck className="mx-auto mb-3 h-8 w-8 text-text-muted" />
        <h1 className="text-xl font-semibold">{t.title}</h1>
        <p className="mt-2 text-sm text-text-muted">{t.accessDenied}</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-6">
        <h1 className="text-[24px] font-semibold tracking-[-0.025em]">{t.title}</h1>
        <p className="mt-1 text-sm text-text-muted">{t.subtitle}</p>
      </header>
      <div className="mb-6 flex gap-1 overflow-x-auto border-b border-border">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id)}
            className={cn(
              "-mb-px flex items-center gap-2 whitespace-nowrap border-b px-3 py-3 text-xs font-medium",
              activeTab === item.id ? "border-text text-text" : "border-transparent text-text-muted",
            )}
          >
            {item.icon}
            {item.label}
          </button>
        ))}
      </div>

      {activeTab === "policy" && initialPolicy ? (
        <PolicyPanel
          policy={initialPolicy}
          canManage={access.managePolicy}
          pending={pending}
          locale={locale}
          t={t}
          onSave={(body) => run(() => updateEnterprisePolicyAction(body))}
        />
      ) : null}
      {activeTab === "usage" ? (
        initialEntitlements ? (
          <UsagePanel
            entitlements={initialEntitlements}
            initialUsage={initialUsage}
            canViewHistory={access.viewUsage}
            locale={locale}
            t={t}
          />
        ) : (
          <Card title={t.contractUsage} icon={<Gauge className="h-4 w-4" />}>
            <div className="flex items-center gap-3" role="alert">
              <p className="text-sm text-red-600">{t.usageUnavailable}</p>
              <button
                type="button"
                onClick={() => router.refresh()}
                className="text-xs font-medium underline"
              >
                {t.retry}
              </button>
            </div>
          </Card>
        )
      ) : null}
      {activeTab === "access" ? (
        <div className="grid gap-5 lg:grid-cols-2">
          {access.managePermissions ? (
            <PermissionsPanel
              overrides={initialOverrides}
              pending={pending}
              locale={locale}
              t={t}
              run={run}
            />
          ) : null}
          {access.viewApiKeys || access.manageApiKeys ? (
            <ApiKeysPanel
              keys={initialApiKeys}
              canManage={access.manageApiKeys}
              pending={pending}
              secret={apiSecret}
              setSecret={setApiSecret}
              locale={locale}
              t={t}
              run={run}
              refresh={() => router.refresh()}
            />
          ) : null}
        </div>
      ) : null}
      {activeTab === "audit" ? (
        <AuditPanel items={initialAudit.items} locale={locale} t={t} />
      ) : null}
      {activeTab === "recovery" ? (
        <RecoveryPanel
          trash={initialTrash.items}
          retention={initialRetention}
          pending={pending}
          locale={locale}
          t={t}
          run={run}
          canBackup={access.downloadBackup}
          canManage={access.manageRecovery}
        />
      ) : null}
      {activeTab === "providers" ? (
        <ProvidersPanel
          sso={initialSso}
          model={initialModelConfig}
          access={access}
          pending={pending}
          locale={locale}
          t={t}
          run={run}
        />
      ) : null}
    </div>
  );
}

/** @ui-only Localized presentation-copy projection. */
type T = { [K in keyof (typeof COPY)["en"]]: string };

const METRIC_LABELS: Record<"en" | "ru", Record<EntitlementMetric, string>> = {
  en: {
    seats: "Enabled seats",
    active_jobs: "Open jobs",
    candidates: "Live candidates",
    candidate_import_rows: "Imported rows",
    assessment_starts: "Assessment starts",
    ai_cost_microusd: "AI cost",
    ai_tokens: "AI tokens",
    storage_bytes: "Private storage",
    integration_runs: "Integration runs",
  },
  ru: {
    seats: "Активные пользователи",
    active_jobs: "Открытые вакансии",
    candidates: "Активные кандидаты",
    candidate_import_rows: "Строки импорта",
    assessment_starts: "Запуски оценок",
    ai_cost_microusd: "Расходы ИИ",
    ai_tokens: "Токены ИИ",
    storage_bytes: "Приватное хранилище",
    integration_runs: "Запуски интеграций",
  },
};

const TECHNICAL_LABELS: Record<"en" | "ru", Record<string, string>> = {
  en: {
    candidate_document_bytes: "Candidate document",
    sourcing_document_bytes: "Sourcing file",
    synchronous_import_rows: "Rows per synchronous import",
    synchronous_recipients: "Recipients per synchronous send",
  },
  ru: {
    candidate_document_bytes: "Документ кандидата",
    sourcing_document_bytes: "Файл источника",
    synchronous_import_rows: "Строк за синхронный импорт",
    synchronous_recipients: "Получателей за синхронную отправку",
  },
};

function formatUsageValue(
  metric: string,
  value: number,
  locale: "en" | "ru",
): string {
  if (metric === "storage_bytes" || metric.endsWith("_document_bytes")) {
    const units = ["B", "KiB", "MiB", "GiB"];
    const sign = value < 0 ? -1 : 1;
    let amount = Math.abs(value);
    let unit = 0;
    while (amount >= 1024 && unit < units.length - 1) {
      amount /= 1024;
      unit += 1;
    }
    return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(sign * amount)} ${units[unit]}`;
  }
  if (metric === "ai_cost_microusd") {
    return `${new Intl.NumberFormat(locale, {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 4,
    }).format(value / 1_000_000)}`;
  }
  return new Intl.NumberFormat(locale).format(value);
}

function UsagePanel({
  entitlements,
  initialUsage,
  canViewHistory,
  locale,
  t,
}: {
  entitlements: Entitlements;
  initialUsage: UsagePage | null;
  canViewHistory: boolean;
  locale: "en" | "ru";
  t: T;
}) {
  const [items, setItems] = useState(initialUsage?.items ?? []);
  const [cursor, setCursor] = useState(initialUsage?.next_cursor ?? null);
  const [hasLoadedInitial, setHasLoadedInitial] = useState(initialUsage !== null);
  const [loadState, setLoadState] = useState<"idle" | "loading" | "error">(
    initialUsage ? "idle" : "error",
  );

  async function loadMore() {
    if (loadState === "loading" || (hasLoadedInitial && !cursor)) return;
    setLoadState("loading");
    const result = await loadUsagePageAction(hasLoadedInitial ? cursor : null);
    if (!result.ok) {
      setLoadState("error");
      return;
    }
    const page = result.data as UsagePage;
    setItems((current) => {
      const seen = new Set(current.map((item) => item.id));
      return [...current, ...page.items.filter((item) => !seen.has(item.id))];
    });
    setCursor(page.next_cursor);
    setHasLoadedInitial(true);
    setLoadState("idle");
  }

  return (
    <div className="space-y-5">
      <Card title={t.contractUsage} icon={<Gauge className="h-4 w-4" />}>
        <p className="mb-4 text-xs leading-5 text-text-muted">{t.contractUsageHint}</p>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {entitlements.entitlements.map((item) => (
            <article key={item.metric} className="rounded-lg border border-border bg-bg p-4">
              <div className="flex items-start justify-between gap-3">
                <h3 className="text-sm font-medium">{METRIC_LABELS[locale][item.metric]}</h3>
                <span className="rounded bg-bg-elevated px-2 py-1 text-[10px] text-text-muted">
                  {item.basis === "current_state" ? t.currentState : t.periodicCumulative}
                </span>
              </div>
              <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
                <div>
                  <dt className="text-text-muted">{t.used}</dt>
                  <dd className="mt-1 font-mono font-semibold">
                    {formatUsageValue(item.metric, item.used, locale)}
                  </dd>
                </div>
                <div>
                  <dt className="text-text-muted">{t.limit}</dt>
                  <dd className="mt-1 font-mono font-semibold">
                    {item.limit === null
                      ? t.unlimited
                      : formatUsageValue(item.metric, item.limit, locale)}
                  </dd>
                </div>
                <div>
                  <dt className="text-text-muted">{t.remaining}</dt>
                  <dd className="mt-1 font-mono font-semibold">
                    {item.remaining === null
                      ? t.unlimited
                      : formatUsageValue(item.metric, item.remaining, locale)}
                  </dd>
                </div>
              </dl>
              <p className="mt-3 text-[10px] text-text-faint">{machineLabel(item.period, locale)}</p>
            </article>
          ))}
        </div>
      </Card>

      <Card title={t.technicalLimits} icon={<ShieldCheck className="h-4 w-4" />}>
        <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Object.entries(entitlements.technical_limits).map(([metric, value]) => (
            <div key={metric} className="rounded-lg border border-border bg-bg p-3">
              <dt className="text-xs text-text-muted">
                {TECHNICAL_LABELS[locale][metric] ?? machineLabel(metric, locale)}
              </dt>
              <dd className="mt-1 font-mono text-lg font-semibold">
                {formatUsageValue(metric, value, locale)}
              </dd>
            </div>
          ))}
        </dl>
      </Card>

      {canViewHistory ? (
        <Card title={t.usageHistory} icon={<FileClock className="h-4 w-4" />}>
          {items.length === 0 ? (
            <p className="text-sm text-text-muted">{t.noUsage}</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-text-muted">
                  <tr>
                    <th className="pb-3">{t.occurredAt}</th>
                    <th className="pb-3">{t.event}</th>
                    <th className="pb-3">{t.used}</th>
                    <th className="pb-3">{t.resource}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {items.map((item) => (
                    <tr key={item.id}>
                      <td className="whitespace-nowrap py-3 pr-4">
                        {new Intl.DateTimeFormat(locale, {
                          dateStyle: "medium",
                          timeStyle: "short",
                        }).format(new Date(item.occurred_at))}
                      </td>
                      <td className="py-3 pr-4">
                        <span className="font-medium">{METRIC_LABELS[locale][item.metric]}</span>
                        <span className="ml-2 text-text-muted">{machineLabel(item.kind, locale)}</span>
                      </td>
                      <td className={`py-3 pr-4 font-mono ${item.amount < 0 ? "text-emerald-600" : ""}`}>
                        {item.amount > 0 ? "+" : ""}
                        {formatUsageValue(item.metric, item.amount, locale)}
                      </td>
                      <td className="py-3 text-text-muted">
                        {item.resource_type ? machineLabel(item.resource_type, locale) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {loadState === "error" ? (
            <div className="mt-4 flex items-center gap-3" role="alert">
              <p className="text-xs text-red-600">
                {hasLoadedInitial ? t.usageLoadFailed : t.usageUnavailable}
              </p>
              <button type="button" onClick={loadMore} className="text-xs font-medium underline">
                {t.retry}
              </button>
            </div>
          ) : cursor ? (
            <button
              type="button"
              disabled={loadState === "loading"}
              onClick={loadMore}
              className="mt-4 rounded-md border border-border px-3 py-2 text-xs font-medium disabled:opacity-50"
            >
              {loadState === "loading" ? t.loading : t.loadMore}
            </button>
          ) : null}
        </Card>
      ) : null}
    </div>
  );
}

function PolicyPanel({
  policy,
  canManage,
  pending,
  locale,
  t,
  onSave,
}: {
  policy: Policy;
  canManage: boolean;
  pending: boolean;
  locale: "en" | "ru";
  t: T;
  onSave: (body: unknown) => void;
}) {
  return (
    <Card title={t.policy} icon={<ShieldCheck className="h-4 w-4" />}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (!canManage) return;
          const data = new FormData(event.currentTarget);
          onSave({
            candidate_retention_days: Number(data.get("candidate")),
            deleted_retention_days: Number(data.get("deleted")),
            audit_retention_days: Number(data.get("audit")),
            candidate_export_enabled: data.get("exports") === "on",
            backup_export_enabled: data.get("backups") === "on",
            approval_separation_of_duties: {
              requisitions: data.get("separate-requisitions") === "on",
              offers: data.get("separate-offers") === "on",
              hiring_decisions: data.get("separate-hiring-decisions") === "on",
            },
            field_visibility: {
              ...policy.field_visibility,
              compensation: data.getAll("compensation"),
            },
          });
        }}
      >
        <fieldset disabled={!canManage} className="grid gap-5 disabled:opacity-75 md:grid-cols-3">
          <NumberField name="candidate" label={t.candidateRetention} value={policy.candidate_retention_days} min={30} max={730} suffix={t.days} />
          <NumberField name="deleted" label={t.deletedRetention} value={policy.deleted_retention_days} min={1} max={3650} suffix={t.days} />
          <NumberField name="audit" label={t.auditRetention} value={policy.audit_retention_days} min={90} max={36500} suffix={t.days} />
          <label className="flex items-center gap-2 text-sm"><input name="exports" type="checkbox" defaultChecked={policy.candidate_export_enabled} />{t.exports}</label>
          <label className="flex items-center gap-2 text-sm"><input name="backups" type="checkbox" defaultChecked={policy.backup_export_enabled} />{t.backups}</label>
          <fieldset className="rounded-lg border border-border p-3 md:col-span-3">
            <legend className="px-1 text-sm font-medium text-text">
              {t.approvalSeparation}
            </legend>
            <p className="mb-3 text-xs text-text-muted">{t.approvalSeparationHint}</p>
            <div className="grid gap-2 md:grid-cols-3">
              <label className="flex items-start gap-2 text-sm">
                <input
                  name="separate-requisitions"
                  type="checkbox"
                  defaultChecked={policy.approval_separation_of_duties?.requisitions ?? true}
                />
                {t.separateRequisitions}
              </label>
              <label className="flex items-start gap-2 text-sm">
                <input
                  name="separate-offers"
                  type="checkbox"
                  defaultChecked={policy.approval_separation_of_duties?.offers ?? true}
                />
                {t.separateOffers}
              </label>
              <label className="flex items-start gap-2 text-sm">
                <input
                  name="separate-hiring-decisions"
                  type="checkbox"
                  defaultChecked={policy.approval_separation_of_duties?.hiring_decisions ?? true}
                />
                {t.separateHiringDecisions}
              </label>
            </div>
          </fieldset>
          <div className="rounded-lg border border-border p-3">
            <p className="mb-2 text-xs text-text-muted">{t.compensation}</p>
            {["admin", "hiring_manager", "recruiter", "interviewer", "restricted"].map((role) => (
              <label key={role} className="mr-4 inline-flex items-center gap-1.5 text-xs">
                <input name="compensation" value={role} type="checkbox" defaultChecked={(policy.field_visibility.compensation || []).includes(role)} />
                {machineLabel(role, locale)}
              </label>
            ))}
          </div>
          {canManage ? <div className="md:col-span-3"><PrimaryButton pending={pending}>{t.save}</PrimaryButton></div> : null}
        </fieldset>
      </form>
    </Card>
  );
}

function PermissionsPanel({ overrides, pending, locale, t, run }: { overrides: PermissionOverride[]; pending: boolean; locale: "en" | "ru"; t: T; run: (action: () => Promise<{ ok: boolean; error?: string }>) => void }) {
  return (
    <Card title={t.permissions} icon={<LockKeyhole className="h-4 w-4" />}>
      <form
        className="grid grid-cols-[1fr_1.4fr_auto] gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          run(() => putPermissionOverrideAction({ role: data.get("role"), permission: data.get("permission"), allowed: data.get("allowed") === "true" }));
        }}
      >
        <Select name="role" ariaLabel={t.role} options={["admin", "recruiter", "hiring_manager", "interviewer", "restricted"]} locale={locale} />
        <Select name="permission" ariaLabel={t.permission} options={PERMISSIONS} locale={locale} />
        <select name="allowed" className="rounded-md border border-border bg-bg px-2 text-xs"><option value="true">{t.allow}</option><option value="false">{t.deny}</option></select>
        <div className="col-span-3"><PrimaryButton pending={pending}>{t.addOverride}</PrimaryButton></div>
      </form>
      <div className="mt-4 divide-y divide-border border-t border-border">
        {overrides.length === 0 ? <p className="py-4 text-xs text-text-muted">{t.noOverrides}</p> : overrides.map((item) => (
          <div key={item.id} className="flex items-center gap-2 py-2 text-xs">
            <span className="rounded bg-bg px-2 py-1">{machineLabel(item.role, locale)}</span>
            <span className="min-w-0 flex-1 truncate">{machineLabel(item.permission, locale)}</span>
            <span className={item.allowed ? "text-emerald-600" : "text-red-600"}>{item.allowed ? t.allow : t.deny}</span>
            <button disabled={pending} onClick={() => run(() => deletePermissionOverrideAction(item.id))} className="p-1 text-text-muted hover:text-red-600" aria-label={locale === "ru" ? "Удалить" : "Delete"}><Trash2 className="h-3.5 w-3.5" /></button>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ApiKeysPanel({ keys, canManage, pending, secret, setSecret, locale, t, run, refresh }: { keys: ApiKey[]; canManage: boolean; pending: boolean; secret: string | null; setSecret: (value: string | null) => void; locale: "en" | "ru"; t: T; run: (action: () => Promise<{ ok: boolean; error?: string }>) => void; refresh: () => void }) {
  return (
    <Card title={t.apiKeys} icon={<KeyRound className="h-4 w-4" />}>
      <p className="mb-3 text-xs text-text-muted">{t.apiHint}</p>
      {canManage ? <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const name = String(new FormData(event.currentTarget).get("name") || "");
          startCreate();
          async function startCreate() {
            const result = await createApiKeyAction({ name, scopes: ["candidates:read", "positions:read"] });
            if (result.ok) { setSecret(result.data.secret); toast.success(t.saved); refresh(); }
            else toast.error(result.error);
          }
        }}
      >
        <input name="name" required maxLength={160} placeholder={t.keyName} className="min-w-0 flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm" />
        <PrimaryButton pending={pending}>{t.createKey}</PrimaryButton>
      </form> : null}
      {secret ? (
        <div className="mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-amber-950 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-100">
          <p className="mb-1 text-xs font-semibold">{t.newSecret}</p>
          <code className="block break-all text-xs">{secret}</code>
          <button type="button" className="mt-2 text-xs underline" onClick={() => navigator.clipboard.writeText(secret)}>{locale === "ru" ? "Копировать" : "Copy"}</button>
        </div>
      ) : null}
      <div className="mt-4 divide-y divide-border">
        {keys.map((item) => (
          <div key={item.id} className="flex items-center gap-3 py-3 text-xs">
            <div className="min-w-0 flex-1"><p className="truncate font-medium">{item.name}</p><p className="text-text-muted"><span className="font-mono">{item.key_prefix}…</span> · {item.scopes.map((scope) => machineLabel(scope, locale)).join(", ")}</p></div>
            {item.revoked_at ? <span className="text-text-faint">{t.revoke}</span> : canManage ? <button type="button" onClick={() => run(() => revokeApiKeyAction(item.id))} className="text-red-600 hover:underline">{t.revoke}</button> : null}
          </div>
        ))}
      </div>
    </Card>
  );
}

function AuditPanel({ items, locale, t }: { items: AuditItem[]; locale: "en" | "ru"; t: T }) {
  return (
    <Card title={t.audit} icon={<FileClock className="h-4 w-4" />}>
      {items.length === 0 ? <p className="text-sm text-text-muted">{t.noAudit}</p> : (
        <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-text-muted"><tr><th className="pb-3">{t.occurredAt}</th><th className="pb-3">{locale === "ru" ? "Инициатор" : "Actor"}</th><th className="pb-3">{t.event}</th><th className="pb-3">{t.resource}</th><th className="pb-3">HTTP</th></tr></thead><tbody className="divide-y divide-border">{items.map((item) => <tr key={item.id}><td className="whitespace-nowrap py-3 pr-4">{new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(item.occurred_at))}</td><td className="max-w-44 truncate pr-4">{item.actor.email || (locale === "ru" ? "Система" : "System")}</td><td className="pr-4">{machineLabel(item.action, locale)}</td><td className="pr-4 text-text-muted">{item.resource_type ? machineLabel(item.resource_type, locale) : "—"}</td><td><span className={item.status_code < 400 ? "text-emerald-600" : "text-red-600"}>{item.method} {item.status_code}</span></td></tr>)}</tbody></table></div>
      )}
    </Card>
  );
}

function RecoveryPanel({ trash, retention, pending, locale, t, run, canBackup, canManage }: { trash: TrashItem[]; retention: Retention | null; pending: boolean; locale: "en" | "ru"; t: T; run: (action: () => Promise<{ ok: boolean; error?: string }>) => void; canBackup: boolean; canManage: boolean }) {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card title={t.recovery} icon={<ArchiveRestore className="h-4 w-4" />}>
        {trash.length === 0 ? <p className="text-sm text-text-muted">{t.noTrash}</p> : trash.map((item) => <div key={`${item.resource_type}-${item.id}`} className="flex items-center gap-3 border-b border-border py-3 text-xs"><div className="min-w-0 flex-1"><p className="truncate font-medium">{item.label}</p><p className="text-text-muted">{machineLabel(item.resource_type, locale)} · {new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(item.deleted_at))}</p></div><button disabled={pending} onClick={() => run(() => restoreTrashAction(item.resource_type, item.id))} className="text-accent hover:underline">{t.restore}</button></div>)}
      </Card>
      <Card title={t.policy} icon={<DatabaseBackup className="h-4 w-4" />}>
        {retention ? <dl className="grid grid-cols-2 gap-3">{[[t.deletedCandidates, retention.deleted_candidates], [t.deletedPositions, retention.deleted_positions], [t.oldCandidates, retention.old_active_candidates], [t.oldAudit, retention.expired_audit_logs]].map(([label, value]) => <div key={String(label)} className="rounded-lg border border-border bg-bg p-3"><dt className="text-xs text-text-muted">{label}</dt><dd className="mt-1 font-mono text-xl">{value}</dd></div>)}</dl> : null}
        <p className="mt-4 text-xs text-text-muted">{t.purgeHint}</p>
        {canBackup ? <p className="mt-2 text-xs text-text-muted">{t.backupHint}</p> : null}
        <div className="mt-3 flex flex-wrap gap-2">{canManage ? <button type="button" disabled={pending} onClick={() => { if (window.confirm(t.purgeConfirm)) run(() => runRetentionAction(false)); }} className="rounded-md border border-red-300 px-3 py-2 text-xs font-medium text-red-600 hover:bg-red-50 dark:border-red-900 dark:hover:bg-red-950/30">{t.purge}</button> : null}{canBackup ? <a href="/api/enterprise/backup" className="rounded-md border border-border px-3 py-2 text-xs font-medium hover:bg-bg">{t.backup}</a> : null}</div>
      </Card>
    </div>
  );
}

function ProvidersPanel({ sso, model, access, pending, locale, t, run }: { sso: SsoConfig | null; model: ModelConfig | null; access: EnterpriseControlAccess; pending: boolean; locale: "en" | "ru"; t: T; run: (action: () => Promise<{ ok: boolean; error?: string }>) => void }) {
  const [modelProvider, setModelProvider] = useState<ModelConfig["provider"]>(
    model?.provider ?? "inherit",
  );
  const privateProvider = ["openai_compatible", "vllm", "ollama"].includes(
    modelProvider,
  );
  const managedAcceptanceActive = Boolean(
    model?.managed_processing_accepted_at &&
      !model?.managed_processing_revoked_at,
  );
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      {access.viewSso || access.manageSso ? <Card title={t.sso} icon={<LockKeyhole className="h-4 w-4" />}>
        <p className="mb-4 text-xs text-text-muted">{t.unverifiedSso}</p>
        {sso ? (
          <dl className="space-y-2 rounded-lg border border-border bg-bg p-3 text-xs">
            <div className="flex gap-3"><dt className="min-w-24 text-text-muted">{t.provider}</dt><dd>{machineLabel(sso.provider, locale)}</dd></div>
            <div className="flex gap-3"><dt className="min-w-24 text-text-muted">{t.issuer}</dt><dd className="min-w-0 break-all">{sso.issuer_url}</dd></div>
            <div className="flex gap-3"><dt className="min-w-24 text-text-muted">{t.domains}</dt><dd>{sso.allowed_domains.join(", ") || "—"}</dd></div>
            <div className="flex gap-3"><dt className="min-w-24 text-text-muted">{t.status}</dt><dd><Status value={sso.status} locale={locale} /></dd></div>
          </dl>
        ) : (
          <p className="text-xs text-text-muted">{t.status}: <Status value="unavailable" locale={locale} /></p>
        )}
      </Card> : null}
      {access.viewModel || access.manageModel ? <Card title={t.model} icon={<Bot className="h-4 w-4" />}>
        <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); if (!access.manageModel) return; const data = new FormData(event.currentTarget); const isPrivate = ["openai_compatible", "vllm", "ollama"].includes(modelProvider); run(() => updateModelConfigurationAction({ provider: modelProvider, base_url: isPrivate ? String(data.get("url") || "") || null : null, model: isPrivate ? String(data.get("model") || "") || null : null, data_residency: modelProvider === "openrouter" || modelProvider === "inherit" ? "provider" : data.get("residency"), managed_processing_accepted: modelProvider === "openrouter" && data.get("managed_processing_accepted") === "on" })); }}>
          <fieldset disabled={!access.manageModel} className="space-y-3 disabled:opacity-75">
            <select
              name="provider"
              aria-label={t.provider}
              value={modelProvider}
              onChange={(event) => setModelProvider(event.target.value as ModelConfig["provider"])}
              className="min-w-0 rounded-md border border-border bg-bg px-2 py-2 text-xs"
            >
              {["inherit", "openrouter", "openai_compatible", "vllm", "ollama"].map((item) => <option key={item} value={item}>{machineLabel(item, locale)}</option>)}
            </select>
            {privateProvider ? (
              <>
                <p className="text-xs leading-5 text-text-muted">{t.privateModelHint}</p>
                <TextField name="url" label={t.baseUrl} value={model?.base_url || ""} required />
                <TextField name="model" label={t.modelName} value={model?.model || ""} required />
                <Select
                  name="residency"
                  ariaLabel={t.residency}
                  options={["private_cloud", "on_prem"]}
                  defaultValue={model?.data_residency === "on_prem" ? "on_prem" : "private_cloud"}
                  locale={locale}
                />
              </>
            ) : modelProvider === "openrouter" ? (
              <label className="flex items-start gap-2 rounded-lg border border-amber-300/60 bg-amber-50 p-3 text-xs leading-5 text-amber-950 dark:bg-amber-950/20 dark:text-amber-100">
                <input
                  name="managed_processing_accepted"
                  type="checkbox"
                  required
                  defaultChecked={managedAcceptanceActive}
                  className="mt-1"
                />
                <span>{t.managedDisclosure}</span>
              </label>
            ) : (
              <p className="text-xs leading-5 text-text-muted">{t.inheritedModelHint}</p>
            )}
            {model ? <p className="text-xs text-text-muted">{t.status}: <Status value={model.status} locale={locale} /></p> : null}
            {access.manageModel ? <PrimaryButton pending={pending}>{t.save}</PrimaryButton> : null}
          </fieldset>
        </form>
      </Card> : null}
    </div>
  );
}

function Card({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) { return <section className="rounded-xl border border-border bg-bg-elevated p-5"><h2 className="mb-4 flex items-center gap-2 text-sm font-semibold">{icon}{title}</h2>{children}</section>; }
function PrimaryButton({ pending, children }: { pending: boolean; children: React.ReactNode }) { return <button disabled={pending} className="rounded-md bg-accent px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">{children}</button>; }
function NumberField({ name, label, value, min, max, suffix }: { name: string; label: string; value: number; min: number; max: number; suffix: string }) { return <label className="text-xs text-text-muted">{label}<div className="mt-1 flex items-center rounded-md border border-border bg-bg"><input name={name} type="number" defaultValue={value} min={min} max={max} required className="min-w-0 flex-1 bg-transparent px-3 py-2 text-sm text-text outline-none" /><span className="pr-3">{suffix}</span></div></label>; }
function TextField({ name, label, value, required = false }: { name: string; label: string; value: string; required?: boolean }) { return <label className="block text-xs text-text-muted">{label}<input name={name} defaultValue={value} required={required} className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text" /></label>; }
function Select({ name, ariaLabel, options, defaultValue, locale }: { name: string; ariaLabel: string; options: string[]; defaultValue?: string; locale: "en" | "ru" }) { return <select name={name} aria-label={ariaLabel} defaultValue={defaultValue} className="min-w-0 rounded-md border border-border bg-bg px-2 py-2 text-xs">{options.map((item) => <option key={item} value={item}>{machineLabel(item, locale)}</option>)}</select>; }
function Status({ value, locale }: { value: string; locale: "en" | "ru" }) { return <span className={cn("rounded px-1.5 py-0.5", value === "ready" ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950" : "bg-amber-100 text-amber-700 dark:bg-amber-950")}>{machineLabel(value, locale)}</span>; }
