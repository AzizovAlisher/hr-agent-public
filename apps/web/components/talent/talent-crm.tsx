"use client";

import { AlertTriangle, FolderHeart, Loader2, MailPlus, RefreshCw, Search, Sparkles, Trash2, UserPlus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import { toast } from "sonner";

import { useI18n } from "@/components/i18n/locale-provider";
import {
  addTalentPoolMembersAction,
  createNurtureSequenceAction,
  createTalentPoolAction,
  deleteTalentPoolAction,
  launchNurtureSequenceAction,
  listTalentPoolMembersAction,
  rediscoverTalentAction,
  removeTalentPoolMemberAction,
  searchTalentAction,
} from "@/app/(app)/talent/actions";
import type { ApiResponseSchema } from "@/lib/generated/contracts";
import { cn } from "@/lib/utils";

type Pool = ApiResponseSchema<"TalentPoolOut">;
type Candidate = ApiResponseSchema<"TalentCandidateOut">;
type Sequence = ApiResponseSchema<"TalentNurtureSequenceOut">;
type Template = ApiResponseSchema<"CommunicationTemplateOut">;
type Position = ApiResponseSchema<"PositionOut">;

export type TalentCrmProps = {
  initialPools: Pool[];
  initialSequences: Sequence[];
  templates: Template[];
  positions: Position[];
  loadFailures: {
    pools: boolean;
    sequences: boolean;
    templates: boolean;
    positions: boolean;
  };
};

const COPY = {
  en: {
    title: "Talent CRM", subtitle: "Rediscover strong people, organize talent pools, and keep relationships warm.", pools: "Talent pools", search: "Semantic search", nurture: "Nurture", createPool: "Create pool", poolName: "Pool name", description: "Description", members: "members", loadMembers: "Open pool", emptyPool: "No candidates in this pool yet.", noPools: "Create your first talent pool to organize candidates.", find: "Search past candidates", query: "Skills, company, title, or project", runSearch: "Search", rediscover: "Match a vacancy", allPositions: "Choose a vacancy", noResults: "No matching candidates.", addTo: "Add to pool", remove: "Remove", match: "match", inPipeline: "Already in pipeline", sequences: "Nurture sequences", noSequences: "No nurture sequences yet.", sequenceName: "Sequence name", choosePool: "Choose a pool", chooseTemplate: "Choose a message template", delay: "Delay (days)", createSequence: "Create sequence", launch: "Launch for pool", launched: "Nurture scheduled", deleteConfirm: "Delete this talent pool?", deletePool: "Delete pool", saved: "Saved", failed: "Action failed", selectPool: "Select a pool first", noTemplates: "Create an active communication template first.", loadFailed: "Some Talent CRM data could not be loaded.", memberLoadFailed: "Could not load this pool. Try again.", retry: "Retry", loadMore: "Load more",
  },
  ru: {
    title: "Talent CRM", subtitle: "Возвращайте сильных кандидатов, собирайте кадровый резерв и поддерживайте контакт.", pools: "Кадровые резервы", search: "Семантический поиск", nurture: "Прогрев", createPool: "Создать резерв", poolName: "Название резерва", description: "Описание", members: "участников", loadMembers: "Открыть резерв", emptyPool: "В этом резерве пока нет кандидатов.", noPools: "Создайте первый кадровый резерв, чтобы организовать кандидатов.", find: "Поиск по прошлым кандидатам", query: "Навыки, компания, роль или проект", runSearch: "Найти", rediscover: "Подобрать под вакансию", allPositions: "Выберите вакансию", noResults: "Подходящих кандидатов нет.", addTo: "Добавить в резерв", remove: "Убрать", match: "совпадение", inPipeline: "Уже в воронке", sequences: "Цепочки прогрева", noSequences: "Цепочек прогрева пока нет.", sequenceName: "Название цепочки", choosePool: "Выберите резерв", chooseTemplate: "Выберите шаблон сообщения", delay: "Задержка (дни)", createSequence: "Создать цепочку", launch: "Запустить для резерва", launched: "Прогрев запланирован", deleteConfirm: "Удалить этот кадровый резерв?", deletePool: "Удалить резерв", saved: "Сохранено", failed: "Не удалось выполнить действие", selectPool: "Сначала выберите резерв", noTemplates: "Сначала создайте активный шаблон сообщения.", loadFailed: "Часть данных Talent CRM не загрузилась.", memberLoadFailed: "Не удалось загрузить резерв. Попробуйте ещё раз.", retry: "Повторить", loadMore: "Загрузить ещё",
  },
};

/** @ui-only Talent CRM tab state. */
type Tab = "pools" | "search" | "nurture";
/** @ui-only Talent-search request selector retained for pagination. */
type TalentResultRequest =
  | { kind: "search"; value: string }
  | { kind: "position"; value: string };

function appendUniqueCandidates(current: Candidate[], incoming: Candidate[]): Candidate[] {
  const known = new Set(current.map((candidate) => candidate.id));
  return [
    ...current,
    ...incoming.filter((candidate) => !known.has(candidate.id)),
  ];
}

export function TalentCrm({ initialPools, initialSequences, templates, positions, loadFailures }: TalentCrmProps) {
  const { locale } = useI18n();
  const t = COPY[locale];
  const router = useRouter();
  const [tab, setTab] = useState<Tab>("pools");
  const [pools, setPools] = useState(initialPools);
  const [selectedPool, setSelectedPool] = useState(initialPools[0]?.id ?? "");
  const [members, setMembers] = useState<Candidate[]>([]);
  const [membersNextCursor, setMembersNextCursor] = useState<string | null>(null);
  const [memberLoadError, setMemberLoadError] = useState(false);
  const [results, setResults] = useState<Candidate[]>([]);
  const [resultTotal, setResultTotal] = useState(0);
  const [resultNextCursor, setResultNextCursor] = useState<string | null>(null);
  const [resultRequest, setResultRequest] = useState<TalentResultRequest | null>(null);
  const memberRequestGeneration = useRef(0);
  const resultRequestGeneration = useRef(0);
  const [pending, start] = useTransition();
  const hasLoadFailure = Object.values(loadFailures).some(Boolean);

  useEffect(() => {
    setPools(initialPools);
    setSelectedPool((current) => {
      if (current && initialPools.some((pool) => pool.id === current)) return current;
      setMembers([]);
      setMembersNextCursor(null);
      setMemberLoadError(false);
      return initialPools[0]?.id ?? "";
    });
  }, [initialPools]);

  function success() { toast.success(t.saved); router.refresh(); }
  function failure(error?: string) { toast.error(error || t.failed); }
  async function fetchMembers(
    poolId: string,
    cursor: string | null = null,
    append = false,
  ) {
    const generation = ++memberRequestGeneration.current;
    const result = await listTalentPoolMembersAction(poolId, cursor);
    if (generation !== memberRequestGeneration.current) return;
    if (result.ok) {
      const incoming = result.data.items as Candidate[];
      setMembers((current) =>
        append ? appendUniqueCandidates(current, incoming) : incoming,
      );
      setMembersNextCursor(result.data.next_cursor);
      setMemberLoadError(false);
    } else {
      if (!append) setMembers([]);
      if (!append) setMembersNextCursor(null);
      setMemberLoadError(true);
      failure(result.error);
    }
  }
  function loadMembers(poolId = selectedPool) {
    if (!poolId) return failure(t.selectPool);
    start(() => fetchMembers(poolId, null, false));
  }
  function loadMoreMembers() {
    if (!selectedPool || !membersNextCursor) return;
    start(() => fetchMembers(selectedPool, membersNextCursor, true));
  }
  async function fetchResults(
    request: TalentResultRequest,
    cursor: string | null = null,
    append = false,
  ) {
    const generation = ++resultRequestGeneration.current;
    const result =
      request.kind === "search"
        ? await searchTalentAction(request.value, undefined, cursor)
        : await rediscoverTalentAction(request.value, cursor);
    if (generation !== resultRequestGeneration.current) return;
    if (result.ok) {
      const incoming = result.data.items as Candidate[];
      setResults((current) =>
        append ? appendUniqueCandidates(current, incoming) : incoming,
      );
      setResultTotal(result.data.total);
      setResultNextCursor(result.data.next_cursor);
      setResultRequest(request);
    } else {
      if (!append) {
        setResults([]);
        setResultTotal(0);
        setResultNextCursor(null);
      }
      failure(result.error);
    }
  }
  function loadMoreResults() {
    if (!resultRequest || !resultNextCursor) return;
    start(() => fetchResults(resultRequest, resultNextCursor, true));
  }
  function add(candidateId: string) {
    if (!selectedPool) return failure(t.selectPool);
    start(async () => {
      const result = await addTalentPoolMembersAction(selectedPool, [candidateId]);
      if (result.ok) { await fetchMembers(selectedPool); success(); }
      else failure(result.error);
    });
  }

  return (
    <div className="mx-auto max-w-6xl">
      <header className="mb-6"><h1 className="text-[24px] font-semibold tracking-[-0.025em]">{t.title}</h1><p className="mt-1 text-sm text-text-muted">{t.subtitle}</p></header>
      {hasLoadFailure ? <div className="mb-5 flex items-center gap-3 rounded-lg border border-amber-300/60 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:bg-amber-950/20 dark:text-amber-200"><AlertTriangle className="h-4 w-4 shrink-0" /><span className="min-w-0 flex-1">{t.loadFailed}</span><button type="button" onClick={() => router.refresh()} className="flex items-center gap-1 font-medium underline"><RefreshCw className="h-3.5 w-3.5" />{t.retry}</button></div> : null}
      <div className="mb-6 flex gap-1 border-b border-border">
        {(["pools", "search", "nurture"] as Tab[]).map((item) => <button key={item} onClick={() => setTab(item)} className={cn("-mb-px border-b px-4 py-3 text-xs font-medium", tab === item ? "border-text text-text" : "border-transparent text-text-muted")}>{t[item]}</button>)}
      </div>
      {tab === "pools" ? (
        <Pools
          pools={pools}
          selected={selectedPool}
          setSelected={(id) => {
            memberRequestGeneration.current += 1;
            setSelectedPool(id);
            setMembers([]);
            setMembersNextCursor(null);
            setMemberLoadError(false);
            loadMembers(id);
          }}
          members={members}
          membersHaveMore={membersNextCursor != null}
          memberLoadError={memberLoadError}
          pending={pending}
          t={t}
          onLoad={loadMembers}
          onLoadMore={loadMoreMembers}
          onAdd={() => setTab("search")}
          onDelete={(id) =>
            start(async () => {
              if (!window.confirm(t.deleteConfirm)) return;
              const result = await deleteTalentPoolAction(id);
              if (result.ok) {
                const nextPools = pools.filter((pool) => pool.id !== id);
                setPools(nextPools);
                setSelectedPool(nextPools[0]?.id ?? "");
                setMembers([]);
                setMembersNextCursor(null);
                setMemberLoadError(false);
                success();
              } else failure(result.error);
            })
          }
          onRemove={(candidateId) =>
            start(async () => {
              const result = await removeTalentPoolMemberAction(
                selectedPool,
                candidateId,
              );
              if (result.ok) {
                await fetchMembers(selectedPool);
                success();
              } else failure(result.error);
            })
          }
          onCreate={(name, description) =>
            start(async () => {
              const result = await createTalentPoolAction({
                name,
                description: description || null,
                color: null,
              });
              if (result.ok) {
                const pool = result.data as Pool;
                setPools((current) => [pool, ...current]);
                setSelectedPool(pool.id);
                setMembers([]);
                setMembersNextCursor(null);
                setMemberLoadError(false);
                success();
              } else failure(result.error);
            })
          }
        />
      ) : null}
      {tab === "search" ? (
        <SearchPanel
          pools={pools}
          positions={positions}
          selectedPool={selectedPool}
          setSelectedPool={setSelectedPool}
          results={results}
          total={resultTotal}
          hasMore={resultNextCursor != null}
          pending={pending}
          t={t}
          onSearch={(query) =>
            start(() => fetchResults({ kind: "search", value: query }))
          }
          onRediscover={(id) =>
            start(() => fetchResults({ kind: "position", value: id }))
          }
          onLoadMore={loadMoreResults}
          onAdd={add}
        />
      ) : null}
      {tab === "nurture" ? <NurturePanel pools={pools} sequences={initialSequences} templates={templates} pending={pending} t={t} run={(action) => start(async () => { const result = await action(); if (result.ok) success(); else failure(result.error); })} /> : null}
    </div>
  );
}

/** @ui-only Localized Talent CRM presentation copy. */
type T = { [K in keyof (typeof COPY)["en"]]: string };

function Pools({ pools, selected, setSelected, members, membersHaveMore, memberLoadError, pending, t, onLoad, onLoadMore, onAdd, onDelete, onRemove, onCreate }: { pools: Pool[]; selected: string; setSelected: (id: string) => void; members: Candidate[]; membersHaveMore: boolean; memberLoadError: boolean; pending: boolean; t: T; onLoad: () => void; onLoadMore: () => void; onAdd: () => void; onDelete: (id: string) => void; onRemove: (id: string) => void; onCreate: (name: string, description: string) => void }) {
  return <div className="grid gap-5 lg:grid-cols-[320px_1fr]">
    <section className="rounded-xl border border-border bg-bg-elevated p-4"><h2 className="mb-3 flex items-center gap-2 text-sm font-semibold"><FolderHeart className="h-4 w-4" />{t.pools}</h2><form className="space-y-2 border-b border-border pb-4" onSubmit={(e) => { e.preventDefault(); const d = new FormData(e.currentTarget); onCreate(String(d.get("name") || ""), String(d.get("description") || "")); e.currentTarget.reset(); }}><input required name="name" placeholder={t.poolName} className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm" /><textarea name="description" placeholder={t.description} rows={2} className="w-full resize-none rounded-md border border-border bg-bg px-3 py-2 text-sm" /><button disabled={pending} className="rounded-md bg-accent px-3 py-2 text-xs font-semibold text-white">{t.createPool}</button></form><div className="mt-3 space-y-1">{pools.map((pool) => <button key={pool.id} onClick={() => setSelected(pool.id)} className={cn("flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left", selected === pool.id ? "bg-bg text-text" : "text-text-muted hover:bg-bg")}><span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: pool.color || "var(--accent)" }} /><span className="min-w-0 flex-1 truncate text-sm">{pool.name}</span><span className="font-mono text-[10px]">{pool.member_count}</span></button>)}</div></section>
    <section className="rounded-xl border border-border bg-bg-elevated p-5"><div className="mb-4 flex items-center gap-2"><h2 className="min-w-0 flex-1 truncate text-sm font-semibold">{pools.find((p) => p.id === selected)?.name || t.pools}</h2><button disabled={!selected} onClick={onAdd} className="rounded-md border border-border px-2.5 py-1.5 text-xs disabled:opacity-40"><UserPlus className="mr-1 inline h-3.5 w-3.5" />{t.addTo}</button>{selected ? <button type="button" aria-label={`${t.deletePool}: ${pools.find((p) => p.id === selected)?.name ?? t.pools}`} onClick={() => onDelete(selected)} className="p-2 text-text-muted hover:text-red-600"><Trash2 className="h-4 w-4" /></button> : null}</div>{members.length === 0 ? <div className="py-12 text-center"><p className="text-sm text-text-muted">{memberLoadError ? t.memberLoadFailed : pools.length === 0 ? t.noPools : t.emptyPool}</p><button disabled={!selected || pending} onClick={onLoad} className="mt-3 text-xs text-accent underline">{memberLoadError ? t.retry : t.loadMembers}</button></div> : <><CandidateRows candidates={members} action={(candidate) => <button onClick={() => onRemove(candidate.id)} className="text-xs text-red-600">{t.remove}</button>} t={t} />{membersHaveMore ? <button type="button" disabled={pending} onClick={onLoadMore} className="mt-4 min-h-10 w-full rounded-md border border-border px-3 py-2 text-xs font-medium hover:bg-bg-hover disabled:opacity-50">{t.loadMore}</button> : null}</>}</section>
  </div>;
}

function SearchPanel({ pools, positions, selectedPool, setSelectedPool, results, total, hasMore, pending, t, onSearch, onRediscover, onLoadMore, onAdd }: { pools: Pool[]; positions: Position[]; selectedPool: string; setSelectedPool: (id: string) => void; results: Candidate[]; total: number; hasMore: boolean; pending: boolean; t: T; onSearch: (q: string) => void; onRediscover: (id: string) => void; onLoadMore: () => void; onAdd: (id: string) => void }) {
  return <section className="rounded-xl border border-border bg-bg-elevated p-5"><div className="grid gap-3 md:grid-cols-2"><form onSubmit={(e) => { e.preventDefault(); onSearch(String(new FormData(e.currentTarget).get("q") || "")); }}><label htmlFor="talent-search-query" className="text-xs text-text-muted">{t.find}</label><div className="mt-1 flex"><input id="talent-search-query" name="q" required minLength={2} placeholder={t.query} className="min-w-0 flex-1 rounded-l-md border border-border bg-bg px-3 py-2 text-sm" /><button type="submit" aria-label={t.runSearch} disabled={pending} className="rounded-r-md bg-accent px-3 text-white"><Search className="h-4 w-4" /></button></div></form><div><label htmlFor="talent-rediscover-position" className="text-xs text-text-muted">{t.rediscover}</label><select id="talent-rediscover-position" defaultValue="" onChange={(e) => e.target.value && onRediscover(e.target.value)} className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"><option value="">{t.allPositions}</option>{positions.map((p) => <option key={p.id} value={p.id}>{p.title}</option>)}</select></div></div><div className="mt-4 flex items-center gap-2 border-t border-border pt-4"><label htmlFor="talent-target-pool" className="sr-only">{t.choosePool}</label><select id="talent-target-pool" value={selectedPool} onChange={(e) => setSelectedPool(e.target.value)} className="rounded-md border border-border bg-bg px-2 py-1.5 text-xs"><option value="">{t.choosePool}</option>{pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select><span className="text-xs text-text-muted">{total} {t.match}</span>{pending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}</div>{results.length === 0 ? <p className="py-12 text-center text-sm text-text-muted">{t.noResults}</p> : <><CandidateRows candidates={results} action={(candidate) => <button onClick={() => onAdd(candidate.id)} className="text-xs text-accent">{t.addTo}</button>} t={t} />{hasMore ? <button type="button" disabled={pending} onClick={onLoadMore} className="mt-4 min-h-10 w-full rounded-md border border-border px-3 py-2 text-xs font-medium hover:bg-bg-hover disabled:opacity-50">{t.loadMore}</button> : null}</>}</section>;
}

function CandidateRows({ candidates, action, t }: { candidates: Candidate[]; action: (candidate: Candidate) => React.ReactNode; t: T }) { return <div className="divide-y divide-border">{candidates.map((candidate) => <div key={candidate.id} className="flex items-center gap-3 py-3"><div className="min-w-0 flex-1"><Link href={`/candidates/${candidate.id}`} className="truncate text-sm font-medium hover:underline">{candidate.source_ref || candidate.id}</Link><p className="mt-0.5 text-xs text-text-muted">{candidate.source}{candidate.best_position_title ? ` · ${candidate.best_position_title}` : ""}{candidate.best_score != null ? ` · ${candidate.best_score}/100` : ""}</p>{candidate.match_reasons?.length ? <div className="mt-1 flex flex-wrap gap-1">{candidate.match_reasons.map((reason) => <span key={reason} className="rounded bg-bg px-1.5 py-0.5 text-[10px]">{reason}</span>)}</div> : null}</div>{candidate.already_in_pipeline ? <span className="text-[10px] text-emerald-600">{t.inPipeline}</span> : null}{action(candidate)}</div>)}</div>; }

function NurturePanel({ pools, sequences, templates, pending, t, run }: { pools: Pool[]; sequences: Sequence[]; templates: Template[]; pending: boolean; t: T; run: (action: () => Promise<{ ok: boolean; error?: string }>) => void }) {
  return <div className="grid gap-5 lg:grid-cols-[360px_1fr]"><section className="rounded-xl border border-border bg-bg-elevated p-5"><h2 className="mb-3 flex items-center gap-2 text-sm font-semibold"><MailPlus className="h-4 w-4" />{t.createSequence}</h2>{templates.length === 0 ? <p className="text-xs text-amber-600">{t.noTemplates}</p> : <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); const d = new FormData(e.currentTarget); const template = templates.find((item) => item.id === d.get("template")); if (!template) return; run(() => createNurtureSequenceAction({ name: d.get("name"), pool_id: d.get("pool") || null, steps: [{ channel: template.channel, template_id: template.id, delay_days: Number(d.get("delay")) }] })); }}><input name="name" required placeholder={t.sequenceName} className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm" /><select required name="pool" className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"><option value="">{t.choosePool}</option>{pools.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select><select required name="template" className="w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"><option value="">{t.chooseTemplate}</option>{templates.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.channel}</option>)}</select><label className="block text-xs text-text-muted">{t.delay}<input name="delay" type="number" min={0} max={365} defaultValue={0} className="mt-1 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm" /></label><button disabled={pending} className="rounded-md bg-accent px-3 py-2 text-xs font-semibold text-white">{t.createSequence}</button></form>}</section><section className="rounded-xl border border-border bg-bg-elevated p-5"><h2 className="mb-3 text-sm font-semibold">{t.sequences}</h2><div className="space-y-3">{sequences.length === 0 ? <p className="py-12 text-center text-sm text-text-muted">{t.noSequences}</p> : sequences.map((sequence) => <div key={sequence.id} className="rounded-lg border border-border bg-bg p-4"><div className="flex items-start gap-2"><div className="min-w-0 flex-1"><h3 className="text-sm font-medium">{sequence.name}</h3><p className="mt-1 text-xs text-text-muted">{sequence.steps.map((step) => `${step.delay_days}d · ${step.channel} · ${step.template_name}`).join(" → ")}</p></div><button disabled={pending || !sequence.pool_id} onClick={() => run(() => launchNurtureSequenceAction(sequence.id))} className="rounded-md border border-border px-2.5 py-1.5 text-xs disabled:opacity-40"><Sparkles className="mr-1 inline h-3.5 w-3.5" />{t.launch}</button></div></div>)}</div></section></div>;
}
