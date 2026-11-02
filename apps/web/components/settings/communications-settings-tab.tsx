"use client";

import { useEffect, useMemo, useState, useTransition } from "react";
import {
  Check,
  Clipboard,
  KeyRound,
  Loader2,
  Mail,
  MessageCircle,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  createCommunicationAccountAction,
  createCommunicationTemplateAction,
  listCommunicationConnectionsAction,
  rotateCommunicationWebhookSecretAction,
  updateCommunicationTemplateAction,
} from "@/lib/actions";
import type {
  CommunicationAccount,
  CommunicationConnectionOption,
  CommunicationTemplate,
  OutboundMessageChannel,
} from "@/lib/types";
import { isCommunicationAccountAvailable } from "@/lib/communication-capabilities";
import { cn } from "@/lib/utils";

const controlClass =
  "w-full rounded-[6px] border border-border bg-bg px-3 py-2 text-[12px] text-text outline-none transition-colors focus:border-border-strong";

/** @ui-only Ephemeral rotated-secret display state. */
type SecretState = { accountId: string; value: string } | null;
/** @ui-only Permission-derived communication settings controls. */
type CommunicationsSettingsAccess = {
  viewAccounts: boolean;
  manageAccounts: boolean;
  rotateCredentials: boolean;
  viewTemplates: boolean;
  manageTemplates: boolean;
};

export function CommunicationsSettingsTab({
  initialAccounts,
  initialTemplates,
  initialLoadFailed,
  webhookBaseUrl,
  access,
}: {
  initialAccounts: CommunicationAccount[];
  initialTemplates: CommunicationTemplate[];
  initialLoadFailed: boolean;
  webhookBaseUrl: string;
  access: CommunicationsSettingsAccess;
}) {
  const { m } = useI18n();
  const copy = m.communications;
  const [accounts, setAccounts] = useState(initialAccounts);
  const [templates, setTemplates] = useState(initialTemplates);
  const [accountChannel, setAccountChannel] =
    useState<OutboundMessageChannel>("email");
  const [accountBinding, setAccountBinding] = useState("sandbox");
  const [connections, setConnections] = useState<CommunicationConnectionOption[]>([]);
  const [connectionsFailed, setConnectionsFailed] = useState(false);
  const [accountName, setAccountName] = useState("");
  const [senderLabel, setSenderLabel] = useState("");
  const [secret, setSecret] = useState<SecretState>(null);
  const [accountPending, startAccount] = useTransition();
  const [rotatingId, setRotatingId] = useState<string | null>(null);

  const [templateId, setTemplateId] = useState<string | null>(null);
  const [templateName, setTemplateName] = useState("");
  const [templateChannel, setTemplateChannel] =
    useState<OutboundMessageChannel>("email");
  const [templateLanguage, setTemplateLanguage] = useState("ru");
  const [templateSubject, setTemplateSubject] = useState("");
  const [templateBody, setTemplateBody] = useState("");
  const [templateActive, setTemplateActive] = useState(true);
  const [templatePending, startTemplate] = useTransition();

  const compatibleConnections = useMemo(
    () =>
      connections.filter((connection) => {
        if (connection.status !== "connected") return false;
        if (accountChannel === "email") {
          return (
            ["google_workspace", "microsoft_365"].includes(connection.provider) &&
            connection.capabilities.includes("email.send")
          );
        }
        return (
          connection.provider === "telegram" &&
          connection.capabilities.includes("message.telegram.send")
        );
      }),
    [accountChannel, connections],
  );

  useEffect(() => {
    if (!access.viewAccounts) return;
    let active = true;
    void listCommunicationConnectionsAction().then((result) => {
      if (!active) return;
      if (!result.ok) {
        setConnectionsFailed(true);
        return;
      }
      setConnections(result.data);
      setConnectionsFailed(false);
    });
    return () => {
      active = false;
    };
  }, [access.viewAccounts]);

  useEffect(() => {
    if (
      accountBinding !== "sandbox" &&
      !compatibleConnections.some((connection) => connection.id === accountBinding)
    ) {
      setAccountBinding("sandbox");
    }
  }, [accountBinding, compatibleConnections]);

  function createAccount(event: React.FormEvent) {
    event.preventDefault();
    if (!access.manageAccounts) return;
    const name = accountName.trim();
    if (!name || accountPending) return;
    const selectedConnection = compatibleConnections.find(
      (connection) => connection.id === accountBinding,
    );
    startAccount(async () => {
      const result = await createCommunicationAccountAction({
        channel: accountChannel,
        provider: selectedConnection?.provider ?? "sandbox",
        integration_connection_id: selectedConnection?.id ?? null,
        name,
        sender_label: senderLabel.trim() || null,
        configuration: {},
      });
      if (!result.ok) {
        toast.error(copy.settingsSaveFailed(result.error));
        return;
      }
      setAccounts((current) => [...current, result.data.account]);
      setAccountName("");
      setSenderLabel("");
      setAccountBinding("sandbox");
      if (result.data.webhook_secret) {
        setSecret({
          accountId: result.data.account.id,
          value: result.data.webhook_secret,
        });
      }
      toast.success(copy.accountCreated);
    });
  }

  function rotateSecret(account: CommunicationAccount) {
    if (
      !access.rotateCredentials ||
      rotatingId ||
      !window.confirm(copy.rotateSecretConfirm(account.name))
    ) {
      return;
    }
    setRotatingId(account.id);
    startAccount(async () => {
      try {
        const result = await rotateCommunicationWebhookSecretAction(account.id);
        if (!result.ok) {
          toast.error(copy.settingsSaveFailed(result.error));
          return;
        }
        setSecret({ accountId: account.id, value: result.data.webhook_secret });
        setAccounts((current) =>
          current.map((item) =>
            item.id === account.id
              ? { ...item, webhook_configured: true }
              : item,
          ),
        );
        toast.success(copy.secretRotated);
      } finally {
        setRotatingId(null);
      }
    });
  }

  async function copyText(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(copy.copied);
    } catch {
      toast.error(copy.copyFailed);
    }
  }

  function resetTemplateForm() {
    setTemplateId(null);
    setTemplateName("");
    setTemplateChannel("email");
    setTemplateLanguage("ru");
    setTemplateSubject("");
    setTemplateBody("");
    setTemplateActive(true);
  }

  function editTemplate(template: CommunicationTemplate) {
    if (!access.manageTemplates) return;
    if (template.channel === "whatsapp") return;
    setTemplateId(template.id);
    setTemplateName(template.name);
    setTemplateChannel(template.channel);
    setTemplateLanguage(template.language);
    setTemplateSubject(template.subject_template ?? "");
    setTemplateBody(template.body_template);
    setTemplateActive(template.is_active);
  }

  function saveTemplate(event: React.FormEvent) {
    event.preventDefault();
    if (!access.manageTemplates) return;
    const name = templateName.trim();
    const body = templateBody.trim();
    if (!name || !body || templatePending) return;
    const values = {
      name,
      language: templateLanguage.trim() || "ru",
      subject_template:
        templateChannel === "email" ? templateSubject.trim() || null : null,
      body_template: body,
      is_active: templateActive,
    };
    startTemplate(async () => {
      const result = templateId
        ? await updateCommunicationTemplateAction(templateId, values)
        : await createCommunicationTemplateAction({
            ...values,
            channel: templateChannel,
          });
      if (!result.ok) {
        toast.error(copy.settingsSaveFailed(result.error));
        return;
      }
      setTemplates((current) => {
        const exists = current.some((item) => item.id === result.data.id);
        const next = exists
          ? current.map((item) =>
              item.id === result.data.id ? result.data : item,
            )
          : [...current, result.data];
        return next.sort((left, right) => left.name.localeCompare(right.name));
      });
      toast.success(templateId ? copy.templateUpdated : copy.templateCreated);
      resetTemplateForm();
    });
  }

  function toggleTemplate(template: CommunicationTemplate) {
    if (!access.manageTemplates || template.channel === "whatsapp") return;
    startTemplate(async () => {
      const result = await updateCommunicationTemplateAction(template.id, {
        is_active: !template.is_active,
      });
      if (!result.ok) {
        toast.error(copy.settingsSaveFailed(result.error));
        return;
      }
      setTemplates((current) =>
        current.map((item) =>
          item.id === result.data.id ? result.data : item,
        ),
      );
      if (templateId === result.data.id) {
        setTemplateActive(result.data.is_active);
      }
      toast.success(copy.templateUpdated);
    });
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-[16px] font-semibold text-text">
          {copy.settingsTitle}
        </h2>
        <p className="mt-1 text-[12px] text-text-muted">{copy.settingsDesc}</p>
      </div>

      {initialLoadFailed ? (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-score-mid/30 bg-score-mid/5 px-4 py-3 text-[11px] text-score-mid">
          <span>{copy.settingsLoadFailed}</span>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="inline-flex items-center gap-1.5 font-medium"
          >
            <RefreshCw className="h-3.5 w-3.5" /> {copy.retry}
          </button>
        </div>
      ) : null}

      {access.viewAccounts ? <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="mb-4 flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-primary" />
          <div>
            <h3 className="text-[12px] font-semibold uppercase tracking-[0.07em] text-text-muted">
              {copy.accounts}
            </h3>
            <p className="mt-0.5 text-[10.5px] text-text-faint">
              {copy.sandboxHint}
            </p>
          </div>
        </div>

        {accounts.length === 0 ? (
          <p className="mb-4 rounded-lg border border-dashed border-border px-3 py-6 text-center text-[11px] text-text-faint">
            {copy.noAccounts}
          </p>
        ) : (
          <ul className="mb-4 space-y-2">
            {accounts.map((account) => {
              const webhookUrl = `${webhookBaseUrl.replace(/\/$/, "")}/communications/webhooks/sandbox/${account.id}`;
              const available = isCommunicationAccountAvailable(account);
              return (
                <li
                  key={account.id}
                  className="rounded-lg border border-border bg-bg p-3"
                >
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        {account.channel === "email" ? (
                          <Mail className="h-3.5 w-3.5 text-text-muted" />
                        ) : (
                          <MessageCircle className="h-3.5 w-3.5 text-text-muted" />
                        )}
                        <span className="text-[12px] font-medium text-text">
                          {account.name}
                        </span>
                        <span className="rounded-full border border-border px-1.5 py-0.5 text-[9px] uppercase text-text-faint">
                            {account.channel} · {account.provider}
                        </span>
                        <span
                          className={cn(
                            "inline-flex items-center gap-1 text-[9.5px]",
                            available
                              ? "text-score-high"
                              : account.is_active
                                ? "text-score-mid"
                                : "text-text-faint",
                          )}
                        >
                          {available ? (
                            <Check className="h-3 w-3" />
                          ) : null}
                          {available
                            ? copy.enabled
                            : account.is_active
                              ? copy.providerUnavailable
                              : copy.disabled}
                        </span>
                      </div>
                      {account.sender_label ? (
                        <p className="mt-1 text-[10.5px] text-text-muted">
                          {account.sender_label}
                        </p>
                      ) : null}
                      {account.provider === "sandbox" ? (
                        <div className="mt-1.5 max-w-2xl">
                          <button
                            type="button"
                            onClick={() => void copyText(webhookUrl)}
                            className="block max-w-full truncate font-mono text-[9.5px] text-text-faint hover:text-text"
                            title={webhookUrl}
                          >
                            {copy.webhook}: {webhookUrl}
                          </button>
                          <p className="mt-1 text-[9.5px] leading-relaxed text-text-faint">
                            {copy.webhookSignedHint}
                          </p>
                        </div>
                      ) : null}
                    </div>
                    {access.rotateCredentials && account.provider === "sandbox" ? (
                      <button
                        type="button"
                        disabled={rotatingId === account.id || accountPending}
                        onClick={() => rotateSecret(account)}
                        className="inline-flex items-center gap-1.5 rounded-[6px] border border-border px-2.5 py-1.5 text-[10.5px] text-text-muted hover:bg-bg-hover disabled:opacity-50"
                      >
                        {rotatingId === account.id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <KeyRound className="h-3 w-3" />
                        )}
                        {copy.rotateSecret}
                      </button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {access.rotateCredentials && secret ? (
          <div className="mb-4 rounded-lg border border-score-mid/30 bg-score-mid/5 p-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-[11px] font-medium text-score-mid">
                  {copy.secretOnce}
                </p>
                <p className="mt-1 font-mono text-[10px] text-text-faint">
                  {copy.secretFor(
                    accounts.find((account) => account.id === secret.accountId)
                      ?.name ?? secret.accountId,
                  )}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setSecret(null)}
                aria-label={copy.dismissSecret}
                className="rounded p-1 text-text-faint hover:bg-bg-hover hover:text-text"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="mt-2 flex gap-2">
              <input
                readOnly
                value={secret.value}
                aria-label={copy.webhookSecret}
                className={cn(controlClass, "font-mono text-[10.5px]")}
              />
              <button
                type="button"
                onClick={() => void copyText(secret.value)}
                className="inline-flex items-center gap-1.5 rounded-[6px] border border-border bg-bg px-3 py-2 text-[11px]"
              >
                <Clipboard className="h-3.5 w-3.5" /> {copy.copy}
              </button>
            </div>
          </div>
        ) : null}

        {access.manageAccounts ? <form
          onSubmit={createAccount}
          className="grid gap-2 border-t border-border pt-4 md:grid-cols-[130px_1fr_1fr_1fr_auto]"
        >
          <select
            value={accountChannel}
            onChange={(event) =>
                setAccountChannel(event.target.value as OutboundMessageChannel)
            }
            aria-label={copy.channel}
            className={controlClass}
          >
            <option value="email">Email</option>
            <option value="telegram">Telegram</option>
          </select>
          <select
            value={accountBinding}
            onChange={(event) => setAccountBinding(event.target.value)}
            aria-label={copy.deliveryConnection}
            className={controlClass}
          >
            <option value="sandbox">{copy.sandboxConnection}</option>
            {compatibleConnections.map((connection) => (
              <option key={connection.id} value={connection.id}>
                {connection.name} · {connection.provider}
              </option>
            ))}
          </select>
          <input
            value={accountName}
            onChange={(event) => setAccountName(event.target.value)}
            placeholder={copy.accountNamePlaceholder}
            aria-label={copy.name}
            maxLength={120}
            className={controlClass}
          />
          <input
            value={senderLabel}
            onChange={(event) => setSenderLabel(event.target.value)}
            placeholder={copy.senderLabelPlaceholder}
            aria-label={copy.senderLabel}
            maxLength={255}
            className={controlClass}
          />
          <button
            type="submit"
            disabled={!accountName.trim() || accountPending}
            className="inline-flex items-center justify-center gap-1.5 rounded-[6px] bg-text px-3 py-2 text-[11px] font-medium text-bg disabled:opacity-50"
          >
            {accountPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Plus className="h-3.5 w-3.5" />
            )}
            {copy.newAccount}
          </button>
          {connectionsFailed ? (
            <p className="md:col-span-5 text-[10.5px] text-score-mid">
              {copy.connectionsLoadFailed}
            </p>
          ) : null}
        </form> : null}
      </section> : null}

      {access.viewTemplates ? <section className="rounded-xl border border-border bg-bg-elevated p-5">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-[12px] font-semibold uppercase tracking-[0.07em] text-text-muted">
              {copy.templates}
            </h3>
            <p className="mt-1 max-w-3xl text-[10.5px] leading-4 text-text-faint">
              {copy.templateVariables}
            </p>
            <p className="mt-1 max-w-3xl text-[10.5px] leading-4 text-score-mid">
              {copy.templateSafety}
            </p>
          </div>
          {access.manageTemplates && templateId ? (
            <button
              type="button"
              onClick={resetTemplateForm}
              className="inline-flex items-center gap-1.5 rounded-[6px] border border-border px-2.5 py-1.5 text-[10.5px] text-text-muted hover:bg-bg-hover"
            >
              <Plus className="h-3 w-3" /> {copy.newTemplate}
            </button>
          ) : null}
        </div>

        <div className="grid gap-5 lg:grid-cols-[minmax(0,0.85fr)_minmax(360px,1.15fr)]">
          <div>
            {templates.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border px-3 py-8 text-center text-[11px] text-text-faint">
                {copy.noTemplates}
              </p>
            ) : (
              <ul className="space-y-2">
                {templates.map((template) => {
                  const legacyWhatsApp = template.channel === "whatsapp";
                  return (
                    <li
                      key={template.id}
                      className={cn(
                        "rounded-lg border bg-bg p-3",
                        templateId === template.id
                          ? "border-border-strong"
                          : "border-border",
                      )}
                    >
                      <div className="flex items-start justify-between gap-3">
                        {access.manageTemplates && !legacyWhatsApp ? (
                          <button
                            type="button"
                            onClick={() => editTemplate(template)}
                            className="min-w-0 flex-1 text-left"
                          >
                            <TemplateSummary template={template} />
                          </button>
                        ) : (
                          <div className="min-w-0 flex-1 text-left">
                            <TemplateSummary template={template} />
                          </div>
                        )}
                        {legacyWhatsApp ? (
                          <div className="flex shrink-0 flex-col items-end gap-1">
                            <span className="rounded-[5px] border border-score-mid/30 px-2 py-1 text-[9.5px] text-score-mid">
                              {copy.legacyTemplateUnavailable}
                            </span>
                            <span className="text-[9px] text-text-faint">
                              {copy.legacyTemplateStoredStatus(template.is_active)}
                            </span>
                          </div>
                        ) : access.manageTemplates ? (
                          <div className="flex shrink-0 items-center gap-1">
                            <button
                              type="button"
                              onClick={() => editTemplate(template)}
                              aria-label={copy.editTemplate}
                              className="rounded p-1.5 text-text-faint hover:bg-bg-hover hover:text-text"
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </button>
                            <button
                              type="button"
                              disabled={templatePending}
                              onClick={() => toggleTemplate(template)}
                              className={cn(
                                "rounded-[5px] border px-2 py-1 text-[9.5px] disabled:opacity-50",
                                template.is_active
                                  ? "border-score-high/25 text-score-high"
                                  : "border-border text-text-faint",
                              )}
                            >
                              {template.is_active ? copy.enabled : copy.disabled}
                            </button>
                          </div>
                        ) : null}
                      </div>
                      {legacyWhatsApp ? (
                        <p className="mt-2 border-t border-border/60 pt-2 text-[10px] leading-4 text-text-faint">
                          {copy.legacyTemplateReadOnly}
                        </p>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {access.manageTemplates ? <form
            onSubmit={saveTemplate}
            className="rounded-lg border border-border bg-bg p-4"
          >
            <div className="mb-3 flex items-center justify-between gap-3">
              <h4 className="text-[11.5px] font-medium text-text">
                {templateId ? copy.editTemplate : copy.newTemplate}
              </h4>
              <label className="inline-flex items-center gap-2 text-[10.5px] text-text-muted">
                <input
                  type="checkbox"
                  checked={templateActive}
                  onChange={(event) => setTemplateActive(event.target.checked)}
                  className="accent-current"
                />
                {copy.enabled}
              </label>
            </div>
            <div className="grid gap-2 sm:grid-cols-[1fr_130px_90px]">
              <input
                value={templateName}
                onChange={(event) => setTemplateName(event.target.value)}
                placeholder={copy.templateNamePlaceholder}
                aria-label={copy.name}
                maxLength={120}
                className={controlClass}
              />
              <select
                value={templateChannel}
                disabled={Boolean(templateId)}
                onChange={(event) => {
                  const next = event.target.value as OutboundMessageChannel;
                  setTemplateChannel(next);
                  if (next !== "email") setTemplateSubject("");
                }}
                aria-label={copy.channel}
                className={cn(controlClass, "disabled:opacity-60")}
              >
                <option value="email">Email</option>
                <option value="telegram">Telegram</option>
              </select>
              <input
                value={templateLanguage}
                onChange={(event) => setTemplateLanguage(event.target.value)}
                placeholder="ru"
                aria-label={copy.language}
                minLength={2}
                maxLength={8}
                className={controlClass}
              />
            </div>
            {templateChannel === "email" ? (
              <input
                value={templateSubject}
                onChange={(event) => setTemplateSubject(event.target.value)}
                placeholder={copy.subject}
                aria-label={copy.subject}
                maxLength={500}
                className={cn(controlClass, "mt-2")}
              />
            ) : null}
            <textarea
              value={templateBody}
              onChange={(event) => setTemplateBody(event.target.value)}
              placeholder={copy.templateBodyPlaceholder}
              aria-label={copy.body}
              rows={8}
              maxLength={20_000}
              className={cn(controlClass, "mt-2 resize-y")}
            />
            <div className="mt-3 flex justify-end gap-2">
              {templateId ? (
                <button
                  type="button"
                  onClick={resetTemplateForm}
                  className="rounded-[6px] px-3 py-2 text-[11px] text-text-muted hover:bg-bg-hover"
                >
                  {m.common.cancel}
                </button>
              ) : null}
              <button
                type="submit"
                disabled={
                  !templateName.trim() ||
                  !templateBody.trim() ||
                  templatePending
                }
                className="inline-flex items-center gap-1.5 rounded-[6px] bg-text px-3 py-2 text-[11px] font-medium text-bg disabled:opacity-50"
              >
                {templatePending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : null}
                {copy.save}
              </button>
            </div>
          </form> : null}
        </div>
      </section> : null}
    </div>
  );
}

function TemplateSummary({ template }: { template: CommunicationTemplate }) {
  return (
    <>
      <span className="flex flex-wrap items-center gap-2">
        <span className="truncate text-[11.5px] font-medium text-text">
          {template.name}
        </span>
        <span className="rounded-full border border-border px-1.5 py-0.5 text-[9px] uppercase text-text-faint">
          {template.channel} · {template.language}
        </span>
      </span>
      <span className="mt-1.5 line-clamp-2 whitespace-pre-wrap text-[10.5px] leading-4 text-text-muted">
        {template.body_template}
      </span>
    </>
  );
}
