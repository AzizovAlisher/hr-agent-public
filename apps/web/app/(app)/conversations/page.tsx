import { apiGet, apiGetAll } from "@/lib/api-client";
import { getCurrentPermissionMap } from "@/lib/current-permissions";
import { getMessages } from "@/lib/i18n";
import {
  canUseAiWorkspace,
  hasPermission,
} from "@/lib/permissions";
import type {
  ChatSessionListItem,
  CommunicationThreadPage,
} from "@/lib/types";
import { LocaleSwitcher } from "@/components/app/locale-switcher";
import { Topbar } from "@/components/app/topbar";
import { ConversationsHub } from "@/components/communications/conversations-hub";

export const dynamic = "force-dynamic";

const EMPTY_INBOX: CommunicationThreadPage = {
  items: [],
  next_cursor: null,
};

function redirectFailure(result: PromiseSettledResult<unknown>): unknown | null {
  if (result.status !== "rejected") return null;
  const cause = result.reason as { digest?: string };
  return cause?.digest?.startsWith("NEXT_REDIRECT") ? result.reason : null;
}

export default async function ConversationsPage() {
  const m = await getMessages();
  const permissions = await getCurrentPermissionMap();
  const canUseAi = canUseAiWorkspace(permissions);
  const canReadInbox = hasPermission(permissions, "communications.read");
  const canManageInbox = hasPermission(permissions, "communications.manage");
  const [sessionsResult, inboxResult] = await Promise.allSettled([
    canUseAi
      ? apiGetAll<ChatSessionListItem>("/chat/sessions")
      : Promise.resolve<ChatSessionListItem[]>([]),
    canReadInbox
      ? apiGet<CommunicationThreadPage>("/communications/threads?limit=30")
      : Promise.resolve<CommunicationThreadPage>(EMPTY_INBOX),
  ]);
  for (const result of [sessionsResult, inboxResult]) {
    const redirect = redirectFailure(result);
    if (redirect) throw redirect;
  }
  const sessions =
    sessionsResult.status === "fulfilled" ? sessionsResult.value : [];
  const inbox = inboxResult.status === "fulfilled" ? inboxResult.value : EMPTY_INBOX;

  return (
    <>
      <Topbar
        crumbs={[{ label: m.conversations.crumb }]}
        actions={<LocaleSwitcher />}
      />
      <div className="flex-1 overflow-y-auto overflow-x-hidden px-7 py-8">
        <header className="mb-7 flex items-end justify-between gap-6">
          <div>
            <h1 className="mb-1 text-[24px] font-semibold tracking-[-0.025em]">
              {m.conversations.title}
            </h1>
            <p className="text-[13px] text-text-muted">
              {m.communications.candidateSubtitle}
            </p>
          </div>
        </header>
        <ConversationsHub
          sessions={sessions}
          chatLoadFailed={canUseAi && sessionsResult.status === "rejected"}
          initialInbox={inbox}
          inboxLoadFailed={canReadInbox && inboxResult.status === "rejected"}
          access={{ canUseAi, canReadInbox, canManageInbox }}
        />
      </div>
    </>
  );
}
