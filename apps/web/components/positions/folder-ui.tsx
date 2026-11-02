"use client";

import {
  Archive,
  Banknote,
  BookOpen,
  Briefcase,
  Building2,
  Cpu,
  Database,
  FlaskConical,
  Folder,
  GraduationCap,
  HardHat,
  Landmark,
  Layers,
  Megaphone,
  Server,
  ShoppingCart,
  Stethoscope,
  Truck,
  Users,
  Wrench,
  type LucideIcon,
} from "lucide-react";

/**
 * Folder colours map a stored colour name to the existing tag tokens
 * (globals.css) so folders match the chip palette and theme correctly.
 * Storing a name (not a hex) keeps it theme-aware in light/dark.
 */
export const FOLDER_COLORS: Record<
  string,
  { text: string; bg: string; border: string; dot: string }
> = {
  blue: { text: "text-tag-tech", bg: "bg-tag-tech-bg", border: "border-tag-tech-border", dot: "bg-tag-tech" },
  violet: { text: "text-tag-seniority", bg: "bg-tag-seniority-bg", border: "border-tag-seniority-border", dot: "bg-tag-seniority" },
  green: { text: "text-tag-format", bg: "bg-tag-format-bg", border: "border-tag-format-border", dot: "bg-tag-format" },
  amber: { text: "text-tag-salary", bg: "bg-tag-salary-bg", border: "border-tag-salary-border", dot: "bg-tag-salary" },
  orange: { text: "text-tag-location", bg: "bg-tag-location-bg", border: "border-tag-location-border", dot: "bg-tag-location" },
  teal: { text: "text-tag-language", bg: "bg-tag-language-bg", border: "border-tag-language-border", dot: "bg-tag-language" },
  rose: { text: "text-tag-education", bg: "bg-tag-education-bg", border: "border-tag-education-border", dot: "bg-tag-education" },
  slate: { text: "text-text-muted", bg: "bg-bg-elevated", border: "border-border", dot: "bg-text-faint" },
};

export const FOLDER_COLOR_NAMES = Object.keys(FOLDER_COLORS);
export const DEFAULT_FOLDER_COLOR = "slate";

export function folderColor(color: string | null | undefined) {
  return FOLDER_COLORS[color ?? ""] ?? FOLDER_COLORS[DEFAULT_FOLDER_COLOR];
}

/**
 * Curated lucide icons selectable for folders. Stored by id (kebab name); the
 * migration seeds a subset of these. Unknown ids fall back to a plain folder.
 */
export const FOLDER_ICONS: Record<string, LucideIcon> = {
  folder: Folder,
  "building-2": Building2,
  server: Server,
  archive: Archive,
  cpu: Cpu,
  landmark: Landmark,
  "flask-conical": FlaskConical,
  "graduation-cap": GraduationCap,
  "hard-hat": HardHat,
  briefcase: Briefcase,
  users: Users,
  wrench: Wrench,
  megaphone: Megaphone,
  "shopping-cart": ShoppingCart,
  database: Database,
  stethoscope: Stethoscope,
  "book-open": BookOpen,
  truck: Truck,
  banknote: Banknote,
  layers: Layers,
};

export const FOLDER_ICON_IDS = Object.keys(FOLDER_ICONS);

export function folderIcon(icon: string | null | undefined): LucideIcon {
  return FOLDER_ICONS[icon ?? ""] ?? Folder;
}
