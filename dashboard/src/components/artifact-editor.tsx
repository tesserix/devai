"use client";

/**
 * Artifact authoring editor — ported from the Agentic Registry's ArtifactEditor
 * so creating a Skill / Agent / Prompt / MCP server / Tool / Workflow / Blueprint
 * via DevAI matches the registry's own experience exactly: a two-pane modal with
 * a form on the left and a LIVE YAML/JSON manifest on the right, file upload,
 * continuous lint/validation, and a Publish action. Publishing posts the registry
 * CR manifest through DevAI's /api/registry/{plural} write path, so the artifact
 * shows up in both DevAI and the aregistry marketplace.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  FileCode,
  FileJson,
  Loader2,
  Plus,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { api } from "@/lib/api";
import { Select } from "@/components/ui/select";
import { dump as toYAML, parse as parseYAML } from "@/lib/yaml";
import {
  editorDocument,
  fieldsFor,
  getPath,
  lintManifest,
  pluralForKind,
  setPath,
  type Field,
} from "@/lib/registry-schemas";

type Doc = Record<string, unknown>;
type Format = "yaml" | "json";

function serialize(doc: Doc, fmt: Format): string {
  return fmt === "json" ? JSON.stringify(doc, null, 2) : toYAML(doc);
}
function parse(text: string, fmt: Format): Doc {
  const v = fmt === "json" ? JSON.parse(text) : parseYAML(text);
  if (v == null || typeof v !== "object") throw new Error("document must be an object");
  return v as Doc;
}
function formatFromName(name: string): Format {
  return name.toLowerCase().endsWith(".json") ? "json" : "yaml";
}
function rowsFromDoc(doc: Doc): [string, string][] {
  const l = (getPath(doc, "metadata.labels") as Record<string, string>) ?? {};
  return Object.entries(l);
}
function labelsObject(rows: [string, string][]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of rows) if (k.trim()) out[k.trim()] = v;
  return out;
}

// Team is OPTIONAL metadata — a label, never part of the artifact's identity.
// The tenant (namespace) is the uniqueness boundary; team just groups/filters
// within it. Mirrors the seed convention (community seeds use devai.io/team).
const TEAM_LABEL = "devai.io/team";
function teamOf(rows: [string, string][]): string {
  return rows.find(([k]) => k === TEAM_LABEL)?.[1] ?? "";
}
function withTeam(rows: [string, string][], team: string): [string, string][] {
  const rest = rows.filter(([k]) => k !== TEAM_LABEL);
  return team.trim() ? [...rest, [TEAM_LABEL, team.trim()]] : rest;
}
// A 409 from the publish path means the name is already taken in the tenant.
function isConflict(msg: string): boolean {
  return /\b409\b/.test(msg) || /already exists/i.test(msg);
}

export default function ArtifactEditor({
  kind,
  open,
  onClose,
  onCreated,
  initialDocument,
}: {
  kind: string;
  open: boolean;
  onClose: () => void;
  onCreated: (name: string) => void;
  initialDocument?: Doc;
}) {
  const plural = useMemo(() => pluralForKind(kind), [kind]);
  const fields = useMemo(() => fieldsFor(kind), [kind]);
  const editing = initialDocument != null;
  const originalName = String(getPath(initialDocument, "metadata.name") ?? "").trim();
  const [doc, setDoc] = useState<Doc>(() => editorDocument(kind, initialDocument));
  const [format, setFormat] = useState<Format>("yaml");
  const [raw, setRaw] = useState<string>("");
  const [labelRows, setLabelRows] = useState<[string, string][]>([]);
  const [parseErr, setParseErr] = useState<string | null>(null);
  const [submitErr, setSubmitErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [teamOptions, setTeamOptions] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const d = editorDocument(kind, initialDocument);
    setDoc(d);
    setRaw(serialize(d, format));
    setLabelRows(rowsFromDoc(d));
    setParseErr(null);
    setSubmitErr(null);
    setConflict(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, kind, initialDocument]);

  // The user's teams — optional grouping. Free-text is still allowed, so a
  // catalog blip never blocks authoring.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    api
      .listTeams()
      .then((ts) => {
        if (!cancelled) setTeamOptions(ts.map((t) => t.name).filter(Boolean));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open]);

  function applyDoc(next: Doc) {
    setDoc(next);
    setRaw(serialize(next, format));
    setParseErr(null);
    setConflict(false);
  }
  function onField(path: string, value: string) {
    applyDoc(setPath(doc, path, value));
  }
  function onFieldVal(path: string, value: unknown) {
    applyDoc(setPath(doc, path, value));
  }
  function onLabels(rows: [string, string][]) {
    setLabelRows(rows);
    applyDoc(setPath(doc, "metadata.labels", labelsObject(rows)));
  }
  function onRaw(text: string) {
    setRaw(text);
    try {
      const parsed = parse(text, format);
      setDoc(parsed);
      setLabelRows(rowsFromDoc(parsed));
      setParseErr(null);
    } catch (e) {
      setParseErr(e instanceof Error ? e.message : String(e));
    }
  }
  function switchFormat(next: Format) {
    setFormat(next);
    setRaw(serialize(doc, next));
    setParseErr(null);
  }
  async function onUpload(file: File | null | undefined) {
    if (!file) return;
    const fmt = formatFromName(file.name);
    try {
      const text = await file.text();
      setFormat(fmt);
      setSubmitErr(null);
      setRaw(text);
      try {
        const parsed = parse(text, fmt);
        setDoc(parsed);
        setLabelRows(rowsFromDoc(parsed));
        setParseErr(null);
      } catch (e) {
        setParseErr(e instanceof Error ? e.message : String(e));
      }
    } catch (e) {
      setParseErr(`could not read file: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  const name = String(getPath(doc, "metadata.name") ?? "").trim();
  const nameChanged = editing && name !== originalName;
  const lint = useMemo(() => (parseErr ? [] : lintManifest(doc, kind)), [doc, kind, parseErr]);
  const lintErrors = lint.filter((i) => i.level === "error");
  const lintWarnings = lint.filter((i) => i.level === "warning");
  const canSubmit = !!name && !nameChanged && !parseErr && lintErrors.length === 0 && !submitting;

  const team = teamOf(labelRows);
  function onTeam(value: string) {
    onLabels(withTeam(labelRows, value));
  }

  async function submit(overwrite = false) {
    if (!canSubmit) return;
    setSubmitting(true);
    setSubmitErr(null);
    const clean = setPath(doc, "metadata.labels", labelsObject(labelRows));
    try {
      await api.publishArtifact(plural, clean, overwrite);
      onCreated(name);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setSubmitErr(msg);
      setConflict(isConflict(msg));
    } finally {
      setSubmitting(false);
    }
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center"
      style={{ background: "var(--surface-overlay)", padding: "5vh 16px" }}
      onMouseDown={onClose}
    >
      <div
        className="panel-raised flex flex-col w-full"
        style={{ maxWidth: 1000, maxHeight: "90vh" }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 h-16 border-b shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          <div>
            <div className="label-eyebrow">{editing ? "Edit artifact" : "New artifact"}</div>
            <h2 className="font-serif text-xl font-semibold leading-tight" style={{ color: "var(--ink-strong)" }}>
              {kind}
            </h2>
          </div>
          <button className="btn-ghost" style={{ padding: 8 }} onClick={onClose} aria-label="Close">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Two-pane body */}
        <div className="grid grid-cols-1 lg:grid-cols-2 min-h-0 flex-1">
          {/* Form */}
          <div className="overflow-y-auto p-6 space-y-4 border-b lg:border-b-0 lg:border-r" style={{ borderColor: "var(--border-subtle)" }}>
            {fields.map((f) => {
              if (f.type === "ref") {
                return (
                  <RefInput
                    key={f.path}
                    f={f}
                    value={String(getPath(doc, f.path) ?? "")}
                    onChange={(v) => onField(f.path, v)}
                  />
                );
              }
              if (f.type === "refList") {
                return (
                  <RefListInput
                    key={f.path}
                    f={f}
                    value={(getPath(doc, f.path) as unknown[]) ?? []}
                    onChange={(v) => onFieldVal(f.path, v)}
                  />
                );
              }
              if (f.type === "group") {
                return (
                  <div key={f.path}>
                    <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-soft)" }}>
                      {f.label}
                    </label>
                    {f.help && <p className="text-[11px] mb-2 -mt-1" style={{ color: "var(--ink-muted)" }}>{f.help}</p>}
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 rounded-md p-3" style={{ background: "var(--surface-muted)", border: "1px solid var(--border-subtle)" }}>
                      {(f.children ?? []).map((c) => (
                        <FieldInput
                          key={c.path}
                          f={c}
                          value={String(getPath(doc, `${f.path}.${c.path}`) ?? "")}
                          onChange={(v) => onFieldVal(`${f.path}.${c.path}`, v)}
                        />
                      ))}
                    </div>
                  </div>
                );
              }
              return (
                <FieldInput
                  key={f.path}
                  f={f}
                  value={String(getPath(doc, f.path) ?? "")}
                  onChange={(v) => onFieldVal(f.path, v)}
                  disabled={editing && f.path === "metadata.name"}
                />
              );
            })}

            {/* Team — optional grouping within the tenant (a label, not identity) */}
            <div>
              <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-soft)" }}>
                Team <span style={{ color: "var(--ink-muted)" }}>· optional</span>
              </label>
              <input
                className="field w-full"
                list="artifact-team-list"
                placeholder="e.g. platform — leave blank for the whole tenant"
                value={team}
                onChange={(e) => onTeam(e.target.value)}
              />
              <datalist id="artifact-team-list">
                {teamOptions.map((t) => (
                  <option key={t} value={t} />
                ))}
              </datalist>
              <p className="text-[11px] mt-1" style={{ color: "var(--ink-muted)" }}>
                Stored as the <span className="font-mono">{TEAM_LABEL}</span> label for filtering. The name must be
                unique across the whole tenant regardless of team.
              </p>
            </div>

            {/* Labels */}
            <div>
              <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-soft)" }}>
                Labels
              </label>
              <div className="space-y-2">
                {labelRows.map(([k, v], i) => (
                  <div key={i} className="flex gap-2">
                    <input
                      className="field flex-1 font-mono text-[12px]"
                      placeholder="key"
                      value={k}
                      onChange={(e) => onLabels(labelRows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))}
                    />
                    <input
                      className="field flex-1 font-mono text-[12px]"
                      placeholder="value"
                      value={v}
                      onChange={(e) => onLabels(labelRows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))}
                    />
                    <button className="btn-ghost shrink-0" style={{ padding: 8 }} onClick={() => onLabels(labelRows.filter((_, j) => j !== i))}>
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
                <button className="btn-secondary text-[12px]" style={{ padding: "5px 10px" }} onClick={() => onLabels([...labelRows, ["", ""]])}>
                  <Plus className="w-3.5 h-3.5" /> Add label
                </button>
              </div>
            </div>
          </div>

          {/* Live manifest */}
          <div className="flex flex-col min-h-0" style={{ background: "var(--surface-muted)" }}>
            <div className="flex items-center justify-between px-4 h-11 border-b shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
              <span className="label-eyebrow">Manifest · live</span>
              <div className="flex items-center gap-1">
                <input
                  ref={fileInput}
                  type="file"
                  accept=".yaml,.yml,.json,application/json,application/x-yaml,text/yaml,text/plain"
                  className="hidden"
                  onChange={(e) => onUpload(e.target.files?.[0])}
                />
                <button className="btn-ghost" style={{ padding: "4px 9px", fontSize: 12 }} onClick={() => fileInput.current?.click()} title="Upload a YAML or JSON manifest">
                  <Upload className="w-3.5 h-3.5" /> Upload
                </button>
                <span className="mx-1" style={{ width: 1, height: 16, background: "var(--border-subtle)" }} />
                <button className={format === "yaml" ? "btn-secondary" : "btn-ghost"} style={{ padding: "4px 9px", fontSize: 12 }} onClick={() => switchFormat("yaml")}>
                  <FileCode className="w-3.5 h-3.5" /> YAML
                </button>
                <button className={format === "json" ? "btn-secondary" : "btn-ghost"} style={{ padding: "4px 9px", fontSize: 12 }} onClick={() => switchFormat("json")}>
                  <FileJson className="w-3.5 h-3.5" /> JSON
                </button>
              </div>
            </div>
            <textarea
              spellCheck={false}
              value={raw}
              onChange={(e) => onRaw(e.target.value)}
              className="flex-1 font-mono text-[12.5px] leading-relaxed p-4 resize-none outline-none"
              style={{ background: "transparent", color: "var(--ink)", minHeight: 280, tabSize: 2 }}
            />
            {parseErr ? (
              <div className="px-4 py-2 text-[12px] border-t font-mono flex items-center gap-2" style={{ borderColor: "var(--border-subtle)", color: "var(--error-ink)", background: "var(--error-soft-bg)" }}>
                <ShieldAlert className="w-3.5 h-3.5 shrink-0" /> {parseErr}
              </div>
            ) : (
              <div className="border-t shrink-0 max-h-[34%] overflow-y-auto" style={{ borderColor: "var(--border-subtle)" }}>
                {lint.length === 0 ? (
                  <div className="px-4 py-2 text-[12px] flex items-center gap-2" style={{ color: "var(--ink-soft)" }}>
                    <ShieldCheck className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--accent-soft-ink)" }} />
                    Validated · conforms to the {kind} standard, no unsafe content.
                  </div>
                ) : (
                  <ul className="py-1.5">
                    {lintErrors.map((iss, i) => (
                      <li key={`e${i}`} className="px-4 py-1 text-[12px] flex items-start gap-2" style={{ color: "var(--error-ink)" }}>
                        <ShieldAlert className="w-3.5 h-3.5 shrink-0 mt-0.5" /> <span>{iss.message}</span>
                      </li>
                    ))}
                    {lintWarnings.map((iss, i) => (
                      <li key={`w${i}`} className="px-4 py-1 text-[12px] flex items-start gap-2" style={{ color: "var(--ink-soft)" }}>
                        <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" /> <span>{iss.message}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between gap-3 px-6 h-16 border-t shrink-0" style={{ borderColor: "var(--border-subtle)" }}>
          <span className="text-[12px]" style={{ color: submitErr || lintErrors.length ? "var(--error-ink)" : "var(--ink-muted)" }}>
            {submitErr
              ? submitErr
              : nameChanged
                ? "An existing agent's name cannot be changed — restore the published name to continue"
              : lintErrors.length
                ? `${lintErrors.length} validation ${lintErrors.length === 1 ? "issue" : "issues"} — fix before publishing`
                : "Published to your tenant · the name must be unique within it"}
          </span>
          <div className="flex items-center gap-2">
            <button className="btn-secondary" onClick={onClose}>Cancel</button>
            {!editing && conflict && (
              <button
                className="btn-secondary"
                disabled={submitting}
                onClick={() => submit(true)}
                title="The name is taken — publish a new version of the existing artifact instead"
              >
                Publish as new version
              </button>
            )}
            <button className="btn-primary" disabled={!canSubmit} style={{ opacity: canSubmit ? 1 : 0.5 }} onClick={() => submit(editing)}>
              {submitting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
              {editing ? "Publish new version" : `Publish ${kind}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function FieldInput({ f, value, onChange, disabled = false }: { f: Field; value: string; onChange: (v: unknown) => void; disabled?: boolean }) {
  const dimmed = disabled ? { opacity: 0.6, cursor: "not-allowed" } : undefined;
  return (
    <div>
      <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-soft)" }}>
        {f.label} {f.required && <span style={{ color: "var(--error-ink)" }}>*</span>}
      </label>
      {f.type === "select" ? (
        <Select
          value={value || f.options?.[0] || ""}
          onChange={(next) => onChange(next)}
          disabled={disabled}
          options={(f.options ?? []).map((o) => ({ value: o, label: o }))}
          ariaLabel={f.label}
        />
      ) : f.type === "textarea" ? (
        <textarea
          className={`field w-full ${f.mono ? "font-mono text-[12.5px]" : ""}`}
          style={dimmed}
          disabled={disabled}
          rows={3}
          placeholder={f.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : f.type === "number" ? (
        <input
          className={`field ${f.mono ? "font-mono text-[13px]" : ""}`}
          style={dimmed}
          disabled={disabled}
          type="number"
          min={f.min}
          max={f.max}
          step={f.step}
          placeholder={f.placeholder}
          value={value}
          onChange={(event) => onChange(event.target.value === "" ? null : Number(event.target.value))}
        />
      ) : (
        <input
          className={`field ${f.mono ? "font-mono text-[13px]" : ""}`}
          style={dimmed}
          disabled={disabled}
          placeholder={f.placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {f.help && (
        <p className="text-[11px] mt-1" style={{ color: "var(--ink-muted)" }}>
          {f.help}
        </p>
      )}
    </div>
  );
}

function RefInput({ f, value, onChange }: { f: Field; value: string; onChange: (v: string) => void }) {
  const [candidates, setCandidates] = useState<string[]>([]);
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .listRegistry(f.itemKind ?? "prompts")
      .then((items) => {
        if (!cancelled) setCandidates(items.map((item) => item.name).filter(Boolean));
      })
      .catch(() => {
        if (!cancelled) setUnreachable(true);
      });
    return () => {
      cancelled = true;
    };
  }, [f.itemKind]);

  return (
    <div>
      <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-soft)" }}>
        {f.label}
      </label>
      <input
        className="field w-full font-mono text-[13px]"
        list={`ref-${f.path}`}
        placeholder={unreachable ? "catalog unavailable — type a name" : f.placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <datalist id={`ref-${f.path}`}>
        {candidates.map((candidate) => (
          <option key={candidate} value={candidate} />
        ))}
      </datalist>
      {f.help && (
        <p className="text-[11px] mt-1" style={{ color: "var(--ink-muted)" }}>
          {f.help}
        </p>
      )}
    </div>
  );
}

function RefListInput({ f, value, onChange }: { f: Field; value: unknown[]; onChange: (v: unknown[]) => void }) {
  const [candidates, setCandidates] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [unreachable, setUnreachable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .listRegistry(f.itemKind ?? "skills")
      .then((items) => {
        if (!cancelled) setCandidates(items.map((a) => a.name).filter(Boolean));
      })
      .catch(() => {
        if (!cancelled) setUnreachable(true);
      });
    return () => {
      cancelled = true;
    };
  }, [f.itemKind]);

  const entries = value.map((e) =>
    typeof e === "string" ? e : ((e as Record<string, unknown>)?.name as string) ?? ((e as Record<string, unknown>)?.id as string) ?? "(inline)",
  );

  function add(name: string) {
    const n = name.trim();
    if (!n || entries.includes(n)) return;
    onChange([...value, n]);
    setDraft("");
  }
  function remove(i: number) {
    onChange(value.filter((_, idx) => idx !== i));
  }

  const remaining = candidates.filter((c) => !entries.includes(c));
  const chipBase = "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] font-mono border";

  return (
    <div>
      <label className="block text-[12px] font-medium mb-1.5" style={{ color: "var(--ink-soft)" }}>
        {f.label}
      </label>
      {entries.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-2">
          {entries.map((nm, i) => {
            const known = candidates.includes(nm) || nm === "(inline)" || unreachable;
            return (
              <span
                key={`${nm}-${i}`}
                className={chipBase}
                style={
                  known
                    ? { background: "var(--surface-muted)", borderColor: "var(--border-subtle)", color: "var(--ink-soft)" }
                    : { background: "var(--error-soft-bg)", borderColor: "var(--error-soft-bd)", color: "var(--error-ink)" }
                }
                title={!known ? "not found in the catalog" : undefined}
              >
                {nm}
                <button type="button" onClick={() => remove(i)} aria-label={`Remove ${nm}`} className="ml-0.5">
                  <X className="w-3 h-3" />
                </button>
              </span>
            );
          })}
        </div>
      )}
      <div className="flex gap-2">
        <input
          className="field flex-1"
          list={`reflist-${f.path}`}
          placeholder={unreachable ? "catalog unavailable — type a name" : `Add a ${f.itemKind?.replace(/s$/, "") ?? "ref"}…`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add(draft);
            }
          }}
        />
        <datalist id={`reflist-${f.path}`}>
          {remaining.map((c) => (
            <option key={c} value={c} />
          ))}
        </datalist>
        <button type="button" className="btn-secondary" onClick={() => add(draft)}>
          <Plus className="w-4 h-4" /> Add
        </button>
      </div>
      {f.help && (
        <p className="text-[11px] mt-1" style={{ color: "var(--ink-muted)" }}>
          {f.help}
        </p>
      )}
    </div>
  );
}
