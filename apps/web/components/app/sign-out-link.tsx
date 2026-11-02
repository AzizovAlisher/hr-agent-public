"use client";

import { LogOut } from "lucide-react";

import { useI18n } from "@/components/i18n/locale-provider";

export function SignOutLink() {
  const { m } = useI18n();
  return (
    <form
      action="/api/auth/local-logout?scope=current"
      method="post"
      className="w-full"
    >
      <button
        type="submit"
        className="group flex w-full items-center gap-2.5 rounded-[6px] px-2 py-1.5 text-[13px] font-medium text-text-muted transition-colors hover:bg-bg-hover hover:text-text focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      >
        <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center opacity-85">
          <LogOut className="h-3.5 w-3.5" aria-hidden />
        </span>
        <span className="flex-1 text-left">{m.app.signOut}</span>
      </button>
    </form>
  );
}
