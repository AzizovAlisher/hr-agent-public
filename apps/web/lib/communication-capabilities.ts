import type { CommunicationAccount } from "@/lib/types";

export function isCommunicationAccountAvailable(
  account: CommunicationAccount,
): boolean {
  return account.is_active && account.delivery_available;
}

export function isSandboxAccount(account: CommunicationAccount | undefined): boolean {
  return account?.provider === "sandbox";
}
