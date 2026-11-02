/**
 * Categorize a requirement (key/value pair from `position.requirements`)
 * into one of seven UI buckets, each with its own colour token.
 *
 * Inputs are intentionally permissive — `requirements` is a free-form jsonb
 * dict so the categoriser has to recognise both the canonical keys we set
 * ourselves (e.g. `salary_range`, `work_format`) and the freeform tech keys
 * the AI invents (e.g. `python`, `kubernetes`, `team_lead`).
 *
 * Returns `null` for keys that are pure metadata (source_url, imported_from,
 * etc.) — those should never render as a chip.
 */

/** @ui-only Presentation grouping for free-form position requirements. */
export type RequirementCategory =
  | "tech"
  | "salary"
  | "location"
  | "seniority"
  | "experience"
  | "language"
  | "format"
  | "education"
  | "employment"
  | "other";

/** @ui-only Presentation row for a categorized requirement. */
export type CategorizedRequirement = {
  key: string;
  value: string;
  category: RequirementCategory;
  /** Suggested display label (may differ from key/value pair). */
  label: string;
  /** When true, the chip should show only `label`; otherwise show key·value. */
  labelOnly: boolean;
};

// Keys that are ingest metadata only — never render as a chip.
const META_KEYS = new Set([
  "source_ref",
  "source_url",
  "company",
  "imported_from",
  "applied_via",
  "applicant_phone",
  "applicant_email",
  "applicant_name",
]);

const SALARY_KEYS = new Set([
  "salary",
  "salary_range",
  "salary_from",
  "salary_to",
  "compensation",
  "rate",
]);

const LOCATION_KEYS = new Set([
  "location",
  "city",
  "country",
  "region",
  "office",
]);

const SENIORITY_KEYS = new Set(["seniority", "grade", "level"]);

const EXPERIENCE_KEYS = new Set([
  "experience",
  "experience_years",
  "years",
  "years_experience",
]);

const FORMAT_KEYS = new Set(["work_format", "format", "remote", "hybrid"]);

const EMPLOYMENT_KEYS = new Set([
  "employment_type",
  "employment",
  "contract",
  "contract_type",
]);

const LANGUAGE_KEYS = new Set([
  "language",
  "languages",
  "russian",
  "english",
  "tajik",
  "tojiki",
  "language_skills",
]);

const EDUCATION_KEYS = new Set([
  "education",
  "degree",
  "university",
  "diploma",
  "qualification",
]);

// Heuristic: keys that LOOK like a tech name even if not in the lists above
// (treated as `tech` with the key as label).
const TECH_HINTS = [
  "python",
  "java",
  "javascript",
  "typescript",
  "go",
  "rust",
  "kubernetes",
  "docker",
  "postgres",
  "mysql",
  "redis",
  "kafka",
  "react",
  "vue",
  "angular",
  "fastapi",
  "django",
  "spring",
  "linux",
  "aws",
  "gcp",
  "azure",
  "ip",
  "mpls",
  "bgp",
  "ospf",
  "cisco",
  "voip",
  "sip",
  "voice",
  "noc",
  "siem",
  "soc",
  "1c",
  "sap",
  "excel",
  "word",
];

// Localized labels for canonical machine values (work_format, employment_type,
// seniority). Falls back to the raw value if the locale dict has no entry.
const VALUE_LABELS: Record<string, Record<"ru" | "en", string>> = {
  remote: { ru: "Удалённо", en: "Remote" },
  hybrid: { ru: "Гибрид", en: "Hybrid" },
  onsite: { ru: "Офис", en: "On-site" },
  full_time: { ru: "Полная занятость", en: "Full-time" },
  part_time: { ru: "Частичная занятость", en: "Part-time" },
  contract: { ru: "Контракт", en: "Contract" },
  junior: { ru: "Junior", en: "Junior" },
  middle: { ru: "Middle", en: "Middle" },
  senior: { ru: "Senior", en: "Senior" },
  lead: { ru: "Lead", en: "Lead" },
  principal: { ru: "Principal", en: "Principal" },
};

function titleCase(s: string): string {
  // Capitalise first letter of each word — works for ASCII and Cyrillic.
  // Snake/kebab → spaces, then uppercase the first letter of each word.
  return s
    .replace(/[_-]+/g, " ")
    .replace(/(^|\s)(\p{L})/gu, (_m, sep, ch) => sep + ch.toLocaleUpperCase());
}

// Chips must read at a glance. 64 was set when chips were rendered without
// width caps; the new chip component (requirement-chips.tsx) also enforces a
// max-w, so we can be aggressive here. Full text remains available via the
// `title` attribute / hover tooltip on each chip.
const MAX_LABEL = 28;
function clampLabel(label: string): string {
  if (label.length <= MAX_LABEL) return label;
  return label.slice(0, MAX_LABEL - 1).trimEnd() + "…";
}

/**
 * Strip parenthetical detail from values that contain a degree/level + a long
 * specialty list inside parens. The parens are noise on a chip but kept in
 * the underlying `value` for the tooltip.
 *
 * "Высшее (информационные технологии, телекоммуникации)" → "Высшее"
 * "Среднее специальное или высшее (документационное …)" → "Среднее специальное или высшее"
 */
function stripParenSuffix(value: string): string {
  return value.replace(/\s*\([^)]*\)\s*$/u, "").trim() || value;
}

// Noise qualifiers — values that say either "yes this is required" OR
// "at <some generic level>" without adding specific info. Once an item is
// on the requirements list its presence already implies it's required, and
// "advanced" / "уверенное владение" don't tell the recruiter anything they
// couldn't assume. Strip these from the chip label.
//
// What we DO keep (because it adds real info): values with digits ("5+ лет",
// "от 1 года"), comma-separated specifics ("LDP, RSVP-TE", "Cisco, Juniper"),
// and longer descriptive phrases ("по закрытым вакансиям").
// Use \p{L} (any Unicode letter) with the `u` flag so Cyrillic word
// characters are matched. JS `\w` is ASCII-only even with `u`, so a pattern
// like `обязательн\w*` would NEVER match `обязательно` — that's a footgun
// we hit before. `L` covers letters, `\d` digits, `_` underscore.
const W = "[\\p{L}\\d_]";

const NOISE_QUALIFIER_RE = new RegExp(
  "^(" +
    [
      // "yes/required" markers
      `обязательн${W}*`,
      "обязательность",
      "required",
      "yes",
      "y",
      "true",
      "есть",
      "нужно",
      `необходим${W}*`,
      "must[\\s-]?have",
      "mandatory",
      "требуется",
      // Russian level descriptors
      `уверенн${W}*(\\s+(владени${W}+|пользовател${W}+|использован${W}+))?`,
      `хорош${W}+(\\s+(знани${W}+|владени${W}+))?`,
      `отличн${W}+(\\s+(знани${W}+|владени${W}+))?`,
      `глубок${W}+(\\s+(знани${W}+|владени${W}+|пониман${W}+))?`,
      `базов${W}+(\\s+(знани${W}+|владени${W}+|уровень))?`,
      `продвинут${W}+(\\s+уровень)?`,
      `начальн${W}+(\\s+уровень)?`,
      `средн${W}+(\\s+(уровень|знани${W}+))?`,
      `свободн${W}+(\\s+владени${W}+)?`,
      `опытн${W}+`,
      "знани[ея]",
      "владени[ея]",
      "опыт\\s+работы",
      `профессиональн${W}*`,
      "обязательно?\\s+знание",
      "в\\s+полном\\s+объеме",
      // English level descriptors
      "advanced",
      "expert",
      "intermediate",
      "beginner",
      "preferred",
      "strong",
      "good",
      "excellent",
      "fluent",
      "professional",
      "nice[\\s-]to[\\s-]have",
      "plus",
      "n/?a",
    ].join("|") +
    ")\\.?$",
  "iu",
);

function isNoiseQualifier(value: string): boolean {
  return NOISE_QUALIFIER_RE.test(value.trim());
}

function localize(value: string, locale: "ru" | "en"): string {
  return VALUE_LABELS[value]?.[locale] ?? value;
}

export function categorize(
  rawKey: string,
  rawValue: unknown,
  locale: "ru" | "en" = "ru",
): CategorizedRequirement | null {
  const key = String(rawKey).trim().toLowerCase();
  if (META_KEYS.has(key)) return null;
  if (rawValue == null || rawValue === "") return null;
  const value = String(rawValue).trim();
  if (!value) return null;

  // Salary — value-only chip, often verbose ("8000-12000 сомони/мес net").
  if (SALARY_KEYS.has(key)) {
    return { key, value, category: "salary", label: clampLabel(value), labelOnly: true };
  }

  // Location — value-only chip.
  if (LOCATION_KEYS.has(key)) {
    return { key, value, category: "location", label: clampLabel(value), labelOnly: true };
  }

  // Work format — uses a localized label for canonical values.
  if (FORMAT_KEYS.has(key)) {
    const lower = value.toLowerCase().replace(/[\s-]+/g, "_");
    return {
      key,
      value,
      category: "format",
      label: clampLabel(localize(lower, locale)),
      labelOnly: true,
    };
  }

  // Employment type — same treatment.
  if (EMPLOYMENT_KEYS.has(key)) {
    const lower = value.toLowerCase().replace(/[\s-]+/g, "_");
    return {
      key,
      value,
      category: "employment",
      label: clampLabel(localize(lower, locale)),
      labelOnly: true,
    };
  }

  // Seniority — single token, capitalised.
  if (SENIORITY_KEYS.has(key)) {
    const lower = value.toLowerCase();
    return {
      key,
      value,
      category: "seniority",
      label: clampLabel(localize(lower, locale)),
      labelOnly: true,
    };
  }

  // Experience — show "Опыт: N+ лет" / "Experience: N+ years".
  if (EXPERIENCE_KEYS.has(key)) {
    const prefix = locale === "ru" ? "Опыт" : "Experience";
    return {
      key,
      value,
      category: "experience",
      label: clampLabel(`${prefix}: ${value}`),
      labelOnly: true,
    };
  }

  // Language — single chip per language. Value carries the level.
  if (LANGUAGE_KEYS.has(key) || /^lang_/.test(key)) {
    const niceKey = LANGUAGE_KEYS.has(key) ? "" : titleCase(key.replace(/^lang_/, ""));
    const label = clampLabel(niceKey ? `${niceKey} · ${value}` : value);
    return { key, value, category: "language", label, labelOnly: true };
  }

  // Education. Strip the parenthetical specialty list ("Высшее (X, Y)") so
  // the chip stays compact; full value is preserved on `value` for tooltip.
  if (EDUCATION_KEYS.has(key)) {
    return {
      key,
      value,
      category: "education",
      label: clampLabel(stripParenSuffix(value)),
      labelOnly: true,
    };
  }

  // Tech keywords — render as "Key · qualifier" if value is a real qualifier
  // (e.g. "5+ years"), else just the key.
  if (TECH_HINTS.includes(key) || /^skill_|_skill$/.test(key)) {
    const label = clampLabel(
      isNoiseQualifier(value)
        ? titleCase(key)
        : `${titleCase(key)} · ${value}`,
    );
    return { key, value, category: "tech", label, labelOnly: true };
  }

  // Default: unknown freeform key. Treat as tech if the value looks like a
  // qualifier ("5+ years", "advanced", "preferred"); else as "other".
  // NB: no \b — JS regex word boundaries only fire on ASCII, so Cyrillic
  // qualifiers ("обязательно", "желательно") wouldn't match with \b.
  const looksLikeTech = /(years?|лет|advanced|preferred|required|expert|желательн|обязательн|уверенн|владени|базов|продвин|опыт)/i.test(
    value,
  );
  if (looksLikeTech) {
    return {
      key,
      value,
      category: "tech",
      label: clampLabel(
        isNoiseQualifier(value)
          ? titleCase(key)
          : `${titleCase(key)} · ${value}`,
      ),
      labelOnly: true,
    };
  }

  // Last resort: render as a neutral chip with key·value.
  return {
    key,
    value,
    category: "other",
    label: clampLabel(
      isNoiseQualifier(value)
        ? titleCase(key)
        : `${titleCase(key)} · ${value}`,
    ),
    labelOnly: true,
  };
}

export function categorizeAll(
  requirements: Record<string, unknown> | null | undefined,
  locale: "ru" | "en" = "ru",
): CategorizedRequirement[] {
  if (!requirements) return [];
  const out: CategorizedRequirement[] = [];
  for (const [k, v] of Object.entries(requirements)) {
    const cat = categorize(k, v, locale);
    if (cat) out.push(cat);
  }
  // Stable visual order: salary → location → format → seniority → experience
  // → language → education → tech → other. Within a category, alphabetical.
  const order: Record<RequirementCategory, number> = {
    salary: 0,
    location: 1,
    format: 2,
    employment: 3,
    seniority: 4,
    experience: 5,
    language: 6,
    education: 7,
    tech: 8,
    other: 9,
  };
  return out.sort(
    (a, b) =>
      order[a.category] - order[b.category] ||
      a.label.localeCompare(b.label),
  );
}

/** Tailwind class triplet (text, bg, border) for a category. */
export const CATEGORY_CLASSES: Record<RequirementCategory, string> = {
  tech: "text-tag-tech bg-tag-tech-bg border-tag-tech-border",
  salary: "text-tag-salary bg-tag-salary-bg border-tag-salary-border",
  location: "text-tag-location bg-tag-location-bg border-tag-location-border",
  seniority:
    "text-tag-seniority bg-tag-seniority-bg border-tag-seniority-border",
  experience:
    "text-tag-seniority bg-tag-seniority-bg border-tag-seniority-border",
  language: "text-tag-language bg-tag-language-bg border-tag-language-border",
  format: "text-tag-format bg-tag-format-bg border-tag-format-border",
  education: "text-tag-education bg-tag-education-bg border-tag-education-border",
  employment: "text-text-muted bg-bg border-border",
  other: "text-text-muted bg-bg border-border",
};
