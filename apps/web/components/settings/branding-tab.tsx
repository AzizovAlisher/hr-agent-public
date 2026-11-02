"use client";

import { useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { ImageIcon, Palette, Upload, X } from "lucide-react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  deleteMyOrganizationLogoAction,
  updateMyOrganizationAction,
  uploadMyOrganizationLogoAction,
} from "@/lib/actions";
import type { Organization } from "@/lib/types";
import { cn } from "@/lib/utils";
import { BrandMark } from "@/components/public/brand-mark";

const HEX = /^#[0-9a-fA-F]{6}$/;

export function BrandingTab({
  organization,
  initialLogoUrl,
  canManage,
}: {
  organization: Organization | null;
  initialLogoUrl: string | null;
  canManage: boolean;
}) {
  const router = useRouter();
  const { m } = useI18n();
  const t = m.settings;

  const [color, setColor] = useState<string>(
    organization?.accent_color ?? "",
  );
  const [logoUrl, setLogoUrl] = useState<string | null>(initialLogoUrl);
  const [orgName, setOrgName] = useState<string>(organization?.name ?? "");
  const [savingColor, startColorSave] = useTransition();
  const [uploading, startUpload] = useTransition();
  const [removing, startRemove] = useTransition();
  const fileInputRef = useRef<HTMLInputElement>(null);

  if (!organization) {
    return (
      <p className="text-[12.5px] text-text-muted">{t.brandingNotAdmin}</p>
    );
  }

  function commitColor(next: string) {
    if (!canManage) return;
    const trimmed = next.trim();
    const value = trimmed === "" ? null : trimmed;
    if (value && !HEX.test(value)) {
      toast.error(t.brandingFailedToast(t.brandingColorPlaceholder));
      return;
    }
    startColorSave(async () => {
      const res = await updateMyOrganizationAction({ accent_color: value });
      if (!res.ok) {
        toast.error(t.brandingFailedToast(res.error));
        return;
      }
      toast.success(t.brandingSavedToast);
      router.refresh();
    });
  }

  function pickLogo() {
    if (!canManage) return;
    fileInputRef.current?.click();
  }

  function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    if (!canManage) return;
    const f = e.target.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.set("file", f);
    startUpload(async () => {
      const res = await uploadMyOrganizationLogoAction(fd);
      if (!res.ok) {
        toast.error(t.brandingFailedToast(res.error));
        return;
      }
      // Optimistically swap the preview to the freshly-uploaded file via
      // a local object URL — server-rendered URL will catch up on refresh.
      setLogoUrl(URL.createObjectURL(f));
      toast.success(t.brandingSavedToast);
      router.refresh();
    });
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function removeLogo() {
    if (!canManage) return;
    startRemove(async () => {
      const res = await deleteMyOrganizationLogoAction();
      if (!res.ok) {
        toast.error(t.brandingFailedToast(res.error));
        return;
      }
      setLogoUrl(null);
      toast.success(t.brandingSavedToast);
      router.refresh();
    });
  }

  const previewAccent = color && HEX.test(color) ? color : null;

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="text-[15px] font-semibold tracking-tight">
          {t.brandingTitle}
        </h2>
        <p className="text-[12.5px] text-text-muted">{t.brandingDesc}</p>
      </header>

      {/* Logo */}
      <section className="flex flex-col gap-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
          {t.brandingLogoLabel}
        </span>
        <div className="flex items-center gap-4 rounded-lg border border-border bg-bg-elevated p-4">
          <div className="flex h-14 w-14 items-center justify-center rounded-md border border-border bg-bg">
            {logoUrl ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={logoUrl}
                alt={orgName}
                className="h-full w-full rounded-md object-contain p-1"
              />
            ) : (
              <ImageIcon className="h-6 w-6 text-text-faint" />
            )}
          </div>
          <div className="flex flex-1 flex-col gap-1">
            <span className="text-[12.5px] text-text-muted">
              {t.brandingLogoHint}
            </span>
          </div>
          {canManage ? (
            <>
              <input
                ref={fileInputRef}
                type="file"
                accept=".png,.jpg,.jpeg,.svg,.webp,image/png,image/jpeg,image/svg+xml,image/webp"
                onChange={handleFile}
                className="hidden"
              />
              <button
                type="button"
                onClick={pickLogo}
                disabled={uploading || removing}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg px-3 py-1.5 text-[12px] font-medium text-text transition-colors hover:border-border-strong hover:bg-bg-hover disabled:opacity-40"
              >
                <Upload className="h-3 w-3" />
                {logoUrl ? t.brandingLogoReplace : t.brandingLogoUpload}
              </button>
            </>
          ) : null}
          {canManage && logoUrl ? (
            <button
              type="button"
              onClick={removeLogo}
              disabled={uploading || removing}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-bg px-3 py-1.5 text-[12px] text-text-muted transition-colors hover:border-score-low/50 hover:text-score-low disabled:opacity-40"
            >
              <X className="h-3 w-3" />
              {t.brandingLogoRemove}
            </button>
          ) : null}
        </div>
      </section>

      {/* Accent color */}
      <section className="flex flex-col gap-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
          {t.brandingColorLabel}
        </span>
        <div className="flex items-center gap-3 rounded-lg border border-border bg-bg-elevated p-4">
          <span
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-border"
            style={{
              background: previewAccent ?? "var(--bg)",
            }}
          >
            {previewAccent ? null : (
              <Palette className="h-4 w-4 text-text-faint" />
            )}
          </span>
          <input
            type="color"
            value={previewAccent ?? "#2b6cb0"}
            onChange={(e) => setColor(e.target.value)}
            disabled={!canManage}
            className="h-10 w-12 cursor-pointer rounded border border-border bg-bg-elevated disabled:cursor-default disabled:opacity-60"
          />
          <input
            type="text"
            value={color}
            onChange={(e) => setColor(e.target.value)}
            readOnly={!canManage}
            placeholder={t.brandingColorPlaceholder}
            className={cn(
              "flex-1 rounded-md border border-border bg-bg px-3 py-2 font-mono text-[12.5px] uppercase tracking-[0.04em] outline-none transition-colors focus:border-border-strong",
              color && !HEX.test(color) && "border-score-low/50 text-score-low",
            )}
          />
          {canManage ? <button
            type="button"
            onClick={() => commitColor(color)}
            disabled={savingColor || (!!color && !HEX.test(color))}
            className="inline-flex items-center gap-1.5 rounded-md bg-text px-3 py-2 text-[12px] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {savingColor ? m.common.saving : m.common.save}
          </button> : null}
          {canManage && color ? (
            <button
              type="button"
              onClick={() => {
                setColor("");
                commitColor("");
              }}
              disabled={savingColor}
              className="text-[11px] text-text-muted transition-colors hover:text-text"
            >
              {t.brandingColorReset}
            </button>
          ) : null}
        </div>
        <span className="text-[11px] text-text-faint">
          {t.brandingColorHint}
        </span>
      </section>

      {/* Preview */}
      <section className="flex flex-col gap-3">
        <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-text-faint">
          {t.brandingPreviewLabel}
        </span>
        <div
          className="flex flex-col gap-3 rounded-lg border border-border bg-bg p-5"
          style={
            previewAccent
              ? ({ "--primary": previewAccent } as React.CSSProperties)
              : undefined
          }
        >
          <div className="flex items-center gap-2">
            <BrandMark
              logoUrl={logoUrl}
              accentColor={previewAccent}
              orgName={orgName || organization.name}
            />
            <span className="text-[12.5px] font-medium tracking-tight text-text-muted">
              {orgName || organization.name}
            </span>
          </div>
          <button
            type="button"
            disabled
            className="w-fit rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground"
          >
            {/* Use the existing apply CTA copy as a stand-in so admins see
             how their accent color reads on a real button. */}
            {m.apply.submit}
          </button>
        </div>
      </section>
    </div>
  );
}
